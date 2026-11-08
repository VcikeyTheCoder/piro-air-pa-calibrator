"""One nozzle touch, probe points with retries, and Z homing touches.

A touch is Kalico's tapping move (load_cell_probe.py TappingMove), with the
Eddy in Hz instead of a load cell in grams:
  1. tare: Eddy frequency with the nozzle in the air (every touch)
  2. probing move down until the MCU trigger fires (or the force limit stops
     the move)
  3. pullback move: straight up pullback_distance at pullback_speed, then
     flush_step_generation (no dwell)
  4. Kalico's tap analysis (kalico_tap_analysis.py, copied 1:1): five lines
     (approach, compression, dwell, decompression, departure) fitted in the
     time domain; Z = the toolhead position, from the move queue, at the
     time the decompression line meets the departure line (contact breaks)
  5. Kalico's tap quality classifier (kalico_tap_quality_classifier.py)
A bad tap is retried with Kalico's bad_probe_strategy (kalico_retry.py).

Z homing (G28 with probe:z_virtual_endstop) goes through Klipper's own
homing code: it calls the probe twice (homing_speed, then after
homing_retract_dist at second_homing_speed). PIRO-TAP touches twice per
call: the two fast touches find the bed, Z is the average of the two slow
touches, which must agree within home_tolerance."""
import math

from .errors import TapError
from .kalico_retry import RetrySession

LINES = ('approach', 'compression', 'dwell', 'decompression', 'departure')


def explain(res):
    """One line for a touch result."""
    if res.get('ok'):
        q = res.get('quality')
        return 'good tap' + ('' if q is None else ' (quality %.0f%%)' % q)
    return res.get('error') or 'bad tap'


