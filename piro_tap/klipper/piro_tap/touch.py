"""One nozzle touch, probe points with retries, and Z homing touches.

A touch:
  1. tare: Eddy frequency with the nozzle in the air (every touch, every
     retry, every homing touch)
  2. down until the MCU trigger fires (or the force limit stops the move)
  3. dwell, then a slow continuous pullback over pullback_distance
     (Kalico's method; 4 samples per micron by default)
  4. tapfit: Z of contact = where the release line meets the free-air line

Z homing (G28 with probe:z_virtual_endstop) goes through Klipper's own
homing code: it calls the probe twice (homing_speed, then after
homing_retract_dist at second_homing_speed). PIRO-TAP touches twice per
call: the two fast touches find the bed, Z is the average of the two slow
touches, which must agree within home_tolerance."""
import math

from . import tapfit
from .errors import TapError


class Toucher:
    def __init__(self, pt):
        self.pt = pt
        self.printer = pt.printer
        self.reactor = pt.reactor
        self.o = pt.opts
        self.air = None             # (free-air Hz, reactor time)
        self.warned_temp = False

    # -- helpers ------------------------------------------------------------
    def toolhead(self):
        return self.printer.lookup_object('toolhead')

    def nozzle_temp(self):
        try:
            return self.toolhead().get_extruder().get_heater().get_status(
                self.reactor.monotonic()).get('temperature')
        except Exception:
            try:
                return self.toolhead().get_extruder().get_status(
                    self.reactor.monotonic()).get('temperature')
            except Exception:
                return None

    def z_at(self, t):
        kin = self.toolhead().get_kinematics()
        spos = {s.get_name(): s.mcu_to_commanded_position(
            s.get_past_mcu_position(t)) for s in kin.get_steppers()}
        return kin.calc_position(spos)[2]

    def wait_samples(self, until, what):
        """Wait until the Eddy has sent samples past print_time `until`."""
        pt = self.pt
        if pt.mcu.is_fileoutput():
            return
        while pt.collector.latest < until:
            now = self.reactor.monotonic()
            if pt.mcu.estimated_print_time(now) > until + 1.0:
                if pt.hub.error:
                    raise TapError(pt.hub.error)
                raise TapError("the Eddy sent no samples (%s). Check its "
                               "wiring and PIRO_TAP_STATUS" % what)
            self.reactor.pause(now + 0.01)

    def lift(self, gcmd, dist):
        th = self.toolhead()
        cur = th.get_position()
        cur[2] += dist
        th.manual_move(cur, self.pt.get_probe_params(gcmd)['lift_speed'])

    def tare(self, what='tare'):
        """-> (tare Hz, raw noise SD) from tare_time of samples from now."""
        th = self.toolhead()
        t0 = th.get_last_move_time()
        self.wait_samples(t0 + self.o.tare_time, what)
        rows = self.pt.collector.window(t0, t0 + self.o.tare_time + 0.5)
        if not rows:
            if self.pt.mcu.is_fileoutput():
                return 3.0e6, 0.
            raise TapError("no Eddy samples for the tare")
        hz = [r[1] for r in rows]
        m = sum(hz) / len(hz)
        return m, math.sqrt(sum((x - m) ** 2 for x in hz) / len(hz))

    # -- one touch ------------------------------------------------------------
    def touch(self, gcmd, speed=None, trigger_hz=None, sign=None, label='',
              keep=True):
        """One touch at the current XY. -> (toolhead pos, result dict).
        Raises TapError if it cannot be done; a done but poor touch comes
        back with res['ok'] False or a low res['quality']."""
        pt, o = self.pt, self.o
        th = self.toolhead()
        params = pt.get_probe_params(gcmd)
        trig = trigger_hz or gcmd.get_float('TRIGGER_HZ', o.trigger_hz or 0.,
                                            minval=0.)
        if not trig:
            raise TapError("trigger_hz is not set. Run PIRO_TAP_CALIBRATE "
                           "MODE=THRESHOLD (nozzle in the air) first")
        limit = gcmd.get_float('FORCE_LIMIT_HZ', o.force_limit_hz, above=trig)
        pull = o.pullback
        sign = o.sign if sign is None else sign
        speed = speed or params['probe_speed']
        temp = self.nozzle_temp()
        if o.max_temp and label != 'thermal' and temp is not None and \
                temp > o.max_temp:
            raise TapError("nozzle at %.0f C, above max_probe_temp %.0f C"
                           % (temp, o.max_temp))
        fo = pt.mcu.is_fileoutput()
        # 1. tare
        tare, noise = self.tare()
        max_noise = o.max_noise or trig
        if noise > max_noise:
            raise TapError("Eddy too noisy before the touch (%.1f Hz SD, "
                           "allowed %.1f). Is the toolhead still shaking?"
                           % (noise, max_noise))
        if self.air is not None and \
                self.reactor.monotonic() - self.air[1] < 120. and \
                abs(tare - self.air[0]) > 0.5 * limit:
            raise TapError("the nozzle looks pressed already (%.0f Hz from "
                           "its free-air value). Raise Z and clean the "
                           "nozzle" % (tare - self.air[0]))
        # 2. arm the MCU trigger and move down
        pt.trigger.arm(pt.hub.rate, tare, trig, limit, sign)
        pos = th.get_position()
        pos[2] = pt.z_min
        t_move = th.get_last_move_time()
        phoming = self.printer.lookup_object('homing')
        try:
            epos = phoming.probing_move(pt.trig, pos, speed)
        except self.printer.command_error as e:
            raise TapError(self.why(str(e), limit))
        t_trig = pt.trig.get_last_trigger_time() or th.get_last_move_time()
        if phoming.check_probe_first_home(gcmd):
            # first homing touch: Z was unknown; call the stop Z=0
            cur = th.get_position()
            cur[2] -= epos[2]
            th.set_position(cur)
        # 3. dwell and slow pullback
        if o.dwell:
            th.dwell(o.dwell)
        p0 = th.get_last_move_time()
        up = th.get_position()
        up[2] += pull
        th.manual_move(up, pt.pullback_speed())
        p1 = th.get_last_move_time()
        if fo:
            res = {'ok': True, 'z': th.get_position()[2] - pull, 'codes': [],
                   'quality': 100, 'features': {}, 'lines': {}}
            return epos, self.record(res, {}, tare, noise, trig, temp, label,
                                     keep, speed)
        # 4. fit
        self.wait_samples(p1 + 0.03, "pullback")
        rows = pt.collector.window(t_move - o.tare_time, p1 + 0.03)
        samples = [(t, h - tare) for t, h in rows]
        ev = {'move': t_move, 'trigger': t_trig, 'pull0': p0, 'pull1': p1,
              'speed': speed}
        cache = {}

        def z_of(t):
            k = round(t, 4)
            if k not in cache:
                cache[k] = self.z_at(t)
            return cache[k]
        self.reactor.pause(self.reactor.monotonic() + 0.001)
        res = tapfit.analyze(samples, z_of, ev, trig, pull,
                             pt.state.limits(), sign)
        after = [h for t, h in rows if res.get('t_break') and
                 t > res['t_break'] + 0.05]
        if len(after) >= 5:
            self.air = (sum(after) / len(after), self.reactor.monotonic())
        if res.get('sign') and not sign and not res['codes']:
            d = 'up' if res['sign'] > 0 else 'down'
            pt.note = ("pressure moves the frequency %s: set pressure_sign: "
                       "%s" % (d, d))
        detail = {'t': [round(t - t_move, 5) for t, _ in samples],
                  'hz': [round(v, 2) for _, v in samples],
                  'events': {k: round(v - t_move, 5) for k, v in ev.items()
                             if k != 'speed'},
                  'lines': {k: [[round(a - t_move, 5), b] for a, b in v]
                            for k, v in res.get('lines', {}).items()},
                  'marks': {k: round(res[n] - t_move, 5) for k, n in (
                      ('contact', 't_contact'), ('break', 't_break'))
                      if res.get(n) is not None},
                  'result': {k: res.get(k) for k in (
                      'z', 'z_trigger', 'travel', 'peak', 'sign', 'stiffness',
                      'worst')}}
        return epos, self.record(res, detail, tare, noise, trig, temp, label,
                                 keep, speed)

    def record(self, res, detail, tare, noise, trig, temp, label, keep,
               speed):
        pt = self.pt
        pos = self.toolhead().get_position()
        pt.count += 1
        summ = {'x': round(pos[0], 3), 'y': round(pos[1], 3),
                'z': None if res.get('z') is None else round(res['z'], 5),
                'ok': bool(res.get('ok')), 'quality': res.get('quality', 0),
                'codes': res.get('codes', []), 'why': tapfit.explain(res),
                'peak': res.get('peak'), 'noise': round(noise, 2),
                'tare_hz': round(tare, 1), 'trigger_hz': trig,
                'temp': None if temp is None else round(temp, 1),
                'rate': pt.hub.rate, 'speed': speed,
                'depth': res.get('press_depth'),
                'features': res.get('features', {}), 'label': label,
                'session_tap': pt.count}
        if keep:
            summ = pt.store.add(summ, detail)
        pt.last = summ
        return dict(res, summary=summ, temp=temp)

    def why(self, msg, limit):
        if 'RAW_RANGE' in msg:
            return ("the pressure passed force_limit_hz (%.0f Hz) before the "
                    "trigger: nozzle crash, or trigger_hz too high for this "
                    "speed" % limit)
        if 'MONITOR' in msg:
            return "the Eddy stopped sending samples during the move"
        if 'OVERFLOW' in msg:
            return "filter overflow on the MCU (check the Eddy reading)"
        if 'No trigger' in msg or 'Timeout' in msg:
            return ("no contact found down to Z=%.2f. Is trigger_hz too high "
                    "or position_min too high?" % self.pt.z_min)
        return msg

    # -- probing ----------------------------------------------------------------
    FATAL = ('force_limit', 'pressed already', 'max_probe_temp', 'not set',
             'no contact found', 'sent no samples', 'filter:')

    def probe_point(self, gcmd, label='probe'):
        """A probe point: poor touches are repeated, the best valid one is
        used. -> (pos, res)"""
        o = self.o
        tries = gcmd.get_int('TAP_RETRIES', o.retries, minval=0, maxval=10)
        min_q = gcmd.get_int('MIN_QUALITY', o.min_quality, minval=0,
                             maxval=100)
        best, last_err = None, ''
        for attempt in range(tries + 1):
            try:
                epos, res = self.touch(gcmd, label=label)
                if res.get('ok') and (best is None or
                                      res['quality'] > best[1]['quality']):
                    best = (epos, res)
                if res.get('ok') and res['quality'] >= min_q:
                    break
                last_err = tapfit.explain(res)
            except TapError as e:
                last_err = str(e)
                if any(f in last_err for f in self.FATAL):
                    raise
            if attempt < tries:
                gcmd.respond_info("piro_tap: touch %d not used (%s), "
                                  "retrying" % (attempt + 1, last_err))
                self.lift(gcmd, o.retry_lift)
        if best is None:
            raise TapError("no usable touch after %d tries: %s"
                           % (tries + 1, last_err))
        epos, res = best
        if res['quality'] < min_q:
            gcmd.respond_info("piro_tap: best touch only %d%% (%s), used "
                              "anyway" % (res['quality'], tapfit.explain(res)))
        if label != 'thermal':
            self.check_mesh_temp(gcmd, res.get('temp'))
        epos = list(epos)
        epos[2] = res['z']
        return epos, res

    def check_mesh_temp(self, gcmd, temp):
        ref = self.pt.thermal.ref_temp()
        if self.warned_temp or ref is None or temp is None:
            return
        if abs(temp - ref) > 5.:
            self.warned_temp = True
            gcmd.respond_info(
                "piro_tap: probing at %.0f C but Z was homed at %.0f C. Home "
                "and probe at the same nozzle temperature" % (temp, ref))

    # -- Z homing -------------------------------------------------------------
    def home_attempt(self, gcmd):
        """Klipper's homing calls this twice (fast, then slow). Each call:
        two touches. Fast: the second touch is used (the first finds the
        bed). Slow: the average of two touches that agree."""
        o = self.o
        attempt = gcmd.get('HOME_ATTEMPT_NUM', '1')
        params = self.pt.get_probe_params(gcmd)
        lift = max(params['sample_retract_dist'], 1.)
        zs, last = [], None
        most = 2 + (2 * o.retries if attempt != '1' else o.retries)
        for n in range(most):
            if n:
                self.lift(gcmd, lift)
            epos, res = self.touch(gcmd, label='home %s' % attempt)
            if not res.get('ok'):
                gcmd.respond_info("piro_tap: homing touch not used (%s)"
                                  % tapfit.explain(res))
                continue
            zs.append(res['z'])
            last = (epos, res)
            if attempt == '1':
                if len(zs) >= 2:
                    break
                continue
            if len(zs) >= 2 and abs(zs[-1] - zs[-2]) <= o.home_tolerance:
                break
            if len(zs) >= 2:
                gcmd.respond_info("piro_tap: homing touches %.4f and %.4f "
                                  "differ by more than %.3f mm, touching again"
                                  % (zs[-2], zs[-1], o.home_tolerance))
        else:
            if len(zs) < 2:
                raise TapError("Z homing: only %d usable touch(es)" % len(zs))
            raise TapError("Z homing: no two touches agreed within %.3f mm "
                           "(%s)" % (o.home_tolerance,
                                     ", ".join("%.4f" % z for z in zs)))
        epos = list(last[0])
        epos[2] = zs[-1] if attempt == '1' else (zs[-1] + zs[-2]) / 2.
        self.pt.thermal.note_home(last[1].get('temp'))
        gcmd.respond_info("piro_tap: homing %s: Z %s" % (
            'fast' if attempt == '1' else 'slow',
            ", ".join("%.4f" % z for z in zs[-2:])))
        return epos, last[1]

    def run(self, gcmd):
        """The probe session's run_probe."""
        if gcmd.get_command() == 'G28' and \
                gcmd.get('HOME_ATTEMPT_NUM', None) is not None:
            return self.home_attempt(gcmd)
        return self.probe_point(gcmd)
