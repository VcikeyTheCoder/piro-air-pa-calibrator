"""Tap analysis: where the nozzle really touched, and was it a clean tap.

Plain Python (no NumPy): a tap has a few hundred samples.

A tap, seen by the Eddy (Hz of extra pressure over the tare):

    approach     compression  dwell  decompression   departure
    ____________/|‾‾‾‾‾‾‾‾‾‾‾‾\\________________
                 ^ contact    ^ trigger (stop)   ^ contact breaks

The nozzle goes down until the MCU trigger fires and the steppers stop. It
waits a moment (dwell), then lifts slowly (pullback, about 1 sample per
micron). While lifting, the pressure falls in a straight line with Z until
the nozzle leaves the bed, then stays flat. Z of contact = where those two
lines meet (decompression x departure), the method of Kalico's load cell
probe. The lift is slow and the stop is exact, so this point does not depend
on the trigger level or the filter delay.

The other lines tell whether the tap was clean. Ooze between nozzle and bed
shows up as pressure before contact, a pressure that sinks during the dwell
(the plastic squeezes out), a soft decompression or a baseline that does
not come back."""
import bisect
import math

# validation codes (a tap with any of these is not used)
TOO_FEW_SAMPLES = 'TOO_FEW_SAMPLES'
INSUFFICIENT_Z_TRAVEL = 'INSUFFICIENT_Z_TRAVEL'
TAP_SHAPE_INVALID = 'TAP_SHAPE_INVALID'
TAP_CHRONOLOGY = 'TAP_CHRONOLOGY'
BREAK_CONTACT_TOO_EARLY = 'BREAK_CONTACT_TOO_EARLY'
BREAK_CONTACT_TOO_LATE = 'BREAK_CONTACT_TOO_LATE'

CODE_TEXT = {
    TOO_FEW_SAMPLES: "too few Eddy samples during the pullback",
    INSUFFICIENT_Z_TRAVEL: "the pullback moved less than planned",
    TAP_SHAPE_INVALID: "no clear press and release in the signal",
    TAP_CHRONOLOGY: "events out of order (contact after the stop)",
    BREAK_CONTACT_TOO_EARLY: "contact broke right away (pullback too fast "
                             "or almost no press)",
    BREAK_CONTACT_TOO_LATE: "contact had not broken by the end of the "
                            "pullback (pullback_distance too short)",
}

# quality features and the value at which each one scores 0
FEATURES = ('approach', 'departure', 'baseline', 'dwell', 'stiffness',
            'residual')
DEFAULT_LIMITS = {'approach': 0.15, 'departure': 0.30, 'baseline': 0.30,
                  'dwell': 0.35, 'stiffness': 0.60, 'residual': 0.15}
FEATURE_TEXT = {
    'approach': "pressure before contact (ooze string or drag)",
    'departure': "pressure left after lift-off",
    'baseline': "baseline moved during the tap",
    'dwell': "pressure sank while pressed (ooze squeezing out)",
    'stiffness': "release softer or harder than the press (ooze)",
    'residual': "noisy release",
}


