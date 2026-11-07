"""Nozzle thermal expansion: measure it, then correct Z once before a print.

PIRO_TAP_THERMAL_CALIBRATE [START=0] [END=200] [STEP=50] [SETTLE=20]
                           [SAMPLES=3] [LIFT=5]
  At the current XY (homed), for each nozzle temperature START, START+STEP,
  ... END: heat (START=0: heater off, wait until the nozzle stops cooling),
  wait SETTLE seconds, touch SAMPLES times. The Z of contact rises as the
  nozzle grows. A straight line through (temperature, Z) gives the growth
  in mm per degree C: thermal_coeff, staged for SAVE_CONFIG. Static only:
  no check of how fast the nozzle follows the heater.

PIRO_TAP_THERMAL_APPLY TEMP=<first layer temp> [RESET=1]
  In PRINT_START, after homing (and mesh) and before the first layer:
  offset = thermal_coeff x (TEMP - nozzle temp of the last Z homing),
  applied as a G-code Z offset. It replaces the offset applied before, so
  nothing builds up; it never changes during a print.

Sign: a longer nozzle touches the bed earlier, so Z of contact is higher;
when printing hotter than at homing the tip is lower than Z thinks, so the
offset raises Z by the same amount."""
import math
import time

from .errors import TapError


def fit_line(xs, ys):
    """Least squares y = a + b x. -> (a, b, max |residual|)"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        raise TapError("all temperatures were the same")
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    res = max(abs(y - (a + b * x)) for x, y in zip(xs, ys))
    return a, b, res


def steps(start, end, step):
    if step <= 0 or end <= start:
        raise TapError("need START < END and STEP > 0")
    out, t = [], start
    while t <= end + 1e-6:
        out.append(round(t, 3))
        t += step
    if len(out) < 2:
        raise TapError("need at least two temperatures")
    return out


class Thermal:
    def __init__(self, pt, coeff):
        self.pt = pt
        self.printer = pt.printer
        self.reactor = pt.reactor
        self.coeff = coeff              # mm per degree C (thermal_coeff)
        self.home_temp = None           # nozzle temp at the last Z homing
        self.home_time = None
        self.applied = 0.               # G-code Z offset we applied
        self.applied_for = None         # (TEMP, ref)

    # -- reference temperature ----------------------------------------------
    def note_home(self, temp):
        if temp is not None:
            self.home_temp = float(temp)
            self.home_time = time.time()

    def ref_temp(self):
        return self.home_temp

    # -- apply before a print -----------------------------------------------
    def apply(self, gcmd):
        if gcmd.get_int('RESET', 0, minval=0, maxval=1):
            self._set_offset(gcmd, 0.)
            self.applied_for = None
            gcmd.respond_info("piro_tap: thermal Z offset removed")
            return
        temp = gcmd.get_float('TEMP', minval=0., maxval=500.)
        if not self.coeff:
            gcmd.respond_info("piro_tap: thermal_coeff is 0, no thermal "
                              "correction (run PIRO_TAP_THERMAL_CALIBRATE)")
            return
        ref = gcmd.get_float('REF', self.home_temp)
        if ref is None:
            raise TapError("Z was not homed with PIRO-TAP since the restart: "
                           "home Z first, then PIRO_TAP_THERMAL_APPLY")
        want = self.coeff * (temp - ref)
        self._set_offset(gcmd, want)
        self.applied_for = (temp, ref)
        gcmd.respond_info("piro_tap: thermal Z offset %+.4f mm (homed at %.0f "
                          "C, printing at %.0f C, %.2f um/C)"
                          % (want, ref, temp, self.coeff * 1000.))

    def _set_offset(self, gcmd, want):
        delta = want - self.applied
        if abs(delta) > 1e-7:
            gcode = self.printer.lookup_object('gcode')
            gcode.run_script_from_command(
                "SET_GCODE_OFFSET Z_ADJUST=%.6f MOVE=0" % delta)
        self.applied = want

    # -- calibrate ------------------------------------------------------------
    def _heater(self):
        th = self.printer.lookup_object('toolhead')
        return th.get_extruder().get_heater()

    def _temp(self, heater):
        return heater.get_status(self.reactor.monotonic())['temperature']

    def _cool(self, gcmd, heater, below, max_wait):
        """Heater off; wait until the nozzle stops cooling (or is under
        `below`)."""
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(heater, 0.)
        end = self.reactor.monotonic() + max_wait
        hist = []
        last_msg = 0.
        while True:
            now = self.reactor.monotonic()
            t = self._temp(heater)
            hist.append((now, t))
            hist = [h for h in hist if now - h[0] <= 30.]
            if t < below:
                return t
            if now - hist[0][0] >= 29. and hist[0][1] - t < 0.5:
                return t                    # no longer cooling
            if now > end:
                raise TapError("nozzle still cooling after %.0f min (%.0f C)"
                               % (max_wait / 60., t))
            if now - last_msg > 30.:
                gcmd.respond_info("piro_tap: cooling, nozzle at %.0f C" % t)
                last_msg = now
            self.reactor.pause(now + 1.)

    def calibrate(self, gcmd):
        pt = self.pt
        start = gcmd.get_float('START', 0., minval=0.)
        end = gcmd.get_float('END', 200., above=0.)
        step = gcmd.get_float('STEP', 50., above=0.)
        settle = gcmd.get_float('SETTLE', 20., minval=0., maxval=600.)
        n = gcmd.get_int('SAMPLES', 3, minval=1, maxval=10)
        lift = gcmd.get_float('LIFT', 5., minval=1., maxval=50.)
        temps = steps(start, end, step)
        heater = self._heater()
        max_t = getattr(heater, 'max_temp', None)
        if max_t is not None and end > max_t - 5.:
            raise TapError("END=%.0f is too close to the heater's max_temp "
                           "%.0f" % (end, max_t))
        pt.check_homed()
        pheaters = self.printer.lookup_object('heaters')
        old_target = heater.get_status(
            self.reactor.monotonic()).get('target', 0.)
        th = self.printer.lookup_object('toolhead')
        x, y = th.get_position()[:2]
        points = []
        gcmd.respond_info(
            "piro_tap: thermal calibration at X%.1f Y%.1f: %s C, %d touches "
            "each, %.0f s settle. Keep the bed temperature fixed"
            % (x, y, ", ".join("%.0f" % t for t in temps), n, settle))
        try:
            for target in temps:
                pt.end_session()
                self._lift_to(gcmd, lift)
                if target <= 0.:
                    nxt = temps[1] if len(temps) > 1 else 50.
                    self._cool(gcmd, heater, nxt - 10., 30 * 60.)
                else:
                    pheaters.set_temperature(heater, target, True)
                if settle:
                    gcmd.respond_info("piro_tap: nozzle at %.0f C, settling "
                                      "%.0f s" % (self._temp(heater), settle))
                    th.wait_moves()
                    self.reactor.pause(self.reactor.monotonic() + settle)
                pt.begin_session()
                zs, ts = [], []
                for i in range(n):
                    if i:
                        self._lift_to(gcmd, 1.)
                    _, res = pt.toucher.probe_point(gcmd, label='thermal')
                    zs.append(res['z'])
                    ts.append(self._temp(heater))
                pt.end_session()
                m = sum(zs) / len(zs)
                sd = math.sqrt(sum((z - m) ** 2 for z in zs) / len(zs))
                tm = sum(ts) / len(ts)
                points.append([round(tm, 2), round(m, 5), round(sd, 5),
                               target])
                gcmd.respond_info("piro_tap: %.1f C: Z %.4f (spread %.4f)"
                                  % (tm, m, max(zs) - min(zs)))
        finally:
            pt.end_session()
            try:
                self._lift_to(gcmd, lift)
            except Exception:
                pass
            pheaters.set_temperature(heater, old_target)
        a, b, worst = fit_line([p[0] for p in points], [p[1] for p in points])
        t0 = points[0][0]
        growth = b * (points[-1][0] - t0)
        result = {'points': points, 'coeff': b, 'intercept': a,
                  'max_residual': worst, 'time': time.time(), 'x': x, 'y': y}
        pt.state.set('thermal', result)
        self.coeff = b
        gcmd.respond_info(
            "piro_tap: nozzle grows %.1f um from %.0f to %.0f C: %.3f um/C "
            "(largest miss of the line %.1f um)"
            % (growth * 1000., t0, points[-1][0], b * 1000., worst * 1000.))
        pt.save_config(gcmd, 'thermal_coeff', '%.7f' % b)

    def _lift_to(self, gcmd, dist):
        self.pt.toucher.lift(gcmd, dist)

    def status(self):
        cal = self.pt.state.data.get('thermal')
        return {'coeff': self.coeff, 'home_temp': self.home_temp,
                'applied': round(self.applied, 6),
                'applied_for': self.applied_for, 'calibration': cal}
