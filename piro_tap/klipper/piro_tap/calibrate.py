"""PIRO_TAP_CALIBRATE MODE=THRESHOLD | DRIFT | QUALITY

THRESHOLD  Nozzle in the air. Tare first, then watch the Eddy through the
           same filter the MCU trigger uses. Noise = largest minus lowest
           value (peak to peak); trigger_hz = 3 x noise. Then one touch (if
           homed) finds which way pressure moves the frequency.
DRIFT      Nozzle in the air for 10 s: how fast the reading wanders. If it
           moves more than a quarter of trigger_hz during one probe move, a
           slow high-pass (drift_filter_hz) is suggested.
QUALITY    10 touches on a clean nozzle: the ooze checks are scaled to what a
           clean touch looks like on this printer.
pullback_distance is a fixed setting (no calibration). Values for
printer.cfg are staged for SAVE_CONFIG, like Klipper's own calibrations."""
import math

from . import tapfit
from .errors import TapError

TRIGGER_X_NOISE = 3.


def _stats(v):
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / max(1, len(v) - 1))
    return m, sd


class Calibrator:
    def __init__(self, pt):
        self.pt = pt

    def run(self, gcmd):
        mode = gcmd.get('MODE', 'THRESHOLD').upper()
        fn = getattr(self, 'mode_' + mode.lower(), None)
        if fn is None:
            raise gcmd.error("MODE must be THRESHOLD, DRIFT or QUALITY")
        self.pt.begin_session()
        try:
            fn(gcmd)
        finally:
            self.pt.end_session()

    def _collect(self, seconds):
        th = self.pt.toucher.toolhead()
        t0 = th.get_last_move_time()
        self.pt.toucher.wait_samples(t0 + seconds, "calibration")
        rows = self.pt.collector.window(t0, t0 + seconds)
        if len(rows) < 20:
            raise TapError("too few Eddy samples (%d)" % len(rows))
        return rows

    def _touches(self, gcmd, n, **kw):
        self.pt.check_homed()
        out = []
        for i in range(n):
            try:
                _, res = self.pt.toucher.touch(gcmd, label='calibrate', **kw)
                why = tapfit.explain(res)
            except TapError as e:
                res, why = {'ok': False}, str(e)
            gcmd.respond_info("touch %d/%d: %s%s" % (
                i + 1, n, ('z=%.4f ' % res['z']) if res.get('ok') else '',
                why))
            out.append(res)
            self.pt.toucher.lift(gcmd, 1.0)
        return out

    def mode_threshold(self, gcmd):
        pt = self.pt
        secs = gcmd.get_float('DURATION', 3., minval=1., maxval=30.)
        gcmd.respond_info("Tare, then %.0f s of Eddy noise at %d samples/s: "
                          "keep the nozzle in the air and the printer still"
                          % (secs, pt.hub.rate))
        tare, sd_raw = pt.toucher.tare("calibration tare")
        rows = self._collect(secs)
        hz = [r[1] for r in rows]
        f = pt.trigger.filtered(pt.hub.rate, [h - tare for h in hz])
        f = f[max(10, len(f) // 5):]        # skip the filter start-up
        p2p = max(f) - min(f)
        trig = round(max(TRIGGER_X_NOISE * p2p, 1.), 1)
        gcmd.respond_info(
            "tare %.1f Hz; noise through the %g Hz filter: %.1f Hz peak to "
            "peak (raw SD %.1f Hz)\ntrigger_hz = %g x %.1f = %.1f"
            % (tare, pt.opts.lowpass_hz, p2p, sd_raw, TRIGGER_X_NOISE, p2p,
               trig))
        if trig >= pt.opts.force_limit_hz:
            raise TapError("trigger_hz %.0f would be above force_limit_hz "
                           "%.0f: the Eddy is far too noisy" %
                           (trig, pt.opts.force_limit_hz))
        pt.opts.trigger_hz = trig
        pt.save_config(gcmd, 'trigger_hz', '%.1f' % trig)
        if not gcmd.get_int('TAP', 1, minval=0, maxval=1):
            return
        if not pt.is_homed():
            gcmd.respond_info("Not homed: skipping the touch that finds "
                              "pressure_sign")
            return
        res = self._touches(gcmd, 1, trigger_hz=trig, sign=0)[0]
        if res.get('ok') and res.get('sign'):
            name = 'up' if res['sign'] > 0 else 'down'
            gcmd.respond_info("Pressure moves the Eddy frequency %s (peak %.0f "
                              "Hz)" % (name, res.get('peak') or 0))
            pt.opts.sign = res['sign']
            pt.save_config(gcmd, 'pressure_sign', name)

    def mode_drift(self, gcmd):
        pt = self.pt
        secs = gcmd.get_float('DURATION', 10., minval=3., maxval=60.)
        gcmd.respond_info("Watching the Eddy for %.0f s, nozzle in the air"
                          % secs)
        rows = self._collect(secs)
        t = [r[0] - rows[0][0] for r in rows]
        hz = [r[1] for r in rows]
        mt, _ = _stats(t)
        mh, _ = _stats(hz)
        sxx = sum((a - mt) ** 2 for a in t)
        slope = sum((a - mt) * (b - mh) for a, b in zip(t, hz)) / sxx
        p = pt.get_probe_params(gcmd)
        move_t = max(p['sample_retract_dist'], 1.) / p['probe_speed'] + \
            pt.opts.tare_time
        trig = pt.opts.trigger_hz or 20.
        drift = abs(slope) * move_t
        gcmd.respond_info("drift %.2f Hz/s: %.1f Hz during one probe move "
                          "(trigger_hz %.1f)" % (slope, drift, trig))
        val = 0.5 if drift > 0.25 * trig else 0.
        gcmd.respond_info("Drift is large: a 0.5 Hz drift filter keeps it from "
                          "triggering early" if val else
                          "Drift is small: no drift filter needed")
        pt.opts.drift_hz = val
        pt.save_config(gcmd, 'drift_filter_hz', '%g' % val)

    def mode_quality(self, gcmd):
        n = gcmd.get_int('SAMPLES', 10, minval=5, maxval=30)
        gcmd.respond_info("Clean the nozzle first: these touches define a "
                          "clean touch")
        res = self._touches(gcmd, n)
        rows = [r['features'] for r in res if r.get('ok')]
        if len(rows) < 5:
            raise TapError("only %d usable touches" % len(rows))
        lim = tapfit.suggest_limits(rows)
        self.pt.state.set('limits', lim)
        gcmd.respond_info("Quality limits saved (%s): %s" % (
            self.pt.state.path, ", ".join("%s %.3f" % kv
                                         for kv in sorted(lim.items()))))
