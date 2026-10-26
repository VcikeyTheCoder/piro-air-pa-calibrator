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


def _edges(segments, kind):
    out = []
    for s in segments:
        e = (s.get('edge') or {}).get(kind)
        if e and not s.get('partial') and s.get('pa') is not None:
            out.append((s['pa'], s['idx'], e))
    out.sort(key=lambda x: x[0])
    return out


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


# ---------------------------------------------------------------------
# PIRO-EDGE: one score per PA value, one answer per run.
#
# Per PA value (edge_score), for every pulse except the first:
#   * a straight baseline from the slow level before the pulse to the slow
#     level after it (cancels slow load cell drift; idea from OznLab sensor,
#     github.com/ozancs/oznlab-sensor)
#   * slow-down edge: (force - baseline) / step after the slow-down.
#     Too much PA pushes the force below the line: the DIP (deepest 40 ms
#     mean of the median curve, negative = below the line)
#   * speed-up edge: (force - fast level) / step after the speed-up.
#     Too much PA pushes the force above the fast level: the OVERSHOOT
#   * median of the pulses (one bad pulse cannot move it), its standard
#     error from the pulse-to-pulse spread, and a noise limit
#
# Per run (piro_pick):
#   1. PA values measured more than once are pooled (more pulses)
#   2. slow-down edge: going up in PA, the first value whose dip is beyond
#      its limit (and the next one too) is "too much"; a weighted straight
#      line through the dips just above that point gives the exact PA where
#      the dip reaches 0. This is the answer.
#   3. speed-up edge: the same on the overshoot, as an independent check.
#      If both edges agree they are averaged (weighted by how sure each one
#      is); if they disagree the slow-down edge is kept and a warning shown
#   4. PA +- uncertainty and a grade (good / fair / poor) with reasons.
WIN = 0.04              # s, moving mean used for the dip / overshoot
SE_FLOOR = 0.008        # smallest standard error trusted for one point
SLOPE_PRIOR = 10.0      # dip change per 1.0 PA beyond the answer (both runs)


def _movmean(c, n, fn):
    if len(c) < n:
        return None
    acc = [0.]
    for x in c:
        acc.append(acc[-1] + x)
    return fn((acc[k + n] - acc[k]) / n for k in range(len(c) - n + 1))


def _median(v):
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def _med_curve(curves):
    L = min(len(c) for c in curves)
    return [_median([c[k] for c in curves]) for k in range(L)]


def _se(vals):
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1) / n)