# -- straight-line pieces ---------------------------------------------------
def _solve3(m, r):
    """3x3 linear solve (Gauss with pivoting). None if singular."""
    a = [list(m[i]) + [r[i]] for i in range(3)]
    for c in range(3):
        p = max(range(c, 3), key=lambda i: abs(a[i][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for i in range(3):
            if i != c:
                f = a[i][c] / a[c][c]
                for j in range(c, 4):
                    a[i][j] -= f * a[c][j]
    return [a[i][3] / a[i][i] for i in range(3)]


class _Prefix:
    def __init__(self, xs, ys):
        self.xs = xs
        self.n = len(xs)
        S = self.S = [[0.] * (self.n + 1) for _ in range(5)]
        for i, (x, y) in enumerate(zip(xs, ys)):
            for k, v in enumerate((x, x * x, y, x * y, y * y)):
                S[k][i + 1] = S[k][i] + v

    def part(self, i0, i1):
        return [S[i1] - S[i0] for S in self.S]


def hinge_at(pre, b):
    """Best continuous two-slope line with its bend at x=b:
    y = c + sl*(x-b) for x<b, c + sr*(x-b) for x>=b.
    -> (sse, c, sl, sr, n_left, n_right)"""
    k = bisect.bisect_left(pre.xs, b)
    n = pre.n
    L = pre.part(0, k)
    R = pre.part(k, n)
    nl, nr = k, n - k
    # sums of u = x-b over each side
    uL = L[0] - nl * b
    uL2 = L[1] - 2 * b * L[0] + nl * b * b
    uLy = L[3] - b * L[2]
    uR = R[0] - nr * b
    uR2 = R[1] - 2 * b * R[0] + nr * b * b
    uRy = R[3] - b * R[2]
    sy, sy2 = L[2] + R[2], L[4] + R[4]
    m = [[n, uL, uR], [uL, uL2, 0.], [uR, 0., uR2]]
    rhs = [sy, uLy, uRy]
    sol = _solve3(m, rhs)
    if sol is None:
        return None
    sse = sy2 - sum(c * r for c, r in zip(sol, rhs))
    return (max(sse, 0.), sol[0], sol[1], sol[2], nl, nr)


def hinge_fit(xs, ys, min_side=3):
    """Two straight lines meeting at one point, fitted by least squares.
    xs must be sorted. -> dict(b, c, sl, sr, nl, nr, sse) or None."""
    n = len(xs)
    if n < 2 * min_side + 1:
        return None
    pre = _Prefix(xs, ys)
    best = None
    for i in range(min_side, n - min_side + 1):
        b = xs[i - 1] if i == n else 0.5 * (xs[i - 1] + xs[min(i, n - 1)])
        r = hinge_at(pre, b)
        if r is not None and (best is None or r[0] < best[1][0]):
            best = (b, r, i)
    if best is None:
        return None
    # refine between the neighbouring samples (golden section)
    i = best[2]
    lo, hi = xs[max(i - 2, 0)], xs[min(i + 1, n - 1)]
    g = (math.sqrt(5.) - 1.) / 2.

    def f(b):
        r = hinge_at(pre, b)
        return r[0] if r is not None else float('inf')
    a, d = lo, hi
    x1, x2 = d - g * (d - a), a + g * (d - a)
    f1, f2 = f(x1), f(x2)
    for _ in range(40):
        if f1 < f2:
            d, x2, f2 = x2, x1, f1
            x1 = d - g * (d - a)
            f1 = f(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + g * (d - a)
            f2 = f(x2)
    b = 0.5 * (a + d)
    r = hinge_at(pre, b)
    if r is None or r[0] > best[1][0]:
        b, r = best[0], best[1]
    sse, c, sl, sr, nl, nr = r
    return {'b': b, 'c': c, 'sl': sl, 'sr': sr, 'nl': nl, 'nr': nr,
            'sse': sse, 'n': n}


def _mean(v):
    return sum(v) / len(v) if v else 0.


# -- one tap ------------------------------------------------------------------
def analyze(samples, z_of, ev, trigger_hz, pullback_distance, limits=None,
            sign=0):
    """samples: [(t, hz_over_tare), ...] sorted by t.
    z_of(t): toolhead Z at time t (from stepper history).
    ev: times 'move' (probe move start), 'trigger', 'pull0', 'pull1'.
    sign: +1 / -1 if known (more pressure = higher / lower Hz), 0 = find it.
    -> dict with z (contact), ok, codes, quality, features, lines."""
    lim = dict(DEFAULT_LIMITS)
    lim.update(limits or {})
    t_trig, p0, p1 = ev['trigger'], ev['pull0'], ev['pull1']
    out = {'ok': False, 'codes': [], 'quality': 0, 'features': {},
           'z': None, 'z_trigger': None, 'lines': {}, 'sign': sign}
    if not samples:
        out['codes'].append(TOO_FEW_SAMPLES)
        return out
    if not sign:
        near = [v for t, v in samples if t_trig - 0.02 <= t <= p0 + 0.01]
        if not near:
            near = [v for t, v in samples]
        sign = -1 if _mean(near) < 0 else 1
        out['sign'] = sign
    data = [(t, sign * v) for t, v in samples]
    peak_win = [v for t, v in data if t_trig - 0.05 <= t <= p0 + 0.02]
    peak = max(peak_win) if peak_win else max(v for t, v in data)
    out['peak'] = round(peak, 2)
    z_trig = z_of(t_trig)
    out['z_trigger'] = z_trig
    codes = out['codes']

    # pullback: pressure against Z
    pull = [(z_of(t), v, t) for t, v in data if p0 <= t <= p1 + 0.01]
    pull.sort()
    travel = z_of(p1) - z_of(p0)
    out['travel'] = round(travel, 5)
    if len(pull) < 10:
        codes.append(TOO_FEW_SAMPLES)
        return out
    if travel < 0.8 * pullback_distance:
        codes.append(INSUFFICIENT_Z_TRAVEL)
    if peak < 0.5 * trigger_hz:
        codes.append(TAP_SHAPE_INVALID)
    fit = hinge_fit([p[0] for p in pull], [p[1] for p in pull], 3)
    if fit is None:
        codes.append(TAP_SHAPE_INVALID)
        return out
    zb = fit['b']
    out['z'] = zb
    # pressure must fall clearly while lifting through contact
    steep = peak / max(pullback_distance, 0.01)
    if fit['sl'] > -0.2 * steep or fit['sl'] >= fit['sr']:
        codes.append(TAP_SHAPE_INVALID)
    z0 = pull[0][0]
    if fit['nl'] < 3 or zb - z0 < 0.03 * travel:
        codes.append(BREAK_CONTACT_TOO_EARLY)
    if fit['nr'] < 3 or zb > z0 + 0.85 * travel:
        codes.append(BREAK_CONTACT_TOO_LATE)
    if zb < z_trig - 0.02:
        codes.append(TAP_CHRONOLOGY)
    dep_level = fit['c']
    resid = math.sqrt(fit['sse'] / fit['n'])

    # descent: approach and compression against time
    down = [(t, v) for t, v in data if ev.get('move', t_trig - 1.) <= t <= t_trig]
    comp = None
    app_level = None
    if len(down) >= 9:
        dfit = hinge_fit([d[0] for d in down], [d[1] for d in down], 3)
        if dfit is not None and dfit['sr'] > 0:
            comp = dfit
            app_level = dfit['c']
            if dfit['b'] > t_trig + 0.005:
                codes.append(TAP_CHRONOLOGY)
    # dwell: did the pressure sink while pressed?
    dwell = [v for t, v in data if t_trig <= t <= p0]
    dwell_drop = 0.
    if len(dwell) >= 4:
        k = max(2, len(dwell) // 4)
        dwell_drop = (_mean(dwell[:k]) - _mean(dwell[-k:])) / max(peak, 1e-9)

    # quality: each feature 0 (clean) .. its limit (score 0)
    pk = max(peak, 1e-9)
    feats = {'departure': abs(dep_level) / pk, 'dwell': max(0., dwell_drop),
             'residual': resid / pk}
    if app_level is not None:
        feats['approach'] = abs(app_level) / pk
        feats['baseline'] = abs(dep_level - app_level) / pk
    speed = ev.get('speed') or 0.
    if comp is not None and speed > 0:
        k_comp = comp['sr'] / speed               # Hz per mm pressing
        k_rel = -fit['sl']                        # Hz per mm releasing
        if k_comp > 0:
            feats['stiffness'] = abs(1. - k_rel / k_comp)
            out['stiffness'] = {'press': round(k_comp, 1),
                                'release': round(k_rel, 1)}
    scores = {k: max(0., 1. - v / lim[k]) for k, v in feats.items()
              if lim.get(k)}
    if scores:
        q = 100. * (0.5 * min(scores.values())
                    + 0.5 * _mean(list(scores.values())))
    else:
        q = 0.
    worst = min(scores, key=scores.get) if scores else None
    out['features'] = {k: round(v, 4) for k, v in feats.items()}
    out['quality'] = int(round(q)) if not codes else 0
    out['worst'] = worst
    out['ok'] = not codes
    out['press_depth'] = round(zb - z_trig, 5)
    out['noise'] = round(resid, 2)
    # lines for the tap viewer: (t, Hz) end points, in the sensor's sign
    s = sign

    def tz(z):
        best = min(pull, key=lambda p: abs(p[0] - z))
        return best[2]
    lines = {'decompression': [[tz(pull[0][0]), s * (fit['c'] + fit['sl'] *
                                                     (pull[0][0] - zb))],
                               [tz(zb), s * fit['c']]],
             'departure': [[tz(zb), s * fit['c']],
                           [tz(pull[-1][0]), s * (fit['c'] + fit['sr'] *
                                                  (pull[-1][0] - zb))]]}
    if comp is not None:
        t0, t1 = down[0][0], down[-1][0]
        lines['approach'] = [[t0, s * (comp['c'] + comp['sl'] *
                                       (t0 - comp['b']))],
                             [comp['b'], s * comp['c']]]
        lines['compression'] = [[comp['b'], s * comp['c']],
                                [t1, s * (comp['c'] + comp['sr'] *
                                          (t1 - comp['b']))]]
        out['t_contact'] = comp['b']
    out['t_break'] = tz(zb)
    out['lines'] = {k: [[round(a, 5), round(b, 2)] for a, b in v]
                    for k, v in lines.items()}
    return out


def explain(res):
    """One line: why a tap was not used, or how good it was."""
    if res.get('codes'):
        return "; ".join(CODE_TEXT.get(c, c) for c in res['codes'])
    w = res.get('worst')
    if w and res.get('quality', 100) < 80:
        return "quality %d%%: %s" % (res['quality'], FEATURE_TEXT.get(w, w))
    return "quality %d%%" % res.get('quality', 0)


def suggest_limits(feature_rows, floor=None):
    """From clean taps: each limit = mean + 4 SD (at least the floor, and at
    least half the default, so a perfect calibration is not too strict)."""
    out = {}
    for k in FEATURES:
        v = [r[k] for r in feature_rows if k in r]
        if len(v) < 3:
            continue
        m = _mean(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
        lo = (floor or {}).get(k, 0.5 * DEFAULT_LIMITS[k])
        out[k] = round(max(lo, m + 4 * sd), 4)
    return out
