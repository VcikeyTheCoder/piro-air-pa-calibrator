"""PIRO-TAP G-code commands (other than Klipper's own probe commands)."""
from .calibrate import Calibrator
from .errors import TapError
from .touch import explain

HELP = {
    'PIRO_TAP_STATUS': "PIRO-TAP: rate, trigger, thermal, last touch",
    'PIRO_TAP_TEST': "PIRO-TAP: wait for a hand press on the nozzle",
    'PIRO_TAP_CALIBRATE': "PIRO-TAP: MODE=THRESHOLD, DRIFT or "
                          "TAP_PERFORMANCE",
    'PIRO_TAP_THERMAL_CALIBRATE': "PIRO-TAP: nozzle growth START= END= STEP=",
    'PIRO_TAP_THERMAL_APPLY': "PIRO-TAP: Z offset for TEMP= (first layer)",
    'PIRO_TAP_SET_RATE': "PIRO-TAP: Eddy sample rates PROBE= PA= IDLE=",
    'NOZZLE_CLEAN_CHECK': "PIRO-TAP: touch until 3 clean touches agree",
}


class Commands:
    def __init__(self, pt):
        self.pt = pt
        gcode = pt.printer.lookup_object('gcode')
        for name, desc in HELP.items():
            gcode.register_command(name, self._wrap(getattr(self, name)),
                                   desc=desc)

    def _wrap(self, fn):
        pt = self.pt

        def run(gcmd):
            try:
                fn(gcmd)
            except TapError as e:
                raise gcmd.error("piro_tap: %s" % (e,))
            finally:
                pt.end_session()
        return run

    # -------------------------------------------------------------------
    def PIRO_TAP_STATUS(self, gcmd):
        pt, o = self.pt, self.pt.opts
        st = pt.hub.status()
        th = pt.thermal.status()
        lines = [
            "PIRO-TAP %s: %s, %d samples/s (%s; probe %d, PA %d, idle %d)"
            % (pt.name, 'streaming' if st['streaming'] else 'stopped',
               st['rate'], st['mode'], st['rates']['probe'], st['rates']['pa'],
               st['rates']['idle']),
            "trigger_hz: %s  force_limit_hz: %.0f  pressure_sign: %s  "
            "pullback: %.3f mm at %.3f mm/s"
            % ('%.1f' % o.trigger_hz if o.trigger_hz else 'NOT SET',
               o.force_limit_hz, {0: 'auto', 1: 'up', -1: 'down'}[o.sign],
               pt.get_pullback_distance(), pt.get_pullback_speed()),
            "thermal_coeff: %.3f um/C  last Z homing at: %s  applied: %+.4f mm"
            % (th['coeff'] * 1000.,
               '-' if th['home_temp'] is None else '%.0f C' % th['home_temp'],
               th['applied']),
            "Eddy errors: %d  overflows: %d" % (st['errors'], st['overflows'])]
        if st['error']:
            lines.append("start error: " + st['error'])
        if st['note']:
            lines.append("note: " + st['note'])
        if pt.note:
            lines.append(pt.note)
        if pt.last:
            lines.append("last touch: z=%s %s" % (pt.last['z'],
                                                  pt.last['why']))
        gcmd.respond_info("\n".join(lines))

    def PIRO_TAP_SET_RATE(self, gcmd):
        vals = {}
        for m in ('probe', 'pa', 'idle'):
            v = gcmd.get_int(m.upper(), None)
            if v is not None:
                vals[m] = v
        try:
            self.pt.hub.set_rates(**vals)
        except ValueError as e:
            raise TapError(str(e))
        gcmd.respond_info("Rates %s until restart (set probe_rate, pa_rate, "
                          "idle_rate in [%s] to keep them)"
                          % (self.pt.hub.rates, self.pt.name))

    def PIRO_TAP_CALIBRATE(self, gcmd):
        Calibrator(self.pt).run(gcmd)

    def PIRO_TAP_THERMAL_CALIBRATE(self, gcmd):
        self.pt.thermal.calibrate(gcmd)

    def PIRO_TAP_THERMAL_APPLY(self, gcmd):
        self.pt.thermal.apply(gcmd)

    def PIRO_TAP_TEST(self, gcmd):
        """Press the nozzle by hand: the MCU trigger must fire. No move."""
        pt, o = self.pt, self.pt.opts
        n = gcmd.get_int('TAPS', 3, minval=1, maxval=10)
        timeout = gcmd.get_float('TIMEOUT', 30., minval=1., maxval=120.)
        if not o.trigger_hz:
            raise TapError("trigger_hz is not set")
        pt.begin_session()
        th = pt.toucher.toolhead()
        for i in range(n):
            tare, _ = pt.toucher.tare()
            # a hand press may be strong: no force limit here
            pt.trigger.arm(pt.hub.rate, tare, o.trigger_hz,
                           o.force_limit_hz * 50., o.sign)
            gcmd.respond_info("Press the nozzle up (%d of %d)" % (i + 1, n))
            t = th.get_last_move_time()
            pt.trig.home_start(t, 0., 0, 0.)
            if not pt.trig.home_wait(t + timeout):
                raise TapError("no press seen in %.0f s" % timeout)
            gcmd.respond_info("Press seen")
            pt.reactor.pause(pt.reactor.monotonic() + 0.5)

    def NOZZLE_CLEAN_CHECK(self, gcmd):
        """Touch at this XY until CLEAN touches in a row are good taps
        (Kalico's tap analysis and quality) and agree within SPREAD."""
        pt = self.pt
        need = gcmd.get_int('CLEAN', 3, minval=1, maxval=10)
        most = gcmd.get_int('MAX_TAPS', 12, minval=need, maxval=50)
        spread = gcmd.get_float('SPREAD', 0.010, above=0.)
        params = pt.get_probe_params(gcmd)
        pt.check_homed()
        pt.begin_session()
        run = []
        for i in range(most):
            try:
                _, res = pt.toucher.touch(gcmd, label='clean check')
                good = bool(res.get('ok'))
                why = explain(res)
            except TapError as e:
                if 'force_limit' in str(e):
                    raise
                good, res, why = False, {}, str(e)
            run = (run + [res['z']]) if good else []
            gcmd.respond_info("touch %d: %s%s" % (
                i + 1, ('z=%.4f ' % res['z']) if good else '', why))
            pt.toucher.lift(gcmd, max(params['sample_retract_dist'], 0.5))
            if len(run) >= need and \
                    max(run[-need:]) - min(run[-need:]) <= spread:
                gcmd.respond_info(
                    "Nozzle clean: %d clean touches in a row (spread %.4f mm)"
                    % (need, max(run[-need:]) - min(run[-need:])))
                return
        raise TapError("nozzle not clean after %d touches. Wipe it (hot) and "
                       "try again" % most)