def edge_score(t, f, markers, smooth=0.04, skip_first=True):
    """One PA value -> dict or None (see the block comment above)."""
    if len(t) < 50:
        return None
    h = min(max(smooth or 0.04, 0.01), 0.1) / 2.
    fasts = sorted((m for m in markers if m.get('name') == 'fast'),
                   key=lambda m: m['t0'])
    slows = sorted((m for m in markers if m.get('name') == 'slow'),
                   key=lambda m: m['t0'])
    if skip_first and len(fasts) > 1:
        fasts = fasts[1:]
    sps = (len(t) - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 320.
    n_win = max(4, int(round(WIN * sps)))
    fall, rise, amps, sig, drift = [], [], [], [], []
    for m in fasts:
        tu, td = m['t0'], m['t1']
        nxt = [x for x in slows if x['t0'] >= td - 1e-6]
        t_end = nxt[0]['t1'] if nxt else t[-1]
        t_slow, t_fast = t_end - td, td - tu
        if t_slow < 0.15 or t_fast < 0.1:
            continue
        pre_a, pre_b = tu - min(0.30, 0.3 * t_slow + 0.03), tu - 0.03
        pre = _mean(t, f, pre_a, pre_b)
        late_u = _mean(t, f, tu + max(h + 0.1, 0.58 * t_fast), td - h - 0.02)
        ld_a, ld_b = td + 0.5 * t_slow, td + t_slow - h - 0.05
        late_d = _mean(t, f, ld_a, ld_b)
        if None in (pre, late_u, late_d):
            continue
        amp = late_u - late_d
        sg = _detrended_sigma(t, f, ld_a, ld_b)
        if abs(amp) < 1e-9 or (sg is not None and abs(amp) < 5 * sg):
            continue                     # no real pressure step here
        tp, ta = 0.5 * (pre_a + pre_b), 0.5 * (ld_a + ld_b)

        def base(x, pre=pre, late_d=late_d, tp=tp, ta=ta):
            return pre + (late_d - pre) * (x - tp) / (ta - tp)
        a, b = td + h + 0.005, td + h + min(0.30, t_slow - h - 0.06)
        lo, hi = bisect.bisect_left(t, a), bisect.bisect_right(t, b)
        fall.append([(f[i] - base(t[i])) / amp for i in range(lo, hi)])
        a, b = tu + h + 0.005, tu + h + min(0.30, t_fast - h - 0.03)
        lo, hi = bisect.bisect_left(t, a), bisect.bisect_right(t, b)
        rise.append([(f[i] - late_u) / amp for i in range(lo, hi)])
        amps.append(amp)
        drift.append((late_d - pre) / amp)
        if sg is not None:
            sig.append(sg)
    if not amps or min(len(c) for c in fall) < n_win + 2:
        return None
    rise_ok = min(len(c) for c in rise) >= n_win + 2
    fc = _med_curve(fall)
    rc = _med_curve(rise) if rise_ok else []
    p_dip = [_movmean(c, n_win, min) for c in fall]
    p_over = [_movmean(c, n_win, max) for c in rise] if rise_ok else []
    amp_m = _median(amps)
    sig_m = _median(sig) if sig else 0.
    lim = max(0.03, 4. * sig_m / math.sqrt(n_win * len(amps)) / abs(amp_m))
    a_abs = [abs(x) for x in amps]
    stp = max(1, int(round(0.005 * sps)))
    out = {'dip': round(_movmean(fc, n_win, min), 4),
           'dip_se': _r(_se(p_dip)), 'limit': round(lim, 4),
           'over': _r(_movmean(rc, n_win, max)) if rise_ok else None,
           'over_se': _r(_se(p_over)) if rise_ok else None,
           'amp': round(amp_m, 3), 'sigma': round(sig_m, 3),
           'snr': round(abs(amp_m) / sig_m, 1) if sig_m > 0 else None,
           'n': len(amps), 'n_win': n_win,
           'spread': round(max(a_abs) / max(min(a_abs), 1e-9), 3),
           'drift': round(_median(drift), 4),
           'pulse_dips': [round(x, 4) for x in p_dip],
           'fall_curve': [round(fc[k], 4) for k in range(0, len(fc), stp)],
           'rise_curve': [round(rc[k], 4) for k in range(0, len(rc), stp)],
           'dt': round(stp / sps, 5), 'start': round(h + 0.005, 4)}
    return out


def _r(v, d=4):
    return None if v is None else round(v, d)


def floor_from_zero(dip0):
    """PA 0 cannot undershoot: a dip seen there is this printer's noise
    (rule from OznLab sensor)."""
    return 1.3 * max(0., -dip0) + 0.01 if dip0 is not None and dip0 < 0 \
        else 0.


def pool(segments):
    """Group the per-PA scores by PA value; repeats are combined as if they
    were one longer measurement. -> sorted list of point dicts."""
    by = {}
    for s in segments:
        sc = s.get('piro')
        if sc and not s.get('partial') and s.get('pa') is not None:
            by.setdefault(round(s['pa'], 6), []).append((s['idx'], sc))
    pts = []
    for pa in sorted(by):
        rows = by[pa]
        n = sum(r[1]['n'] for r in rows)
        w = lambda key: sum(r[1][key] * r[1]['n'] for r in rows
                            if r[1].get(key) is not None) / max(1, sum(
                                r[1]['n'] for r in rows
                                if r[1].get(key) is not None))
        dips = [d for r in rows for d in r[1].get('pulse_dips') or []]
        k = len(rows)
        se = lambda key: (None if any(r[1].get(key) is None for r in rows)
                          else math.sqrt(sum(r[1][key] ** 2 for r in rows))
                          / k)
        pts.append({'pa': pa, 'idx': rows[-1][0], 'idxs': [r[0] for r in rows],
                    'times': k, 'n': n,
                    'dip': round(w('dip'), 4), 'dip_se': _r(se('dip_se')),
                    'limit': round(max(0.02, w('limit') / math.sqrt(k)), 4),
                    'over': _r(w('over')) if all(r[1].get('over') is not None
                                                 for r in rows) else None,
                    'over_se': _r(se('over_se')),
                    'snr': _r(w('snr'), 1) if all(r[1].get('snr')
                                                  for r in rows) else None,
                    'spread': max(r[1]['spread'] for r in rows),
                    'drift': round(w('drift'), 4),
                    'pulse_dips': dips})
    return pts


def _line(xs, ys, ws):
    """Weighted least squares y = a + b x -> (a, b, var_a, var_b, cov, chi2)."""
    S = sum(ws)
    Sx = sum(w * x for w, x in zip(ws, xs))
    Sy = sum(w * y for w, y in zip(ws, ys))
    Sxx = sum(w * x * x for w, x in zip(ws, xs))
    Sxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))
    det = S * Sxx - Sx * Sx
    if det <= 1e-18:
        return None
    a = (Sy * Sxx - Sx * Sxy) / det
    b = (S * Sxy - Sx * Sy) / det
    chi2 = sum(w * (y - a - b * x) ** 2 for w, x, y in zip(ws, xs, ys))
    return a, b, Sxx / det, S / det, -Sx / det, chi2