class Toucher:
    def __init__(self, pt):
        self.pt = pt
        self.printer = pt.printer
        self.reactor = pt.reactor
        self.o = pt.opts
        self.air = None             # (free-air Hz, reactor time)
        self.warned_temp = False
        self.tare_end = None        # print time the last tare ended

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
        th.manual_move([None, None, cur[2] + dist],
                       self.pt.get_probe_params(gcmd)['lift_speed'])

    def tare(self, what='tare'):
        """-> (tare Hz, raw noise SD) from tare_time of samples from now."""
        th = self.toolhead()
        t0 = th.get_last_move_time()
        self.tare_end = t0 + self.o.tare_time
        self.wait_samples(self.tare_end, what)
        rows = self.pt.collector.window(t0, self.tare_end + 0.5)
        if not rows:
            if self.pt.mcu.is_fileoutput():
                return 3.0e6, 0.
            raise TapError("no Eddy samples for the tare")
        hz = [r[1] for r in rows]
        m = sum(hz) / len(hz)
        return m, math.sqrt(sum((x - m) ** 2 for x in hz) / len(hz))

    # Kalico: TappingMove.pullback_move
    def pullback_move(self, gcmd):
        toolhead = self.toolhead()
        pullback_pos = toolhead.get_position()
        pullback_pos[2] += self.pt.get_pullback_distance(gcmd)
        pos = [None, None, pullback_pos[2]]
        toolhead.manual_move(pos, self.pt.get_pullback_speed(gcmd))
        toolhead.flush_step_generation()
        pullback_end = toolhead.get_last_move_time()
        return pullback_end

    # -- one touch ------------------------------------------------------------
    def touch(self, gcmd, speed=None, trigger_hz=None, sign=None, label='',
              keep=True):
        """One touch at the current XY. -> (toolhead pos, result dict).
        Raises TapError if it cannot be done (and on a bad tap Kalico calls
        not recoverable); a recoverable bad tap comes back with res['ok']
        False."""
        pt, o = self.pt, self.o
        th = self.toolhead()
        params = pt.get_probe_params(gcmd)
        trig = trigger_hz or gcmd.get_float('TRIGGER_HZ', o.trigger_hz or 0.,
                                            minval=0.)
        if not trig:
            raise TapError("trigger_hz is not set. Run PIRO_TAP_CALIBRATE "
                           "MODE=THRESHOLD (nozzle in the air) first")
        limit = gcmd.get_float('FORCE_LIMIT_HZ', o.force_limit_hz, above=trig)
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
        t_start = self.tare_end
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
        # 2. arm the MCU trigger, probing move
        pt.trigger.arm(pt.hub.rate, tare, trig, limit, sign)
        pos = th.get_position()
        pos[2] = pt.z_min
        t_move = th.get_last_move_time()
        phoming = self.printer.lookup_object('homing')
        try:
            epos = phoming.probing_move(pt.trig, pos, speed)
        except self.printer.command_error as e:
            raise TapError(self.why(str(e), limit))
        epos = list(epos)
        t_trig = pt.trig.get_last_trigger_time() or th.get_last_move_time()
        # 3. pullback move
        p0 = th.get_last_move_time()
        pullback_end_time = self.pullback_move(gcmd)
        if fo:
            res = {'ok': True, 'z': epos[2], 'quality': None}
            return epos, self.record(res, {}, tare, noise, trig, temp, label,
                                     keep, speed)
        # 4. collect the samples of the tap and analyse them (Kalico)
        self.wait_samples(pullback_end_time, "pullback")
        rows = pt.collector.window(t_start, pullback_end_time)
        samples = [(t, h - tare) for t, h in rows]
        t_end = pt.mcu.estimated_print_time(self.reactor.monotonic())
        collection_time = t_end - pullback_end_time
        if len(samples) < 20:
            raise TapError("only %d Eddy samples for the tap" % len(samples))
        ta = pt.tap_helper.analyze(samples, trig, collection_time, gcmd)
        res = {'ok': ta.is_valid(), 'z': None, 'quality': None,
               'error': None, 'code': None}
        if res['ok']:
            res['z'] = ta.get_tap_pos()[2]
            if pt.classifier.config.decompression_angle is not None:
                res['quality'] = round(
                    pt.classifier.calculate_tap_quality(ta), 1)
        else:
            err = ta.get_validation_error()
            if err is not None:
                res['code'] = err.error_code
                res['error'] = "%s - %s" % (err.error_code, err)
                res['recoverable'] = err.is_recoverable
        # the tap points exist when the shape was found, also for a tap the
        # quality classifier turned down (drawn on the web page)
        pts = ta.get_tap_points()
        if len(pts) != 6:
            pts = []
        if res['ok']:
            press = pts[2].force - pts[1].force
            res['sign'] = 1 if press > 0 else -1
            res['peak'] = round(abs(press), 1)
            # free-air value after contact broke (for the "pressed already"
            # check of the next touch)
            t_break = pts[4].time + float(samples[0][0])
            after = [h for t, h in rows if t > t_break + 0.02]
            if len(after) >= 5:
                self.air = (sum(after) / len(after), self.reactor.monotonic())
            if not sign:
                d = 'up' if res['sign'] > 0 else 'down'
                pt.note = ("pressure moves the frequency %s: set "
                           "pressure_sign: %s" % (d, d))
        detail = self.detail(ta, samples, t_move, {
            'trigger': t_trig, 'pull0': p0, 'pull1': pullback_end_time},
            res, pts)
        epos_out = list(epos)
        if res['ok']:
            epos_out[2] = res['z']
        out = epos_out, self.record(res, detail, tare, noise, trig, temp,
                                    label, keep, speed)
        # Kalico: a bad tap that is not recoverable stops; a recoverable one
        # is reported and left to the retry policy
        if not res['ok'] and res.get('error'):
            if res.get('recoverable'):
                gcmd.respond_info(res['error'])
            else:
                raise TapError(res['error'])
        return out

    def detail(self, ta, samples, t_move, events, res, pts):
        """The tap for the web page: times in s from the start of the
        probing move, Hz from the tare."""
        off = float(samples[0][0])
        rel = lambda t: round(t + off - t_move, 5)
        d = {'t': [round(t - t_move, 5) for t, _ in samples],
             'hz': [round(v, 2) for _, v in samples],
             'events': {k: round(v - t_move, 5) for k, v in events.items()},
             'lines': {}, 'marks': {},
             'result': {'z': res.get('z'), 'sign': res.get('sign'),
                        'peak': res.get('peak'),
                        'quality': res.get('quality'),
                        'error': res.get('error')}}
        if pts:
            for i, name in enumerate(LINES):
                d['lines'][name] = [[rel(pts[i].time), pts[i].force],
                                    [rel(pts[i + 1].time), pts[i + 1].force]]
            d['marks'] = {'contact': rel(pts[1].time),
                          'break': rel(pts[4].time)}
            d['result']['angles'] = [round(a, 2)
                                     for a in ta.get_tap_angles()]
            lines = ta.get_tap_lines()
            d['result']['decompression_angle'] = round(abs(
                self.pt.classifier._slope_to_degrees(lines[3].slope)), 2)
        return d

    def record(self, res, detail, tare, noise, trig, temp, label, keep,
               speed):
        pt = self.pt
        pos = self.toolhead().get_position()
        pt.count += 1
        r = detail.get('result', {}) if detail else {}
        summ = {'x': round(pos[0], 3), 'y': round(pos[1], 3),
                'z': None if res.get('z') is None else round(res['z'], 5),
                'ok': bool(res.get('ok')), 'quality': res.get('quality'),
                'code': res.get('code'), 'why': explain(res),
                'peak': res.get('peak'), 'noise': round(noise, 2),
                'tare_hz': round(tare, 1), 'trigger_hz': trig,
                'temp': None if temp is None else round(temp, 1),
                'rate': pt.hub.rate, 'speed': speed,
                'decompression_angle': r.get('decompression_angle'),
                'label': label, 'session_tap': pt.count}
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

    # -- probing (Kalico: PrinterProbe._run_probe_with_retries) ---------------
    def probe_point(self, gcmd, label='probe'):
        """A probe point: bad taps are handled by bad_probe_strategy.
        -> (pos, res)"""
        th = self.toolhead()
        retry_session = RetrySession(self.pt.retry_factory.customize(gcmd),
                                     gcmd)
        retry_session.set_position(th.get_position())
        params = self.pt.get_probe_params(gcmd)
        while retry_session.can_retry():
            th.manual_move(retry_session.get_probe_position(),
                           retry_session.retry_policy.retry_speed)
            epos, res = self.touch(gcmd, label=label)
            if retry_session.evaluate_probe(res.get('ok')):
                if not res.get('ok'):
                    # IGNORE: Kalico keeps the probing move's stop position
                    res = dict(res, z=epos[2])
                if label != 'thermal':
                    self.check_mesh_temp(gcmd, res.get('temp'))
                x, y = retry_session.get_position()
                return [x, y, res['z']] + list(epos[3:]), res
            # Probe was rejected, retry
            self.lift(gcmd, params['sample_retract_dist'])
            retry_session.scrub_nozzle()
        attempts = retry_session.get_bad_probe_count()
        raise TapError("Probing failed to collect a good sample after %d "
                       "attempts" % attempts)

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
        bed). Slow: the average of two touches that agree. A bad tap is
        touched again, up to bad_probe_retries times."""
        o = self.o
        attempt = gcmd.get('HOME_ATTEMPT_NUM', '1')
        params = self.pt.get_probe_params(gcmd)
        retries = self.pt.retry_factory.customize(gcmd).bad_probe_retries
        lift = max(params['sample_retract_dist'], 1.)
        zs, last = [], None
        most = 2 + (2 * retries if attempt != '1' else retries)
        for n in range(most):
            if n:
                self.lift(gcmd, lift)
            epos, res = self.touch(gcmd, label='home %s' % attempt)
            if not res.get('ok'):
                gcmd.respond_info("piro_tap: homing touch not used (%s)"
                                  % explain(res))
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
