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


def _windows(markers, kind, tmax):
    """[(t_edge, pre_s, post_s)] for every usable edge.
    Works for back-to-back legs (slow, fast, slow, ...) and for legs with
    rests between them: the window before an edge reaches back no further
    than the previous flow change, the window after it stops before the
    next flow change (minus GUARD), so neighbouring edges never leak in."""
    legs = [m for m in markers if m.get('name') in ('slow', 'fast')]
    cuts = sorted({round(m['t0'], 6) for m in legs} |
                  {round(m['t1'], 6) for m in legs} | {0.0})
    out = []
    for m in legs:
        if m.get('name') != 'fast':
            continue
        te = m['t0'] if kind == 'rise' else m['t1']
        before = [c for c in cuts if c < te - 1e-6]
        after = [c for c in cuts if c > te + 1e-6]
        tp = before[-1] if before else te - PRE - GUARD
        tn = after[0] if after else tmax
        pre = max(MIN_PRE, min(PRE, te - tp - GUARD))
        post = max(MIN_POST, min(2.0, tn - te - GUARD))
        out.append((te, pre, post))
    return out


def edge_summary(t, f, markers, kind='rise', npts=POINTS):
    """Return dict or None when there is not enough data."""
    wins = _windows(markers, kind, t[-1] if t else 0.0)
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
           'rise': None, 'overshoot': None}
    t10 = t90 = None
    for i in range(1, npts):
        if grid[i] < -0.03:         # edge time is not exact; allow a little early
            continue
        if t10 is None and y[i] >= 0.1 > y[i - 1]:
            t10 = grid[i - 1] + (grid[i] - grid[i - 1]) * (0.1 - y[i - 1]) / (y[i] - y[i - 1])
        if t90 is None and y[i] >= 0.9 > y[i - 1]:
            t90 = grid[i - 1] + (grid[i] - grid[i - 1]) * (0.9 - y[i - 1]) / (y[i] - y[i - 1])
    if t10 is not None and t90 is not None:
        res['rise'] = round(t90 - t10, 4)
    res['overshoot'] = round(max(0.0, max(v for g, v in zip(grid, y) if g >= 0) - 1.0), 4)
    return res