def edge_estimate(pts, val, se_key, sign, floor=0.):
    """One edge -> where "too much PA" starts. `sign` turns the value into
    an excess that grows with PA (dip: -1, overshoot: +1). Returns a dict
    with k (the answer for this edge), se, the bracket and the line used."""
    rows = [p for p in pts if p.get(val) is not None]
    if len(rows) < 2:
        return {'k': None, 'status': 'few', 'why': 'Not enough PA values'}
    ex = [sign * p[val] for p in rows]
    lim = [max(p['limit'], floor) for p in rows]
    bad = [e > l for e, l in zip(ex, lim)]
    first = None
    for i, b in enumerate(bad):
        if b and (i + 1 == len(bad) or bad[i + 1]):
            first = i
            break
    if first is None:
        return {'k': None, 'status': 'no_dip', 'k_ok': rows[-1]['pa'],
                'why': 'No dip up to PA %.3f' % rows[-1]['pa']}
    if first == 0:
        return {'k': None, 'status': 'low', 'k_bad': rows[0]['pa'],
                'why': 'Dip at the lowest PA %.3f' % rows[0]['pa']}
    k_ok, k_bad = rows[first - 1]['pa'], rows[first]['pa']
    gap = k_bad - k_ok
    clean = [e for e, b, p in zip(ex, bad, rows) if not b and p['pa'] > 0]
    target = max(0., _median(clean)) if (sign > 0 and clean) else 0.
    # the line: the last clean value and the first (up to 3) values that
    # are too high, all close to the corner. Values far above it are left
    # out: there the curve bends and a straight line would miss the corner.
    reach = k_bad + max(0.012, 2. * gap)
    use = [i for i in range(first, len(rows)) if rows[i]['pa'] <= reach][:3]
    if rows[first - 1]['pa'] > 0:
        use = [first - 1] + use
    res = {'k_ok': k_ok, 'k_bad': k_bad, 'target': round(target, 4),
           'used': [rows[i]['pa'] for i in use], 'status': 'ok'}
    fit = None
    if len(use) >= 2:
        xs = [rows[i]['pa'] for i in use]
        ys = [ex[i] for i in use]
        ws = [1. / max(rows[i].get(se_key) or 0.02, SE_FLOOR) ** 2
              for i in use]
        fit = _line(xs, ys, ws)
        if fit and fit[1] <= 0:
            fit = None
    if fit:
        a, b, va, vb, cab, chi2 = fit
        k = (target - a) / b
        var = (va + k * k * vb + 2 * k * cab) / (b * b)
        dof = len(use) - 2
        if dof > 0 and chi2 / dof > 1:
            var *= chi2 / dof
        se = math.sqrt(max(var, 0.))
        res['line'] = {'a': round(sign * a, 5), 'b': round(sign * b, 4)}
        res['method'] = 'line through %d values' % len(use)
    else:
        e0, e1 = ex[first - 1], ex[first]
        k = k_ok + gap * ((target - e0) / (e1 - e0)) if e1 > e0 \
            else 0.5 * (k_ok + k_bad)
        se = gap / math.sqrt(12.)
        res['method'] = 'between %.4f and %.4f' % (k_ok, k_bad)
    lo = max(0., k_ok - gap)
    if not lo <= k <= k_bad:
        k = min(max(k, lo), k_bad)
        se = max(se, gap / math.sqrt(12.))
    se = max(se, 0.15 * gap, 0.0002)     # cannot be surer than the gap
    res.update(k=round(k, 5), se=round(se, 5))
    return res


def predict(pts, floor=0.):
    """Where the answer probably is, from the values that dip so far: a
    line through the lowest two dipping values, or one dip and the typical
    slope. None when nothing dips yet."""
    dips = [p for p in pts if p['pa'] > 0
            and -p['dip'] > max(p['limit'], floor)]
    if not dips:
        return None
    dips.sort(key=lambda p: p['pa'])
    if len(dips) >= 2:
        p1, p2 = dips[0], dips[1]
        s = (-p2['dip'] + p1['dip']) / (p2['pa'] - p1['pa'])
        if 3. <= s <= 40.:
            return round(p1['pa'] - (-p1['dip']) / s, 5)
    p = dips[0]
    return round(p['pa'] - (-p['dip']) / SLOPE_PRIOR, 5)


