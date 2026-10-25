"""Pure functions (no Klipper imports): summarise how the force responds at
each slow->fast (rising) and fast->slow (falling) edge of a PA segment.

For one PA value every sweep gives one edge. We cut a window around each
edge, resample it, average the windows and normalise to 0..1. Then:
  rise      10-90 % rise time in seconds (smaller = crisper response)
  overshoot how far the mean overshoots the plateau (0.05 = 5 %)
  snr       edge amplitude divided by baseline noise (low = do not trust)
Force at the extruder is not exactly pressure at the nozzle, so treat these
as indicators for comparing PA values, not as absolute truth."""
import bisect
import math

PRE = 0.30          # seconds before the edge kept in the window
GUARD = 0.04        # seconds trimmed before the next edge so it cannot leak in
MIN_PRE = 0.10      # shortest baseline we accept before an edge
MIN_POST = 0.10     # shortest settled stretch we accept after an edge
POINTS = 80


def _interp(t, f, x):
    i = bisect.bisect_left(t, x)
    if i <= 0:
        return f[0]
    if i >= len(t):
        return f[-1]
    t0, t1 = t[i - 1], t[i]
    if t1 == t0:
        return f[i]
    return f[i - 1] + (f[i] - f[i - 1]) * (x - t0) / (t1 - t0)


def _windows(markers, kind, tmax, skip_first=False):
    """[(t_edge, pre_s, post_s)] for every usable edge.
    Works for back-to-back legs (slow, fast, slow, ...) and for legs with
    rests between them: the window before an edge reaches back no further
    than the previous flow change, the window after it stops before the
    next flow change (minus GUARD), so neighbouring edges never leak in."""
    legs = [m for m in markers if m.get('name') in ('slow', 'fast')]
    cuts = sorted({round(m['t0'], 6) for m in legs} |
                  {round(m['t1'], 6) for m in legs} | {0.0})
    out = []
    fasts = [m for m in legs if m.get('name') == 'fast']
    if skip_first and len(fasts) > 1:
        # the first pulse after the lead-in is often not representative
        first = min(m.get('sweep', 0) for m in fasts)
        fasts = [m for m in fasts if m.get('sweep', 0) != first]
    for m in fasts:
        te = m['t0'] if kind == 'rise' else m['t1']
        before = [c for c in cuts if c < te - 1e-6]
        after = [c for c in cuts if c > te + 1e-6]
        tp = before[-1] if before else te - PRE - GUARD
        tn = after[0] if after else tmax
        pre = max(MIN_PRE, min(PRE, te - tp - GUARD))
        post = max(MIN_POST, min(2.0, tn - te - GUARD))
        out.append((te, pre, post))
    return out


def edge_summary(t, f, markers, kind='rise', npts=POINTS, skip_first=False):
    """Return dict or None when there is not enough data."""
    wins = _windows(markers, kind, t[-1] if t else 0.0, skip_first)
    if not wins or len(t) < 20:
        return None
    pre = min(w[1] for w in wins)
    post = min(w[2] for w in wins)
    grid = [-pre + (pre + post) * i / (npts - 1) for i in range(npts)]
    rows = []
    for te, _, _ in wins:
        if te - pre < t[0] - 1e-9 or te + post > t[-1] + 1e-9:
            continue                      # window not fully recorded
        rows.append([_interp(t, f, te + g) for g in grid])
    if not rows:
        return None
    n = len(rows)
    mean = [sum(r[i] for r in rows) / n for i in range(npts)]
    base_idx = [i for i, g in enumerate(grid) if g <= -0.05]
    if len(base_idx) < 3:
        return None
    base = sum(mean[i] for i in base_idx) / len(base_idx)
    tail = [mean[i] for i in range(int(npts * 0.75), npts)]
    plateau = sum(tail) / len(tail)
    amp = plateau - base
    sd = math.sqrt(sum((mean[i] - base) ** 2 for i in base_idx) / len(base_idx))
    if abs(amp) < 1e-9:
        return None
    y = [(v - base) / amp for v in mean]
    res = {'kind': kind, 'n': n, 'pre': round(pre, 3), 'post': round(post, 3), 'amp': round(amp, 3),
           'snr': round(abs(amp) / sd, 1) if sd > 1e-12 else None,
           't': [round(g, 4) for g in grid], 'y': [round(v, 4) for v in y],
           'rise': None, 'overshoot': None, 't10': None,
           'skipped_first': bool(skip_first)}
    t10 = t90 = None
    for i in range(1, npts):
        if grid[i] < -0.03:         # edge time is not exact; allow a little early
            continue
        if t10 is None and y[i] >= 0.1 > y[i - 1]:
            t10 = grid[i - 1] + (grid[i] - grid[i - 1]) * (0.1 - y[i - 1]) / (y[i] - y[i - 1])
        if t90 is None and y[i] >= 0.9 > y[i - 1]:
            t90 = grid[i - 1] + (grid[i] - grid[i - 1]) * (0.9 - y[i - 1]) / (y[i] - y[i - 1])
    if t10 is not None:
        res['t10'] = round(t10, 4)
    if t10 is not None and t90 is not None:
        res['rise'] = round(t90 - t10, 4)
    res['overshoot'] = round(max(0.0, max(v for g, v in zip(grid, y) if g >= 0) - 1.0), 4)
    return res


