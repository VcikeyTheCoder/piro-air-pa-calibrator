"""Butterworth filters as second-order sections, in plain Python.

Klipper's trigger_analog.DigitalFilter needs SciPy for anything but one
pre-computed filter, and SciPy is often missing on a Pi. These are the same
filters (bilinear transform with pre-warping, like scipy.signal.butter), so
the MCU's SOS filter can run them. Each section has a DC (lowpass) or
high-frequency (highpass) gain of exactly 1, which keeps the fixed-point
values on the MCU small.

sosfilt() runs the same sections on the host: the calibrations use it to see
the signal exactly as the MCU's trigger sees it."""
import cmath
import math


def _bilinear2(b, a, k):
    """s-domain (b2 s^2 + b1 s + b0)/(a2 s^2 + a1 s + a0) -> z-domain row."""
    b2, b1, b0 = b
    a2, a1, a0 = a
    k2 = k * k
    B = (b2 * k2 + b1 * k + b0, 2. * (b0 - b2 * k2), b2 * k2 - b1 * k + b0)
    A = (a2 * k2 + a1 * k + a0, 2. * (a0 - a2 * k2), a2 * k2 - a1 * k + a0)
    return [B[0] / A[0], B[1] / A[0], B[2] / A[0], 1., A[1] / A[0], A[2] / A[0]]


def _bilinear1(b, a, k):
    """(b1 s + b0)/(a1 s + a0) -> z-domain row (one pole, one zero)."""
    b1, b0 = b
    a1, a0 = a
    A0 = a1 * k + a0
    return [(b1 * k + b0) / A0, (b0 - b1 * k) / A0, 0., 1., (a0 - a1 * k) / A0,
            0.]


def butter(order, freq, sps, btype='lowpass'):
    """Rows [b0, b1, b2, 1, a1, a2] of a Butterworth filter."""
    order = int(order)
    if order < 1 or order > 8:
        raise ValueError("filter order must be 1 to 8")
    if not 0. < freq < sps / 2.:
        raise ValueError("filter frequency %.3g Hz must be between 0 and half "
                         "the sample rate (%.3g Hz)" % (freq, sps / 2.))
    if btype not in ('lowpass', 'highpass'):
        raise ValueError("btype must be lowpass or highpass")
    k = 2. * sps
    wc = k * math.tan(math.pi * freq / sps)        # pre-warped corner
    low = btype == 'lowpass'
    rows = []
    for i in range(order // 2):
        p = cmath.exp(1j * math.pi * (2 * i + order + 1) / (2. * order))
        a = (1., -2. * p.real * wc, wc * wc)
        b = (0., 0., wc * wc) if low else (1., 0., 0.)
        rows.append(_bilinear2(b, a, k))
    if order % 2:
        rows.append(_bilinear1((0., wc) if low else (1., 0.), (1., wc), k))
    return rows


class Design:
    """Stands in for trigger_analog.DigitalFilter (the parts MCU_SosFilter
    uses), without SciPy."""
    def __init__(self, sps):
        self.sps = float(sps)
        self.rows = []

    def add_lowpass(self, freq, order=2):
        self.rows.extend(butter(order, freq, self.sps, 'lowpass'))
        return self

    def add_highpass(self, freq, order=1):
        self.rows.extend(butter(order, freq, self.sps, 'highpass'))
        return self

    def get_filter_sections(self):
        return self.rows

    def get_initial_state(self):
        return [[0., 0.] for _ in self.rows]

    def get_size(self):
        return len(self.rows)

    def describe(self):
        return "%d section(s) at %g samples/s" % (len(self.rows), self.sps)


def sosfilt(rows, xs):
    """Run the sections over xs (direct form II transposed, like the MCU)."""
    out = list(xs)
    for b0, b1, b2, _, a1, a2 in rows:
        z1 = z2 = 0.
        for i, x in enumerate(out):
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[i] = y
        # next section takes this one's output
    return out


def group_delay(rows, sps):
    """Low-frequency delay of the filter in seconds (how late the trigger
    sees a slow rise)."""
    total = 0.
    w = 2. * math.pi * 0.5 / sps            # 0.5 Hz: the slow-rise delay
    for b0, b1, b2, _, a1, a2 in rows:
        def phase(c0, c1, c2, ww):
            return cmath.phase(c0 + c1 * cmath.exp(-1j * ww)
                               + c2 * cmath.exp(-2j * ww))
        dw = w * 1e-3
        pb = phase(b0, b1, b2, w + dw) - phase(b0, b1, b2, w)
        pa = phase(1., a1, a2, w + dw) - phase(1., a1, a2, w)
        total += -(pb - pa) / dw
    return total / sps
