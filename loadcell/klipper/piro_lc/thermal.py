"""Nozzle growth (thermal expansion): calibrate once, predict before a print.

PIRO_LC_GROWTH_CALIBRATE [START=0] [END=200] [STEP=50] [SETTLE=20]
                         [SAMPLES=3] [LIFT=5]
  At the current XY (homed), for each nozzle temperature START, START+STEP,
  ... END: heat (START=0: heater off, wait until the nozzle stops cooling),
  wait SETTLE seconds, touch SAMPLES times. A longer nozzle touches the
  bed earlier, so Z of contact rises. A straight line through
  (temperature, Z) gives nozzle_growth in mm per degree C, staged for
  SAVE_CONFIG in [load_cell_probe].

PIRO_LC_GROWTH_APPLY TEMP=<first layer temperature> [RESET=1]
  In PRINT_START after homing and the bed mesh, before heating to print
  temperature:  offset = nozzle_growth x (TEMP - nozzle temp of the last
  Z homing). Only the latest homing temperature is kept; every Z homing
  overwrites it and clears the applied offset.

The offset shifts Z itself (like the paper offset), not the G-code
offset, so SET_GCODE_OFFSET Z=0 can't remove it. When printing hotter
than at homing the tip is lower than Z thinks, so Z is raised."""
import math

from .errors import LCError


def fit_line(xs, ys):
    """Least squares y = a + b x. -> (a, b, max |residual|)"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        raise LCError("all temperatures were the same")
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    res = max(abs(y - (a + b * x)) for x, y in zip(xs, ys))
    return a, b, res


def steps(start, end, step):
    if step <= 0 or end <= start:
        raise LCError("need START < END and STEP > 0")
    out, t = [], start
    while t <= end + 1e-6:
        out.append(round(t, 3))
        t += step
    if len(out) < 2:
        raise LCError("need at least two temperatures")
    return out


def growth_offset(coeff, temp, home_temp):
    return coeff * (temp - home_temp)


class Growth:
    def __init__(self, lc):
        self.lc = lc
        self.printer = lc.printer
        self.reactor = lc.reactor
        self.applied = 0.               # Z shift applied since the last homing
        self.applied_for = None         # (TEMP, homing temp)

    # -- every Z homing ---------------------------------------------------
    def on_home(self):
        # homing set Z fresh, so no growth shift is in it any more
        self.applied = 0.
        self.applied_for = None

    # -- apply before a print -----------------------------------------------
    def apply(self, gcmd):
        lc = self.lc
        if gcmd.get_int('RESET', 0, minval=0, maxval=1):
            self._shift(0.)
            self.applied_for = None
            gcmd.respond_info("piro_lc: nozzle growth offset removed")
            return
        temp = gcmd.get_float('TEMP', minval=0., maxval=500.)
        coeff = lc.opts.nozzle_growth
        if not coeff:
            gcmd.respond_info("piro_lc: nozzle_growth is 0, no growth "
                              "correction (run PIRO_LC_GROWTH_CALIBRATE)")
            return
        if not lc.z_homed():
            raise LCError("home Z first, then PIRO_LC_GROWTH_APPLY")
        home_temp = lc.home_temp
        if home_temp is None:
            gcmd.respond_info("piro_lc: no Z homing temperature saved, no "
                              "growth correction. Home Z with the load "
                              "cell first")
            return
        want = growth_offset(coeff, temp, home_temp)
        if abs(want) > lc.opts.max_growth_offset:
            raise LCError(
                "nozzle growth offset %+.3f mm is more than "
                "max_growth_offset %.3f (nozzle_growth %.5f mm/C, homed at "
                "%.0f C, printing at %.0f C). Check nozzle_growth"
                % (want, lc.opts.max_growth_offset, coeff, home_temp, temp))
        self._shift(want)
        self.applied_for = (temp, home_temp)
        gcmd.respond_info("piro_lc: nozzle growth offset %+.4f mm (homed at "
                          "%.0f C, first layer at %.0f C, %.2f um/C)"
                          % (want, home_temp, temp, coeff * 1000.))

    def _shift(self, want):
        delta = want - self.applied
        if abs(delta) > 1e-7:
            self.lc.shift_z(delta)
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
        hist, last_msg = [], 0.
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
                raise LCError("nozzle still cooling after %.0f min (%.0f C)"
                              % (max_wait / 60., t))
            if now - last_msg > 30.:
                gcmd.respond_info("piro_lc: cooling, nozzle at %.0f C" % t)
                last_msg = now
            self.reactor.pause(now + 1.)

    def calibrate(self, gcmd):
        lc = self.lc
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
            raise LCError("END=%.0f is too close to the heater's max_temp "
                          "%.0f" % (end, max_t))
        if not lc.z_homed():
            raise LCError("home the printer first")
        pheaters = self.printer.lookup_object('heaters')
        old_target = heater.get_status(
            self.reactor.monotonic()).get('target', 0.)
        th = self.printer.lookup_object('toolhead')
        x, y = th.get_position()[:2]
        points = []
        gcmd.respond_info(
            "piro_lc: nozzle growth calibration at X%.1f Y%.1f: %s C, %d "
            "touches each, %.0f s settle. Keep the bed temperature fixed"
            % (x, y, ", ".join("%.0f" % t for t in temps), n, settle))
        try:
            for target in temps:
                lc.lift(lift)
                if target <= 0.:
                    nxt = temps[1] if len(temps) > 1 else 50.
                    self._cool(gcmd, heater, nxt - 10., 30 * 60.)
                else:
                    pheaters.set_temperature(heater, target, True)
                if settle:
                    gcmd.respond_info("piro_lc: nozzle at %.0f C, settling "
                                      "%.0f s" % (self._temp(heater), settle))
                    th.wait_moves()
                    self.reactor.pause(self.reactor.monotonic() + settle)
                zs, ts = lc.touch(gcmd, n), []
                ts.append(self._temp(heater))
                m = sum(zs) / len(zs)
                sd = math.sqrt(sum((z - m) ** 2 for z in zs) / len(zs))
                tm = sum(ts) / len(ts)
                points.append([round(tm, 2), round(m, 5), round(sd, 5),
                               target])
                gcmd.respond_info("piro_lc: %.1f C: Z %.4f (spread %.4f)"
                                  % (tm, m, max(zs) - min(zs)))
        finally:
            try:
                lc.lift(lift)
            except Exception:
                pass
            pheaters.set_temperature(heater, old_target)
        a, b, worst = fit_line([p[0] for p in points], [p[1] for p in points])
        t0 = points[0][0]
        growth = b * (points[-1][0] - t0)
        result = {'points': points, 'coeff': b, 'intercept': a,
                  'max_residual': worst, 'time': lc.now(), 'x': x, 'y': y}
        lc.store.set('growth', result)
        lc.opts.nozzle_growth = b
        gcmd.respond_info(
            "piro_lc: nozzle grows %.1f um from %.0f to %.0f C: %.3f um/C "
            "(largest miss of the line %.1f um)"
            % (growth * 1000., t0, points[-1][0], b * 1000., worst * 1000.))
        lc.save_config(gcmd, 'nozzle_growth', '%.7f' % b)
        return result

    def status(self):
        lc = self.lc
        return {'coeff': lc.opts.nozzle_growth, 'home_temp': lc.home_temp,
                'home_time': lc.home_time,
                'applied': round(self.applied, 6),
                'applied_for': self.applied_for,
                'calibration': lc.store.get('growth')}