# ---------------------------------------------------------------------
# Models: turn the per-PA edge summaries of one run into a PA pick.
#   rise_fast        speed-up edges: fastest rise whose overshoot stays
#                    under the limit (0.05 + 3/snr)
#   fall_slow        slow-down edges: fastest fall whose undershoot stays
#                    under the limit
#   fall_undershoot  slow-down edges only: going up in PA, find where the
#                    force first dips below the settled slow line by more
#                    than the noise (max(2 %, 3/snr)) and keeps dipping at
#                    the next value; the pick is the PA just before it.
MODELS = {
    'rise_fast': 'Rise fast',
    'fall_slow': 'Fall slow',
    'fall_undershoot': 'Fall undershoot',
    'oznlab': 'OznLab',
}
DIP_MIN = 0.02


def _edges(segments, kind):
    out = []
    for s in segments:
        e = (s.get('edge') or {}).get(kind)
        if e and not s.get('partial') and s.get('pa') is not None:
            out.append((s['pa'], s['idx'], e))
    out.sort(key=lambda x: x[0])
    return out


def _limit(e):
    snr = e.get('snr')
    return 0.05 + (3.0 / snr if snr else 0.05)


def _fastest(segments, kind):
    best = None
    for pa, idx, e in _edges(segments, kind):
        if e.get('rise') is None or e.get('overshoot') is None:
            continue
        if e['overshoot'] <= _limit(e) and (best is None
                                            or e['rise'] < best[2]['rise']):
            best = (pa, idx, e)
    if best is None:
        return {'pa': None, 'idx': None,
                'why': 'no PA value passed the %s limit'
                       % ('overshoot' if kind == 'rise' else 'undershoot')}
    word = 'rise' if kind == 'rise' else 'fall'
    return {'pa': best[0], 'idx': best[1],
            'why': 'fastest %s (%.0f ms) with %s %.0f%% under the %.0f%% '
                   'limit' % (word, best[2]['rise'] * 1000,
                              'overshoot' if kind == 'rise' else 'undershoot',
                              best[2]['overshoot'] * 100,
                              _limit(best[2]) * 100)}


def dip_threshold(e, dip_min=DIP_MIN):
    snr = e.get('snr')
    return max(dip_min, 3.0 / snr if snr else 0.05)