def klipper_k(k_art, smooth):
    """Artificial PA (charge spread evenly over the smoothing window while
    the speed jumps to the middle flow) needs a slightly larger K than
    Klipper's own smoothed PA, which smooths the speed step and the PA term
    with the same window. For a first-order melt lag tau the artificial
    optimum is K_art = tau * x * coth(x), x = (smooth / 2) / tau, while
    Klipper's is K = tau. Returns the Klipper K for a measured K_art."""
    if not k_art or k_art <= 0 or not smooth:
        return k_art
    h, k = smooth / 2., k_art
    for _ in range(40):
        x = h / k
        k = k_art / (x / math.tanh(x))
    return k


def piro_pick(segments, smooth=None):
    """`smooth`: the charge window when the run used artificial PA (its
    answer is then converted to Klipper's K, see klipper_k)."""
    """The run's answer. Dict with pa, unc, grade, why, warnings, edges and
    the points used (for the History graphs)."""
    pts = pool(segments)
    out = {'method': 'PIRO-EDGE', 'pa': None, 'unc': None, 'grade': None,
           'points': [{k: p[k] for k in ('pa', 'idx', 'times', 'n', 'dip',
                                         'dip_se', 'limit', 'over',
                                         'over_se', 'snr', 'drift')}
                      for p in pts],
           'warnings': []}
    if len(pts) < 2:
        out['why'] = 'Not enough PA values'
        return out
    zero = [p for p in pts if p['pa'] == 0.]
    floor = floor_from_zero(zero[0]['dip']) if zero else 0.
    out['floor'] = round(floor, 4)
    fe = edge_estimate(pts, 'dip', 'dip_se', -1, floor)
    re_ = edge_estimate(pts, 'over', 'over_se', 1)
    out['fall'], out['rise'] = fe, re_
    out['prediction'] = predict(pts, floor)
    W = out['warnings']
    if fe.get('k') is None:
        out['why'] = {'no_dip': 'No dip up to PA %.3f'
                                % fe.get('k_ok', 0),
                      'low': 'Dip at the lowest PA',
                      }.get(fe.get('status'), fe.get('why', ''))
        return out
    k, se = fe['k'], fe['se']
    # the speed-up edge is a check, not part of the number: on real runs
    # its overshoot starts at a higher PA than the slow-down dip, so
    # averaging the two would push the answer too high
    if re_.get('k') is not None:
        d = re_['k'] - k
        sc = math.sqrt(se ** 2 + re_['se'] ** 2)
        out['check'] = 'agrees' if abs(d) <= 2. * sc else \
            'higher' if d > 0 else 'lower'
        if out['check'] == 'lower':
            se = max(se, -d / 2.)
            W.append('Rise edge %.4f below the fall edge. Check pulse settings'
                     % re_['k'])
    else:
        out['check'] = 'none'
    near = [p for p in pts if fe['k_ok'] <= p['pa'] <= fe['k_bad'] + 0.02]
    snr = [p['snr'] for p in near if p.get('snr')]
    if snr and min(snr) < 15:
        W.append('Weak signal (SNR %.0f). Raise fast_flow or cycles'
                 % min(snr))
    dr = [abs(p['drift']) for p in near]
    if dr and max(dr) > 0.15:
        W.append('Baseline drift %.0f%% of step. Check load cell mounting'
                 % (100 * max(dr)))
    sp = [p for p in pts if p.get('spread', 1) > 1.35 and p['pa'] > 0] \
        if False else []
    if len(fe.get('used', [])) < 2:
        W.append('One value above the edge; fit not verified')
    if fe['k_bad'] - fe['k_ok'] > max(0.02, 0.5 * k):
        W.append('Gap between clean %.3f and dip %.3f'
                 % (fe['k_ok'], fe['k_bad']))
    lvl = 0 if se <= max(0.0015, 0.04 * k) else \
        1 if se <= max(0.003, 0.08 * k) else 2
    lvl = min(2, lvl + len(W))
    if smooth and k > 0:
        kk = klipper_k(k, smooth)
        out['measured'] = round(k, 4)
        out['smooth'] = smooth
        se *= kk / k
        k = kk
    out.update(pa=round(k, 4), unc=round(se, 4),
               grade=('good', 'fair', 'poor')[lvl])
    out['why'] = ('Edge between %.4f and %.4f; %s gives %.4f%s'
                  % (fe['k_ok'], fe['k_bad'], fe['method'], fe['k'],
                     (', rise edge %.4f' % re_['k'])
                     if re_.get('k') is not None else ''))
    return out