def _undershoot(segments, dip_min=DIP_MIN):
    edges = [x for x in _edges(segments, 'fall')
             if x[2].get('overshoot') is not None]
    if len(edges) < 2:
        return {'pa': None, 'idx': None, 'why': 'not enough PA values'}
    over = [x[2]['overshoot'] > dip_threshold(x[2], dip_min) for x in edges]
    for i, dips in enumerate(over):
        if not dips:
            continue
        if i + 1 < len(over) and not over[i + 1]:
            continue                      # a single blip: not a real start
        if i == 0:
            return {'pa': None, 'idx': None,
                    'why': 'already dips at the lowest PA (%.3f): lower '
                           'PA_START' % edges[0][0]}
        pa, idx, e = edges[i - 1]
        return {'pa': pa, 'idx': idx,
                'why': 'last value before the fall dips below the line '
                       '(dip starts at %.3f: %.0f%% > %.0f%%)'
                       % (edges[i][0], edges[i][2]['overshoot'] * 100,
                          dip_threshold(edges[i][2], dip_min) * 100)}
    pa, idx, e = edges[-1]
    return {'pa': pa, 'idx': idx,
            'why': 'no dip found up to %.3f: raise PA_END to be sure' % pa}


def pick(segments, model, dip_min=DIP_MIN):
    if model == 'rise_fast':
        return _fastest(segments, 'rise')
    if model == 'fall_slow':
        return _fastest(segments, 'fall')
    if model == 'fall_undershoot':
        return _undershoot(segments, dip_min)
    if model == 'oznlab':
        return ozn_pick(segments)
    raise ValueError('unknown model %r' % model)


def all_picks(segments, dip_min=DIP_MIN):
    return {m: pick(segments, m, dip_min) for m in MODELS}


def timing_offset(segments):
    """How long after the commanded speed change the force reaches 10 % of
    its step, taken from the crispest third of PA values (where the melt
    lag is mostly compensated). It is the sensor + processing delay, and
    can be slightly negative because Klipper's PA smoothing starts the
    extruder a little before the commanded change. Seconds or None."""
    out = {}
    for kind in ('rise', 'fall'):
        rows = [e for _, _, e in _edges(segments, kind)
                if e.get('t10') is not None and e.get('rise') is not None]
        if not rows:
            continue
        rows.sort(key=lambda e: e['rise'])
        best = rows[:max(1, len(rows) // 3)]
        vals = sorted(e['t10'] for e in best)
        out[kind] = round(vals[len(vals) // 2], 4)
    if not out:
        return None
    return round(sum(out.values()) / len(out), 4)


def command_fit(t, f, markers, flows):
    """Fit force = offset + gain * flow using the settled second half of
    every slow / fast leg. Returns {'offset', 'gain'} or None."""
    xs, ys = [], []
    for m in markers:
        flow = (flows or {}).get(m.get('name'))
        if flow is None:
            continue
        d = m['t1'] - m['t0']
        a, b = m['t0'] + 0.5 * d, m['t1'] - 0.05 * d
        lo, hi = bisect.bisect_left(t, a), bisect.bisect_left(t, b)
        if hi - lo < 3:
            continue
        xs.append(flow)
        ys.append(sum(f[lo:hi]) / (hi - lo))
    if len(set(xs)) < 2:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    gain = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return {'offset': round(my - gain * mx, 3), 'gain': round(gain, 4)}


# ---------------------------------------------------------------------
# OznLab model, ported from OznLab sensor's OZNLAB_PA_AUTO
# (github.com/ozancs/oznlab-sensor, GPL-3.0, score() / judge()).
# Their rule, which matched printed pattern tests on their printers:
# raise PA until the force dips below its new level after a slow-down;
# the last PA without that dip is the answer.
#   * every slow-down is measured against a straight baseline from the slow
#     level before that pulse to the slow level after it (cancels drift)
#   * the pulses are averaged into one curve; the dip is its deepest 40 ms
#     mean, as a fraction of the pressure step
#   * a dip counts only beyond max(noise limit, the dip already seen at
#     PA 0 x 1.3 + 0.01): PA 0 cannot undershoot, so that is the floor
# Window times scale with the leg lengths so the same code also scores
# runs made with other pulse shapes (OznLab uses 0.6 s fast, 1.0 s slow).
OZN_WIN = 0.04


def _mean(t, f, a, b):
    lo, hi = bisect.bisect_left(t, a), bisect.bisect_right(t, b)
    return (sum(f[lo:hi]) / (hi - lo)) if hi - lo >= 3 else None


def _detrended_sigma(t, f, a, b):
    lo, hi = bisect.bisect_left(t, a), bisect.bisect_right(t, b)
    n = hi - lo
    if n < 12:
        return None
    ts, fs = t[lo:hi], f[lo:hi]
    mt, mf = sum(ts) / n, sum(fs) / n
    sxx = sum((x - mt) ** 2 for x in ts)
    if sxx <= 0:
        return None
    k = sum((x - mt) * (y - mf) for x, y in zip(ts, fs)) / sxx
    return math.sqrt(sum((y - mf - k * (x - mt)) ** 2
                         for x, y in zip(ts, fs)) / (n - 2))


def ozn_score(t, f, markers, smooth=0.04, skip_first=True):
    """One PA value -> {'dip', 'limit', 'mismatch', 'amp', 'sigma', 'n',
    'spread', 'curve', 'dt'} or None. dip < 0 means the force fell below
    its new level after the slow-downs (as a fraction of the step)."""
    if len(t) < 50:
        return None
    h = min(max(smooth, 0.01), 0.1) / 2.
    fasts = sorted((m for m in markers if m.get('name') == 'fast'),
                   key=lambda m: m['t0'])
    slows = sorted((m for m in markers if m.get('name') == 'slow'),
                   key=lambda m: m['t0'])
    if skip_first and len(fasts) > 1:
        fasts = fasts[1:]
    sps = (len(t) - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 320.
    n_win = max(4, int(round(OZN_WIN * sps)))
    m_up, m_dn, amps, curves, sig = [], [], [], [], []
    for m in fasts:
        tu, td = m['t0'], m['t1']
        nxt = [x for x in slows if x['t0'] >= td - 1e-6]
        t_end = nxt[0]['t1'] if nxt else t[-1]
        t_slow, t_fast = t_end - td, td - tu
        if t_slow < 0.15 or t_fast < 0.1:
            continue
        pre_a, pre_b = tu - min(0.30, 0.3 * t_slow + 0.03), tu - 0.03
        pre = _mean(t, f, pre_a, pre_b)
        early_u = _mean(t, f, tu + h + 0.01, tu + h + min(0.10, 0.4 * t_fast))
        late_u = _mean(t, f, tu + max(h + 0.1, 0.58 * t_fast), td - h - 0.02)
        early_d = _mean(t, f, td + h + 0.01, td + h + 0.10)
        ld_a, ld_b = td + 0.5 * t_slow, td + t_slow - h - 0.05
        late_d = _mean(t, f, ld_a, ld_b)
        if None in (pre, early_u, late_u, early_d, late_d):
            continue
        amp = late_u - late_d
        sg = _detrended_sigma(t, f, ld_a, ld_b)
        if sg is not None and abs(amp) < 5 * sg:
            continue                     # no real pressure step here
        if abs(amp) < 1e-9:
            continue
        m_up.append((early_u - late_u) / amp)
        m_dn.append((early_d - late_d) / amp)
        amps.append(amp)
        tp, ta = 0.5 * (pre_a + pre_b), 0.5 * (ld_a + ld_b)

        def base(x, pre=pre, late_d=late_d, tp=tp, ta=ta):
            return pre + (late_d - pre) * (x - tp) / (ta - tp)
        a, b = td + h + 0.005, td + h + min(0.30, t_slow - h - 0.06)
        lo, hi = bisect.bisect_left(t, a), bisect.bisect_right(t, b)
        curves.append([(f[i] - base(t[i])) / amp for i in range(lo, hi)])
        if sg is not None:
            sig.append(sg)
    if not amps or not curves:
        return None
    L = min(len(c) for c in curves)
    if L < n_win + 2:
        return None
    avg = [sum(c[k] for c in curves) / len(curves) for k in range(L)]
    acc = [0.]
    for x in avg:
        acc.append(acc[-1] + x)
    u = min((acc[k + n_win] - acc[k]) / n_win for k in range(L - n_win + 1))
    amp_m = sum(amps) / len(amps)
    sig_m = sorted(sig)[len(sig) // 2] if sig else 0.
    thr = max(0.03, 4. * sig_m / math.sqrt(n_win * len(curves)) / abs(amp_m))
    a_abs = [abs(x) for x in amps]
    stp = max(1, int(round(0.005 * sps)))
    return {'dip': round(u, 4), 'limit': round(thr, 4),
            'mismatch': round(0.5 * (sum(m_dn) / len(m_dn)
                                     - sum(m_up) / len(m_up)), 4),
            'amp': round(amp_m, 3), 'sigma': round(sig_m, 3),
            'n': len(curves),
            'spread': round(max(a_abs) / max(min(a_abs), 1e-9), 3),
            'curve': [round(avg[k], 4) for k in range(0, L, stp)],
            'dt': round(stp / sps, 5), 'start': round(h + 0.005, 4)}


def ozn_floor(dip0):
    """No compensation cannot undershoot: the dip seen at PA 0 is noise."""
    return 1.3 * max(0., -dip0) + 0.01 if dip0 is not None and dip0 < 0 \
        else 0.


def ozn_depth(sc, floor):
    """How far the dip goes beyond its limit (0 = no dip, PA not too high)."""
    lim = max(sc['limit'], floor)
    return max(0., -sc['dip'] - lim), lim


def ozn_zero(points):
    """OznLab's square-wave crossing: PA where a line through the two
    (PA, mismatch) points closest to zero crosses zero (slope must fall)."""
    pts = sorted(points, key=lambda r: abs(r[1]))[:2]
    if len(pts) < 2:
        return None
    (k1, m1), (k2, m2) = pts
    if k1 == k2 or m1 == m2:
        return None
    b = (m2 - m1) / (k2 - k1)
    if b >= 0:
        return None
    k = k1 - m1 / b
    return round(k, 4) if 0 < k < 2 else None


def ozn_pick(segments):
    """Last PA without a dip below the line after the slow-downs. Repeats
    of one PA (the search measures borderline values twice) are combined:
    dips averaged, limit / sqrt(2)."""
    by = {}
    for s in segments:
        sc = s.get('ozn')
        if sc and not s.get('partial') and s.get('pa') is not None:
            by.setdefault(round(s['pa'], 6), []).append((s['idx'], sc))
    if len(by) < 2:
        return {'pa': None, 'idx': None, 'why': 'not enough PA values'}
    pts = []
    for pa in sorted(by):
        rows = by[pa]
        dip = sum(r[1]['dip'] for r in rows) / len(rows)
        lim = sum(r[1]['limit'] for r in rows) / len(rows)
        if len(rows) > 1:
            lim /= math.sqrt(2.)
        mis = sum(r[1]['mismatch'] for r in rows) / len(rows)
        pts.append((pa, rows[-1][0], dip, lim, mis))
    zero = [p for p in pts if p[0] == 0.]
    floor = ozn_floor(zero[0][2]) if zero else 0.
    first_bad = None
    for i, (pa, idx, dip, lim, _) in enumerate(pts):
        if max(0., -dip - max(lim, floor)) > 0.:
            first_bad = i
            break
    cross = ozn_zero([(p[0], p[4]) for p in pts])
    extra = (', square-wave crossing %.4f' % cross) if cross else ''
    if first_bad is None:
        pa, idx = pts[-1][0], pts[-1][1]
        return {'pa': pa, 'idx': idx, 'cross': cross,
                'why': 'no dip up to %.3f: raise the PA range%s'
                       % (pa, extra)}
    if first_bad == 0:
        return {'pa': None, 'idx': None, 'cross': cross,
                'why': 'already dips at the lowest PA (%.3f)%s'
                       % (pts[0][0], extra)}
    pa, idx = pts[first_bad - 1][0], pts[first_bad - 1][1]
    b = pts[first_bad]
    return {'pa': pa, 'idx': idx, 'cross': cross,
            'why': 'last value without a dip below the line (%.3f dips '
                   '%.0f%% > %.0f%%, floor from PA 0 %.0f%%)%s'
                   % (b[0], -b[2] * 100, max(b[3], floor) * 100,
                      floor * 100, extra)}
