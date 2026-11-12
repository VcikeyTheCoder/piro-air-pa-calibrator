"""PA_LOADCELL_TEST (PIRO-EDGE): safety checks, a prime (PRIME_LENGTH mm at
PRIME_FLOW, extrude-only, so the melt is steady), then an adaptive search.
Every PA value is one slow lead-in leg and CYCLES fast/slow pulses, saved as
its own segment.

METHOD=push (default): the nozzle stays still and the pulses are
extrude-only moves that follow exactly what Klipper's own pressure advance
does on a print move, just without XY. Klipper's PA is set to a tiny value
(0.000001) so its smoothing stays on; it applies the PA term only to moves
with XY, so the extrude-only moves carry it themselves: they follow the
unsmoothed target position x(t) + K * v(t) of a line with the same filament
per mm (from LINE_LENGTH) and TEST_ACCEL. Constant speed is one move; each
speed change is cut into pieces of at most PUSH_PIECE s, each carrying the
filament plus the K * a * dt of its part of the ramp. Klipper starts and
ends every extrude-only move at 0 speed, so each piece's cruise speed is
picked to cover its distance in exactly its time at the (raised) extruder
accel. Klipper then smooths it all over the smoothing window, as it would a
print move. max_extrude_only_velocity / _accel, max_accel and
minimum_cruise_ratio (Klipper's lookahead caps extrude-only moves with it
too) are lifted while a train is queued, with no XY motion, and put back
right after; the motor only sees the smoothed motion.
Markers sit at the middle of each ramp, as with the klipper method.

Search (_search), MODE=fall and MODE=area (same search; fall judges by the
slow-down area alone, area by rise + fall):
  1. PA 0: reference
  2. PA_MAX, then +UPPER_STEP until the area balance is above 0; still
     below 0 at PA_LIMIT -> the test fails
  3. the crossing of the area line so far, then values beside it on the
     side that has fewer (higher one first): the first about 0.004 from it,
     the second about 0.010, never closer than 0.003 to a tested value,
     until 2 values sit within 0.015 on each side or MAX_VALUES are tested.
     PA 0 is measured once (PRIME_STEADY fills an empty nozzle first)
  4. the answer: analysis.run_result over everything measured
MODE=edge (default), at most MAX_VALUES PA values (repeats of a tested
value are free; the last value is kept for step 5):
  1. PA 0: reference (noise floor, pressure step size)
  2. upper limit: PA_MAX, then +UPPER_STEP until the force dips after the
     slow-down; no dip at PA_LIMIT -> the test fails
  3. prediction from how deep the dips are, then values just above and
     just below it (higher one first: a mixed order spreads slow drift)
  4. two values inside the remaining gap, one more above it if the line
     needs it, and a second measurement of values close to their limit
  5. a clean value close below the corner, if the repeats left none
  6. the answer: analysis.piro_pick over everything measured
DUAL=1: the speed-up (push) PA is estimated from the same pulses after the
run (analysis.dual_estimate); no extra PA values. APPLY=1 sets push and
pull with SET_DUAL_PA when [dual_pa] is installed. A [dual_pa] push / pull
is put back after the test (the test itself sets one PA with
SET_PRESSURE_ADVANCE, which sets both there).

METHOD=klipper: Klipper's own PA acts on a real line. The nozzle
runs back and forth on a short line along MOVE_AXIS with a constant amount
of filament per mm (85 % of max_extrude_cross_section); fast and slow come
from the toolhead speed, the PA value from SET_PRESSURE_ADVANCE. The turn of
the line sits inside the slow leg, after the dip window. Move times are
computed from the planned trapezoids (MINIMUM_CRUISE_RATIO=0 during the test)
so the lookahead queue is never flushed in the middle of a train. The result
is Klipper's value directly: no smooth time conversion.

APPLY=1 (auto): run() inside auto(), the result set as Klipper's PA; a poor
result, a failure or a cancel applies FAIL_PA and never raises, so a print
start goes on. REUSE_HOURS: the last auto run is reused when it is recent and
matches (filament, target temperature, wall flow, method, smooth time).
RESTRICT=0: the test runs where the nozzle is, no Z check, no moves.
PRIME_STEADY=1: after PRIME_LENGTH the prime goes on until the pushing force
stops rising (empty nozzle), at most PRIME_MAX mm."""
import collections
import logging
import math
import time
from dataclasses import asdict
from .errors import PAError
from . import analysis
from .settings import SEARCH_MAX_TRAINS

DEFAULT_AREA = math.pi * 0.875 ** 2      # 1.75 mm filament
AREA_OFFSETS = (0.004, 0.010, 0.007, 0.013, 0.015)   # from the estimate
AREA_OFFSETS_2ND = (0.010, 0.013, 0.007, 0.004, 0.015)
AREA_GAP = 0.003            # never closer than this to a tested value
PUSH_TINY = 0.000001        # Klipper PA during push: keeps its smoothing on
PUSH_PIECE = 0.001          # s, longest piece of a push ramp
PUSH_E_ACCEL = 1e7          # mm/s^2, extruder and toolhead limit while a push train is queued
PUSH_E_VELOCITY = 1e4       # mm/s, same (Klipper smooths what the motor sees)


def filament_area(ext):
    return getattr(ext, 'filament_area', None) or DEFAULT_AREA


def extruder_info(ext):
    """What the UI needs to check a test before starting it."""
    area = filament_area(ext)
    ratio = getattr(ext, 'max_extrude_ratio', None)
    return {'filament_area': round(area, 4),
            'max_cross_section': round(ratio * area, 3) if ratio else None,
            'max_e_velocity': getattr(ext, 'max_e_velocity', None),
            'max_e_dist': getattr(ext, 'max_e_dist', None),
            'smooth_time': smooth_time(ext),
            'max_extrude_ratio': ratio}


def smooth_time(ext):
    """pressure_advance_smooth_time currently in use (Klipper extruder)."""
    try:
        st = ext.get_status(0).get('smooth_time')
        return float(st) if st is not None else None
    except Exception:
        return None


class SegmentCollector:
    """Receives samples from the bus; hands out one PA window at a time."""
    def __init__(self):
        self.buf, self.latest = [], 0.0

    def on_samples(self, samples):
        self.buf.extend(samples)
        self.latest = samples[-1][0]

    def discard_before(self, t):
        self.buf = [s for s in self.buf if s[0] >= t]

    def take(self, t0, t1):
        out = [s for s in self.buf if t0 <= s[0] <= t1]
        self.buf = [s for s in self.buf if s[0] > t1]
        return out

    def negate(self):
        """The sensor's sign was just flipped: flip what is buffered too."""
        self.buf = [(t, -v) for t, v in self.buf]


def dropped(t, rate):
    """Share of samples missing in a window (gaps in the timestamps)."""
    if len(t) < 10 or not rate:
        return 0.
    dt = 1. / rate
    miss = sum(max(0., round((b - a) / dt) - 1)
               for a, b in zip(t, t[1:]) if b - a > 1.5 * dt)
    return miss / (len(t) + miss)


DROP_LIMIT = 0.05           # more samples missing than this: PA value fails


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


class _Done(Exception):
    pass


class PATestRunner:
    def __init__(self, printer, bus, store, limits):
        self.printer, self.bus, self.store = printer, bus, store
        self.limits = limits        # dict: min_temp, required_z, z_tol, timeout
        self.reactor = printer.get_reactor()
        self.gcode = printer.lookup_object('gcode')
        self.state = 'idle'         # idle | running | cancelling
        self.cancel_flag = False
        self.progress = 0.0
        self.current_pa = None
        self.phase = ''             # lead-in | fast | slow (for the UI)
        self.pa_t = None            # print_time the current PA value started
        # recent legs for the live "what the extruder was told" line:
        # [print_time start, end, flow mm^3/s, name]
        self.legs = collections.deque(maxlen=200)
        self.leg_seq = 0
        self.run_id = None
        self.started = 0.0
        self.note = ''              # search progress for the UI
        self.search = None          # live search steps for the UI
        self.dead = False           # Klipper shut down: no more G-code
        self.last = None            # answer of the last finished test
        self.values = 0             # PA values measured in this run
        self.max_values = 0

    @property
    def toolhead(self):
        # Looked up lazily: Klipper creates 'toolhead' only after all config
        # sections are loaded, so it does not exist yet inside __init__.
        return self.printer.lookup_object('toolhead')

    def cancel(self):
        if self.state == 'running':
            self.cancel_flag, self.state = True, 'cancelling'

    def on_shutdown(self):
        """Klipper shut down (emergency stop, MCU error): stop at the next
        step, and do not send G-code while cleaning up (it cannot run)."""
        if self.state != 'idle':
            self.dead = True
            self.cancel_flag = True

    def _display(self, text=None):
        """Mainsail / screen status line (never M73: that is the print's
        progress)."""
        if self.dead or self.printer.lookup_object('display_status',
                                                    None) is None:
            return              # no status line on this printer
        try:
            self._g("SET_DISPLAY_TEXT MSG=\"%s\"" % text.replace('"', "'")
                    if text else "SET_DISPLAY_TEXT")
        except Exception:
            pass

    def _g(self, script):
        self.gcode.run_script_from_command(script)

    # -- safety -------------------------------------------------------
    def precheck(self, s):
        now = self.reactor.monotonic()
        L = self.limits
        if not self.bus.available():
            raise PAError(self.bus.main.missing_text())
        st = self.toolhead.get_status(now)
        homed = st.get('homed_axes', '')
        if not all(a in homed for a in 'xyz'):
            raise PAError("Must home all axes first")
        z = self.toolhead.get_position()[2]
        free = not s.restrict
        if not free and abs(z - L['required_z']) > L['z_tol']:
            raise PAError("Z must be at required_z %.1f mm (now %.2f mm)"
                          % (L['required_z'], z))
        ext = self.toolhead.get_extruder()
        temp = ext.get_status(now).get('temperature', 0.)
        if temp < L['min_temp']:
            raise PAError("Extruder %.0f °C below min_temp %.0f °C"
                          % (temp, L['min_temp']))
        area = filament_area(ext)
        used = s.filament_mm(area)
        if used > s.max_filament:
            raise PAError("Test needs up to %.0f mm of filament, MAX_FILAMENT "
                          "is %.0f mm. Lower CYCLES, FAST_TIME or FAST_FLOW, "
                          "or raise MAX_FILAMENT"
                          % (used, s.max_filament))
        vmax = getattr(ext, 'max_e_velocity', None)
        sm = self.window(ext, s)
        flows = (('FAST_FLOW', s.fast_flow), ('SLOW_FLOW', s.slow_flow),
                 ('PRIME_FLOW', s.prime_flow))
        if s.method == 'klipper':
            flows = flows[2:]                # only the prime is extrude-only
            mr = getattr(ext, 'max_extrude_ratio', None)
            ln = s.line(area, mr, sm)
            if s.line_length > 0:
                lim = 0.95 * (mr or 0.64 / area)
                if ln['ratio'] > lim:
                    raise PAError("LINE_LENGTH %.1f mm is too short: the line would "
                                  "be %.2f mm2, above max_extrude_cross_section. "
                                  "Use at least %.1f mm"
                                  % (s.line_length, ln['cross_section'],
                                     s.line_length * ln['ratio'] / lim + 0.05))
            vmx = st.get('max_velocity')
            if vmx is not None and ln['v_fast'] > vmx:
                raise PAError("Klipper method needs %.0f mm/s toolhead speed, "
                              "above max_velocity %.0f. Lower FAST_FLOW"
                              % (ln['v_fast'], vmx))
            lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
            if lo is not None:
                i = 'XY'.index(s.move_axis)
                _, a, b = self._line_ends(s, ln)
                if free:        # slide the line inside the axis limits
                    shift = max(0., lo[i] + 0.5 - min(a, b)) - \
                        max(0., max(a, b) - (hi[i] - 0.5))
                    if i == 0:
                        s.test_x += shift
                    else:
                        s.test_y += shift
                    a, b = a + shift, b + shift
                if min(a, b) < lo[i] or max(a, b) > hi[i]:
                    raise PAError("Klipper method line (%.1f mm along %s) does "
                                  "not fit around the test position"
                                  % (ln['length'], s.move_axis))
        for name, flow in flows:
            if vmax is not None and flow / area > vmax:
                raise PAError("%s %.1f mm3/s needs %.1f mm/s, above "
                              "max_extrude_only_velocity %.1f"
                              % (name, flow, flow / area, vmax))
        if s.method == 'push':
            pp = self.push_plan(s, area, ext, st)
            if pp['ramp'] > 0.3 * min(s.fast_time, s.slow_time):
                raise PAError("Push: a speed change at TEST_ACCEL %.0f takes "
                              "%.0f ms, too long for FAST_TIME / SLOW_TIME. "
                              "Raise TEST_ACCEL or shorten LINE_LENGTH"
                              % (pp['accel'], 1000. * pp['ramp']))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None and not free:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside axis limits" % name)
        return temp, area

    def push_plan(self, s, area, ext, st=None):
        """The line the push method copies: accel = TEST_ACCEL, never above
        the printer's max_accel."""
        st = st or self.toolhead.get_status(self.reactor.monotonic())
        acc = min(s.test_accel, st.get('max_accel') or s.test_accel)
        return s.push_plan(area, getattr(ext, 'max_extrude_ratio', None),
                           self.window(ext, s), acc)

    @staticmethod
    def window(ext, s=None):
        """Smoothing window: SMOOTH_TIME if set, else Klipper's
        pressure_advance_smooth_time (clamped 0.01 .. 0.1 s like OznLab)."""
        if s is not None and s.smooth_time:
            return s.smooth_time
        st = smooth_time(ext)
        return min(max(st if st else 0.04, 0.01), 0.1)

    # -- main ---------------------------------------------------------
    def run(self, gcmd, s):
        if self.state != 'idle':
            raise PAError("Test already running")
        if not s.restrict:              # where the nozzle is now
            pos = self.toolhead.get_position()
            s.test_x = s.park_x = round(pos[0], 3)
            s.test_y = s.park_y = round(pos[1], 3)
        temp, area = self.precheck(s)
        self.state, self.cancel_flag, self.progress = 'running', False, 0.0
        self.dead, self.values = False, 0
        self.max_values = s.max_values      # repeats are not counted
        self.started = self.reactor.monotonic()
        now = self.reactor.monotonic()
        ext = self.toolhead.get_extruder()
        est = ext.get_status(now)
        orig_pa = est.get('pressure_advance', 0.)
        orig_sm = est.get('smooth_time')
        # [dual_pa]: the test sets one PA (SET_PRESSURE_ADVANCE sets push =
        # pull there); its push and pull are put back after the test
        self.orig_dual = None
        dpa = self._dual_pa()
        if dpa is not None:
            try:
                d = dpa.get_status(now)
                self.orig_dual = (d['push_pressure_advance'],
                                  d['pull_pressure_advance'])
            except Exception:
                self.orig_dual = None
        fast_mm, slow_mm = s.leg_mm(area)
        sm = self.window(ext, s)
        klip = s.method == 'klipper'
        ln = s.line(area, getattr(ext, 'max_extrude_ratio', None), sm) \
            if klip else None
        tst = self.toolhead.get_status(now)
        self.pp = None if klip else self.push_plan(s, area, ext, tst)
        self.e_lim = (getattr(ext, 'max_e_velocity', None),
                      getattr(ext, 'max_e_accel', None))
        orig_lim = {k: tst.get(k) for k in ('max_accel', 'minimum_cruise_ratio',
                                            'max_accel_to_decel')}
        self.note, self.used, self.search = '', 0.0, None
        self.orig_lim = orig_lim
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'method': 'PIRO-EDGE',
            'filament_area': round(area, 4),
            'leg_mm': {'fast': round(fast_mm, 4), 'slow': round(slow_mm, 4)},
            'filament_mm': round(s.filament_mm(area), 1),
            'smooth_time': sm, 'pa_method': s.method,
            'line': ({k: round(v, 4) for k, v in ln.items()}
                     if ln else None),
            'push': ({k: round(v, 5) for k, v in self.pp.items()}
                     if self.pp else None),
            'analysis': {'skip_first': bool(s.skip_first)},
            'sensor': self.bus.sensor, 'primary': self.bus.main.kind,
            'unit': self.bus.unit})
        col = SegmentCollector()
        col2 = SegmentCollector() if self.bus.other is not None else None
        self.col2 = None
        status, err, saved = 'complete', '', False
        try:
            self.bus.subscribe(col.on_samples)
            self.bus.set_mode('pa')       # Eddy: PA-test sample rate
            if col2 is not None:
                try:
                    self.bus.subscribe(col2.on_samples, self.bus.other.kind)
                    self.col2 = col2
                except PAError as e:
                    gcmd.respond_info("PIRO-EDGE: second sensor not used: %s"
                                      % e)
            self._health_line(gcmd)
            self._display("PA test: priming")
            # 100 % speed and flow so each leg lasts exactly its planned time
            self._g("SAVE_GCODE_STATE NAME=PA_LC\nG90\nM83\nM220 S100\nM221 S100")
            saved = True
            if s.restrict:
                self._g("G1 X%.2f Y%.2f F6000" % (s.test_x, s.test_y))
            k0 = 0. if klip else PUSH_TINY   # push: smoothing on, PA by hand
            if s.smooth_time:
                self._g("SET_PRESSURE_ADVANCE ADVANCE=%.6f SMOOTH_TIME=%.4f"
                        % (k0, sm))
            self._set_klipper_pa(k0)
            if klip:
                self._set_limits(s, orig_lim)
            self._prime(s, area, ext, col, gcmd)
            if self.prime_info:
                self.store.update_meta(self.run_id, {'prime': self.prime_info})
            if s.prime_rest > 0:
                self.toolhead.dwell(s.prime_rest)
            if klip:
                self._motion_check(col, s, ln, gcmd)
            self._search((col, s, area, sm, ln), gcmd)
            if self.cancel_flag:
                status = 'cancelled'
        except Exception as e:
            status, err = 'error', str(e)
            if self.dead:
                status, err = 'error', 'Klipper shut down during the test'
            gcmd.respond_info("PA_LOADCELL_TEST aborted: %s" % err)
        finally:
            self._cleanup(s, orig_pa, saved, col, orig_sm, orig_lim)
            self.state = 'analysing'
            self.note = 'final analysis'
            try:
                meta = self.store.finalize(self.run_id, status, err)
            except Exception as e:
                logging.exception("pa_loadcell: finalize failed")
                meta = {'segments': [], 'analysis': {}}
                status, err = 'error', err or str(e)
            # the answer is stored (run, search, last) before the state goes
            # back to idle, so every viewer that sees 'idle' sees the result
            res = (meta.get('analysis') or {}).get('result') or {}
            dual = res.get('dual') or {}
            self.last = {'run': self.run_id, 'status': status,
                         'pa': res.get('pa'), 'unc': res.get('unc'),
                         'grade': res.get('grade'), 'method': res.get('method'),
                         'why': res.get('why') or err,
                         'push': dual.get('push'),
                         'push_unc': dual.get('push_unc'),
                         'push_grade': dual.get('push_grade'),
                         'dual': s.dual}
            if self.search is not None:
                self.search.update(result=res.get('pa'), unc=res.get('unc'),
                                   grade=res.get('grade'),
                                   push=dual.get('push') if s.dual else None,
                                   push_unc=dual.get('push_unc'),
                                   push_grade=dual.get('push_grade'),
                                   error=None if res.get('pa') is not None
                                   else (err or res.get('why') or status))
                try:
                    self.store.set_search(self.run_id, self.search)
                except Exception:
                    pass
            self.state, self.current_pa, self.phase = 'idle', None, ''
            self.pa_t, self.note = None, ''
            self._display(None)
        gcmd.respond_info("Run %d %s: %d PA values%s"
                          % (self.run_id, status, len(meta['segments']),
                             (", %s PA %.4f +- %.4f (%s)"
                              % (res.get('method', 'PIRO-EDGE'), res['pa'],
                                 res['unc'], res['grade']))
                             if res.get('pa') is not None else ''))
        dual = res.get('dual') or {}
        if s.dual and res.get('pa') is not None and s.mode != 'area':
            gcmd.respond_info(
                ("  dual: pull %.4f, push %.4f +- %.4f (%s, from the %s)"
                 % (res['pa'], dual['push'], dual['push_unc'],
                    dual['push_grade'], dual.get('source')))
                if dual.get('push') is not None else
                "  dual: no push value (%s), push = pull"
                % (dual.get('why') or 'no speed-up result'))
        for name, r in ((meta.get('analysis') or {}).get('channels') or
                        {}).items():
            gcmd.respond_info("  %s (compared, not applied): %s"
                              % (name, ("PA %.4f +- %.4f (%s)"
                                        % (r['pa'], r['unc'], r['grade']))
                                 if r.get('pa') is not None else
                                 (r.get('why') or 'no result')))
        if status == 'error':
            raise PAError(err)
        return self.run_id

    # -- auto PA (APPLY=1) ----------------------------------------------
    def auto(self, gcmd, s):
        """Test (or reuse) and set the result as Klipper's PA. Never raises:
        a print start must go on, with FAIL_PA when there is no good value.
        DUAL=1 with [dual_pa] installed: push and pull with SET_DUAL_PA."""
        now = self.reactor.monotonic()
        try:
            ext = self.toolhead.get_extruder()
            target = float(ext.get_status(now).get('target', 0.) or 0.)
            sm = self.window(ext, s)
        except Exception:
            target, sm = 0., s.smooth_time or 0.04
        hit = None
        if s.reuse_hours > 0:
            try:
                hit = self._reuse(s, target, sm)
            except Exception:
                hit = None
        if hit:
            rid, a = hit
            push = a.get('push') if s.dual else None
            done = self._apply(a['applied'], push)
            try:
                self.store.update_auto(rid, {'reused': int(a.get('reused', 0)) + 1,
                                             'last_used': time.time()})
            except Exception:
                pass
            gcmd.respond_info(
                "PIRO-EDGE: %s reused from Run %d (%s, %.0f °C, %.1f mm³/s, "
                "%.0f min ago)" % (self._applied_text(a['applied'], push, done),
                                   rid, a.get('filament') or '-',
                                   a.get('target_temp', 0), a.get('wall_flow', 0),
                                   (time.time() - a.get('time', 0)) / 60.))
            return a['applied']
        if self.state != 'idle':
            self.fallback(gcmd, s.fail_pa, "a test is already running")
            return s.fail_pa
        before, rid, reason, res = self.run_id, None, '', {}
        try:
            rid = self.run(gcmd, s)
        except Exception as e:
            reason = str(e)
            rid = self.run_id if self.run_id != before else None
        if rid is not None:
            try:
                m = self.store.get_run(rid)
                res = (m.get('analysis') or {}).get('result') or {}
                if not reason and m.get('status') != 'complete':
                    reason = m.get('status') or 'no result'
            except Exception as e:
                reason = reason or str(e)
        good = not reason and res.get('pa') is not None \
            and res.get('grade') in ('good', 'fair')
        if not good and not reason:
            reason = ("grade %s" % res['grade']) if res.get('pa') is not None \
                else "no result"
        value = float(res['pa']) if good else s.fail_pa
        dual = res.get('dual') or {}
        push = None
        if good and s.dual and dual.get('push') is not None and \
                dual.get('push_grade') in ('good', 'fair'):
            push = float(dual['push'])
        done = self._apply(value, push)
        if rid is not None:
            try:
                self.store.update_auto(rid, {
                    'applied': round(value, 4), 'fallback': not good,
                    'push': round(push, 4) if push is not None and done
                    else None,
                    'reason': reason, 'time': time.time(),
                    'filament': s.filament, 'target_temp': round(target, 1),
                    'wall_flow': round(s.fast_flow, 3), 'pa_method': s.method,
                    'mode': s.mode,
                    'smooth': round(sm, 4), 'reused': 0})
            except Exception:
                pass
        if good:
            gcmd.respond_info("PIRO-EDGE: %s applied (%s, +- %.4f)"
                              % (self._applied_text(value, push, done),
                                 res['grade'], res.get('unc') or 0))
            if s.dual and push is None and s.mode != 'area':
                why = dual.get('why') or 'no speed-up result'
                if dual.get('push') is not None:
                    why = 'push %.4f graded %s' % (dual['push'],
                                                   dual.get('push_grade'))
                gcmd.respond_info("PIRO-EDGE: push = pull (%s)" % why)
        else:
            gcmd.respond_info(("PIRO-EDGE: test cancelled, PA %.4f applied" % value)
                              if reason == 'cancelled' else
                              "PIRO-EDGE: test failed (%s), PA %.4f applied"
                              % (reason, value))
        return value

    @staticmethod
    def _applied_text(pull, push, done):
        if push is None:
            return "PA %.4f" % pull
        if done:
            return "pull %.4f, push %.4f" % (pull, push)
        return ("PA %.4f (push %.4f not set: [dual_pa] is not installed)"
                % (pull, push))

    def fallback(self, gcmd, value, reason):
        """APPLY=1 with settings that cannot run: FAIL_PA, no error."""
        self._apply(value)
        gcmd.respond_info("PIRO-EDGE: test failed (%s), PA %.4f applied"
                          % (reason, value))

    def _dual_pa(self):
        """The [dual_pa] object of the active extruder, or None."""
        try:
            name = self.toolhead.get_extruder().get_name()
        except Exception:
            name = 'extruder'
        obj = self.printer.lookup_object(
            'dual_pa' if name == 'extruder' else 'dual_pa ' + name, None)
        return obj if obj is not None and hasattr(obj, 'get_status') else None

    def _dual_cmd(self, push, pull):
        try:
            name = self.toolhead.get_extruder().get_name()
        except Exception:
            name = 'extruder'
        return "SET_DUAL_PA EXTRUDER=%s PUSH=%.5f PULL=%.5f" % (name, push, pull)

    def _apply(self, value, push=None):
        """Set the PA. push: the speed-up PA, set with [dual_pa] (if it is
        not installed only value is set). -> True when push was set."""
        try:
            self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f" % value)
        except Exception:
            pass
        if push is None or self._dual_pa() is None:
            return False
        try:
            self._g(self._dual_cmd(push, value))
            return True
        except Exception:
            return False

    def _reuse(self, s, target, sm):
        """The last auto run, when recent and made under the same
        conditions. -> (run id, auto dict) or None"""
        rid, m = self.store.last_auto()
        if rid is None:
            return None
        a = m['auto']
        name = lambda x: ' '.join(str(x or '').lower().split())
        if a.get('fallback') or a.get('applied') is None or not name(s.filament):
            return None
        if time.time() - float(a.get('time', 0)) > s.reuse_hours * 3600.:
            return None
        if name(a.get('filament')) != name(s.filament):
            return None
        if abs(float(a.get('target_temp', -999)) - target) > 2.:
            return None
        wf = float(a.get('wall_flow', 0))
        if abs(wf - s.fast_flow) > 0.05 * s.fast_flow:
            return None
        if a.get('pa_method') != s.method or a.get('mode', 'edge') != s.mode or \
                abs(float(a.get('smooth', 0)) - sm) > 1e-4:
            return None
        return rid, a

    def _log_leg(self, t0, t1, flow, name):
        self.leg_seq += 1
        self.legs.append((self.leg_seq, round(t0, 4), round(t1, 4),
                          flow, name))

    def legs_since(self, seq):
        return [list(x) for x in self.legs if x[0] > seq]

    def _prime(self, s, area, ext, col=None, gcmd=None):
        """Push PRIME_LENGTH mm at PRIME_FLOW, extrude-only (PA does not act
        and does not matter here), split into pieces that respect
        max_extrude_only_distance. With PRIME_STEADY=1 it then keeps
        pushing in 1 s chunks, watching the load cell, until the pushing
        force stops rising (two chunks in a row within 3 % of the last, or
        the noise), at most PRIME_MAX mm in all: an empty nozzle fills up
        before PA 0. The first PA value starts right after."""
        self.prime_mm, self.prime_info = 0., None
        steady = bool(s.prime_steady) and col is not None
        # Eddy channels (main and / or the second sensor): their sign is
        # checked on the first prime piece, rest -> pushing must go up
        signs = []
        if col is not None and self.bus.main.kind == 'eddy':
            signs.append((self.bus.main, col))
        o, c2 = self.bus.other, getattr(self, 'col2', None)
        if o is not None and o.kind == 'eddy' and c2 is not None:
            signs.append((o, c2))
        if s.prime_length <= 0 and not steady:
            if signs and gcmd is not None:
                gcmd.respond_info("PIRO-EDGE: no prime, Eddy sign not checked "
                                  "(using the saved one)")
            return
        self.phase = 'prime'
        th = self.toolhead
        v = s.prime_flow / area                       # mm/s of filament
        limit = getattr(ext, 'max_e_dist', None) or 50.
        # pieces of at most ~2 s, so a cancel is seen within a few seconds
        piece = max(1., min(limit * 0.9, 25., 2. * v))
        chunk = min(max(2., v * 1.0), piece)          # about 1 s of filament
        if steady or signs:         # force at rest: the zero for the levels
            b0 = th.get_last_move_time()
            th.dwell(0.4)
            b1 = th.get_last_move_time()

        def push(e):
            t0 = th.get_last_move_time()
            self._g("G1 E%.5f F%.4f" % (e, v * 60.))
            t1 = th.get_last_move_time()
            self._log_leg(t0, t1, s.prime_flow, 'prime')
            self.prime_mm += e
            return t0, t1
        # equal pieces (Klipper stops between extrude-only moves, so no tiny
        # last piece); with an Eddy and no prime length: one ~1 s piece
        left = s.prime_length if s.prime_length > 0 else (chunk if signs else 0.)
        n = int(math.ceil(left / piece - 1e-9)) if left > 1e-6 else 0
        base = None
        for i in range(n):
            if self.cancel_flag:
                break
            t0, t1 = push(left / n)
            if i == 0 and signs:
                base = self._eddy_signs(signs, col, b0, b1, t0, t1, gcmd)
        if not steady or self.cancel_flag:
            return
        if base is None:
            smp = self._collect(col, b0 + 0.1, b1)
            base = sum(x[1] for x in smp) / len(smp)
        levels, done = [], False
        pend = [push(chunk)] if self.prime_mm + chunk <= s.prime_max + 1e-6 \
            else []
        while pend and not self.cancel_flag:
            if self.prime_mm + chunk <= s.prime_max + 1e-6:
                pend.append(push(chunk))              # keep the extruder busy
            t0, t1 = pend.pop(0)
            smp = self._collect(col, t0 + 0.3 * (t1 - t0), t1)
            f = [x[1] for x in smp]
            m = sum(f) / len(f)
            sd = math.sqrt(sum((x - m) ** 2 for x in f) / max(1, len(f) - 1))
            levels.append(abs(m - base))
            tol = max(0.03 * levels[-1], 3. * sd / math.sqrt(len(f)))
            if len(levels) >= 3 and all(
                    abs(levels[-i] - levels[-i - 1]) <= tol for i in (1, 2)):
                done = True
                break
        self.prime_info = {'mm': round(self.prime_mm, 1), 'steady': done,
                           'levels': [round(x, 1) for x in levels],
                           'sign': self.bus.main.sign}
        if gcmd is not None:
            gcmd.respond_info(
                "PIRO-EDGE: prime %.0f mm, %s" % (
                    self.prime_mm, "pressure steady" if done else
                    "PRIME_MAX reached, pressure still rising"))

    def _eddy_signs(self, items, col, b0, b1, t0, t1, gcmd):
        """Rest (b0..b1) -> the first prime piece (t0..t1): pushing must read
        higher. If it reads lower, flip that Eddy's sign (and what is
        buffered), remembered for the next runs. Too small a change: the
        main sensor stops the test, a second sensor only gets a warning.
        -> the main channel's rest level (after any flip) or None"""
        out = None
        for ch, c in items:
            rest = [x[1] for x in self._collect(c, b0 + 0.1, b1)]
            f = [x[1] for x in self._collect(c, t0 + 0.5 * (t1 - t0), t1)]
            base, m = sum(rest) / len(rest), sum(f) / len(f)
            d = m - base
            noise = math.sqrt(sum((x - m) ** 2 for x in f) / max(1, len(f) - 1))
            main = ch is self.bus.main
            if abs(d) < 3. * noise or abs(d) < 0.5:
                msg = ("Eddy reading barely changes when pushing (%.1f Hz). "
                       "Check the Eddy mount and its target" % d)
                if main:
                    raise PAError(msg)
                if gcmd is not None:
                    gcmd.respond_info("PIRO-EDGE: second sensor: " + msg)
                continue
            if d < 0:
                ch.flip_sign()
                self.bus.save_state()
                c.negate()
                base = -base
                if gcmd is not None:
                    gcmd.respond_info("PIRO-EDGE: Eddy reads lower with "
                                      "pressure, sign flipped (remembered)")
            if main:
                out = base
        return out

    def _health_line(self, gcmd):
        """One line at the start: sensor(s), rate, errors so far."""
        parts = []
        for ch in self.bus.channels:
            i = ch.info()
            parts.append("%s %s %.0f/s, unit %s%s%s" % (
                i['label'], i['source'] or '-', i['rate'], i['unit'],
                ", sign %+d" % i['sign'] if ch.kind == 'eddy' else '',
                ", %d errors" % i['errors'] if i['errors'] else ''))
        gcmd.respond_info("PIRO-EDGE: " + ("; ".join(parts)) +
                          (" (judged by %s)" % self.bus.main.info()['label']
                           if len(parts) > 1 else ''))

    def _set_klipper_pa(self, value):
        self._g("SET_PRESSURE_ADVANCE ADVANCE=%.6f" % value)

    def _push_accel(self, raised):
        """Klipper's lookahead caps every move, extrude-only ones too, at
        the speed a move of that length reaches at max_accel x (1 -
        minimum_cruise_ratio). While a push train is queued (no XY motion)
        both are lifted so each piece lasts exactly its time."""
        orig = self.orig_lim or {}
        if orig.get('max_accel') is None:
            return
        if not raised:
            self._restore_limits(orig)
        elif orig.get('minimum_cruise_ratio') is not None:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.0f MINIMUM_CRUISE_RATIO=0"
                    % PUSH_E_ACCEL)
        else:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.0f ACCEL_TO_DECEL=%.0f"
                    % (PUSH_E_ACCEL, PUSH_E_ACCEL))

    def _e_limits(self, ext, raised):
        """Raise max_extrude_only_velocity / _accel for a push train (the
        motor sees Klipper's smoothed motion) or put them back."""
        v, a = (PUSH_E_VELOCITY, PUSH_E_ACCEL) if raised else self.e_lim
        if v is not None and hasattr(ext, 'max_e_velocity'):
            ext.max_e_velocity = v
        if a is not None and hasattr(ext, 'max_e_accel'):
            ext.max_e_accel = a

    def _measure(self, ctx, pa, stage=None, repeat=False):
        """Run one PA value and save it. Returns (segment index, score)."""
        col, s, area, sm, ln = ctx
        th = self.toolhead
        fil = s.per_pa_mm(area)
        if self.used + fil > s.max_filament - getattr(self, 'prime_mm', s.prime_length) + 1e-6:
            raise PAError("Stopped at MAX_FILAMENT %.0f mm" % s.max_filament)
        self.used += fil
        self.current_pa = pa
        if not repeat:
            self.values += 1
        self._display("PA test %d/%d: PA %.4f%s" % (
            self.values, self.max_values, pa, " (repeat)" if repeat else ""))
        if ln:
            self._set_klipper_pa(pa)          # Klipper's own PA from here on
            self._line_start(s, ln)
        if s.rest_time > 0:
            th.dwell(s.rest_time)
        t_base = th.get_last_move_time()      # recording starts here
        self.pa_t = t_base
        col.discard_before(t_base - 0.05)
        if self.col2 is not None:
            self.col2.discard_before(t_base - 0.05)
        h0 = self.bus.main.health()
        if ln:
            markers, partial = self._train_klipper(s, ln, area, t_base)
        else:
            ext = th.get_extruder()
            self._e_limits(ext, True)
            self._push_accel(True)
            try:
                markers, partial = self._train_push(pa, s, self.pp, t_base)
            finally:
                self._push_accel(False)
                self._e_limits(ext, False)
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not any(m['name'] == 'fast' for m in markers):
            return None, None
        h1 = self.bus.main.health()
        miss = dropped([x[0] for x in samples], self.bus.rate)
        if miss > DROP_LIMIT:
            raise PAError("%s dropped %.0f %% of its samples at PA %.4f. "
                          "Check its wiring / CAN bus load"
                          % (self.bus.main.info()['label'], 100. * miss, pa))
        seg = {'pa': pa, 'unit': self.bus.unit, 'sensor': self.bus.main.kind,
               'rate': round(self.bus.rate, 1), 'markers': markers,
               'partial': partial,
               'method': 'klipper' if ln else 'push',
               'flows': {'fast': s.fast_flow, 'slow': s.slow_flow},
               't': [round(t - t_base, 4) for t, _ in samples],
               'f': [round(v, 3) for _, v in samples]}
        lost = {k: h1[k] - h0[k] for k in h0 if h1[k] > h0[k]}
        if lost or miss > 0.005:
            seg['dropped'] = dict(lost, missing=round(miss, 4))
        if self.col2 is not None:
            try:
                s2 = self._collect(self.col2, t_base, t_end, wait=1.0)
                o = self.bus.other
                seg['channels'] = {o.kind: {
                    'unit': o.unit, 'rate': round(o.rate, 1),
                    't': [round(t - t_base, 4) for t, _ in s2],
                    'f': [round(v, 3) for _, v in s2]}}
            except PAError:
                pass                      # second sensor late: main only
        if stage:
            seg['stage'] = stage
        if repeat:
            seg['repeat'] = True
        idx = self.store.add_segment(self.run_id, seg)
        if partial:
            return idx, None
        return idx, analysis.edge_score(seg['t'], seg['f'], markers, sm,
                                        skip_first=bool(s.skip_first),
                                        area_window=s.area_window)

    def _train_push(self, pa, s, pp, t_base):
        """One push train: a ramp from rest to slow, a slow lead-in, then
        (ramp up, fast, ramp down, slow) x CYCLES and a ramp back to rest,
        all extrude-only, following x(t) + pa * v(t) (see the top of this
        file). Fast and slow legs last FAST_TIME / SLOW_TIME from the middle
        of one ramp to the middle of the next."""
        th = self.toolhead
        ext = th.get_extruder()
        e_max = 0.9 * (getattr(ext, 'max_e_dist', None) or 50.)
        ea = PUSH_E_ACCEL
        vs, vf, r = pp['v_slow'], pp['v_fast'], pp['ramp']
        acc = pp['e_accel']
        pos = {'want': 0., 'sent': 0.}       # filament mm: target / commanded
        rel = lambda x: round(x - t_base, 4)

        def piece(d, T, out):
            """d mm in exactly T s; Klipper ramps it from and to 0 speed."""
            pos['want'] += d
            e = round(pos['want'] - pos['sent'], 5)
            if abs(e) < 1e-5:
                out.append("G4 P%.4f" % (T * 1000.))
                return
            disc = ea * ea * T * T - 4. * ea * abs(e)
            if disc <= 0:
                raise PAError("Push piece too fast (%.3f mm in %.1f ms)"
                              % (e, T * 1000.))
            vc = (ea * T - math.sqrt(disc)) / 2.
            pos['sent'] += e
            out.append("G1 E%.5f F%.5f" % (e, vc * 60.))

        def flat(v, T):
            out, n = [], max(1, int(math.ceil(v * T / e_max)))
            for _ in range(n):
                piece(v * T / n, T / n, out)
            self._g("\n".join(out))

        def ramp(v0, v1):
            """v0 -> v1 at the line's filament accel; returns its middle."""
            dur = abs(v1 - v0) / acc
            a = (v1 - v0) / dur
            n = max(2, int(math.ceil(dur / PUSH_PIECE)))
            dt, out = dur / n, []
            for i in range(n):
                t0, t1 = i * dt, (i + 1) * dt
                piece(v0 * dt + 0.5 * a * (t1 * t1 - t0 * t0) + pa * a * dt,
                      dt, out)
            t_a = th.get_last_move_time()
            self._g("\n".join(out))
            return t_a + dur / 2.

        markers, partial = [], False
        self.phase = 'lead-in'
        t0 = th.get_last_move_time()
        ramp(0., vs)
        flat(vs, s.slow_time - r / 2.)
        for k in range(s.cycles):
            if self.cancel_flag:
                partial = True
                break
            self.phase = 'fast'
            up = ramp(vs, vf)
            if k == 0:
                markers.append({'name': 'slow', 'sweep': -1, 't0': rel(t0),
                                't1': rel(up)})
                self._log_leg(t0, up, s.slow_flow, 'slow')
            flat(vf, s.fast_time - r)
            self.phase = 'slow'
            down = ramp(vf, vs)
            last = k == s.cycles - 1
            flat(vs, s.slow_time - (r / 2. if last else r))
            end = th.get_last_move_time() + (0. if last else r / 2.)
            markers.append({'name': 'fast', 'sweep': k, 't0': rel(up),
                            't1': rel(down)})
            markers.append({'name': 'slow', 'sweep': k, 't0': rel(down),
                            't1': rel(end)})
            self._log_leg(up, down, s.fast_flow, 'fast')
            self._log_leg(down, end, s.slow_flow, 'slow')
        ramp(vs, 0.)                          # back to rest, PA charge out
        return markers, partial

    # -- klipper method -------------------------------------------------
    def _set_limits(self, s, orig):
        """Test accel (never above the current limit) and no cruise ratio,
        so a speed change is one short ramp at the start / end of a leg."""
        acc = min(s.test_accel, orig.get('max_accel') or s.test_accel)
        self.accel = acc
        if orig.get('minimum_cruise_ratio') is not None:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f MINIMUM_CRUISE_RATIO=0" % acc)
        else:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f ACCEL_TO_DECEL=%.1f"
                    % (acc, acc))

    def _restore_limits(self, orig):
        if orig.get('max_accel') is None:
            return
        if orig.get('minimum_cruise_ratio') is not None:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f MINIMUM_CRUISE_RATIO=%.3f"
                    % (orig['max_accel'], orig['minimum_cruise_ratio']))
        elif orig.get('max_accel_to_decel') is not None:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f ACCEL_TO_DECEL=%.1f"
                    % (orig['max_accel'], orig['max_accel_to_decel']))
        else:
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f" % orig['max_accel'])

    @staticmethod
    def _line_ends(s, ln):
        """(axis, start, end) of the line: centred on the test position, or
        (LINE_DIR = 1 / -1) starting there and running towards + / -."""
        i = 'XY'.index(s.move_axis)
        c = (s.test_x, s.test_y)[i]
        if s.line_dir:
            return s.move_axis, c, c + s.line_dir * ln['length']
        return s.move_axis, c - ln['length'] / 2, c + ln['length'] / 2

    def _line_start(self, s, ln):
        axis, a, b = self._line_ends(s, ln)
        x, y = (a, s.test_y) if axis == 'X' else (s.test_x, a)
        self._g("G1 X%.3f Y%.3f F6000" % (x, y))

    @staticmethod
    def plan_line(s, ln, accel):
        """The moves of one train on the line: list of
        (name, distance, cruise_v, start_v, end_v) and the leg times.
        Junctions: same direction -> the lower cruise speed, turn -> 0."""
        vf, vs = ln['v_fast'], ln['v_slow']
        seq = [('lead', ln['d_before'], vs, 0., vs)]
        for k in range(s.cycles):
            seq.append(('fast', ln['d_fast'], vf, vs, vs))
            seq.append(('after', ln['d_after'], vs, vs, 0.))       # turn
            seq.append(('before', ln['d_before'], vs, 0.,
                        vs if k < s.cycles - 1 else 0.))
        out, t = [], 0.
        for name, d, vc, v0, v1 in seq:
            ta, tdc = (vc - v0) / accel, (vc - v1) / accel
            da, dd = (vc * vc - v0 * v0) / (2 * accel), \
                (vc * vc - v1 * v1) / (2 * accel)
            tc = max(0., d - da - dd) / vc
            out.append({'name': name, 'd': d, 'v': vc, 't0': t,
                        'ta': ta, 'tc': tc, 'td': tdc,
                        't1': t + ta + tc + tdc})
            t += ta + tc + tdc
        return out

    def _train_klipper(self, s, ln, area, t_base):
        """Queue one train on the line without flushing the lookahead, then
        markers from the planned trapezoids (speed changes at the middle of
        each ramp)."""
        axis, a, b = self._line_ends(s, ln)
        plan = self.plan_line(s, ln, self.accel)
        rel = lambda x: round(x, 4)
        pos, sign, partial = a, (1. if b >= a else -1.), False
        self.phase = 'lead-in'
        for i, mv in enumerate(plan):
            if self.cancel_flag and mv['name'] == 'fast':
                partial = True
                plan = plan[:i]
                break
            pos += sign * mv['d']
            self._g("G1 %s%.4f E%.5f F%.2f" % (axis, pos, mv['d'] * ln['ratio'],
                                               mv['v'] * 60.))
            if mv['name'] == 'after':
                sign = -sign
        markers, first = [], plan[0]
        ups = [m['t0'] + m['ta'] / 2 for m in plan if m['name'] == 'fast']
        downs = [m['t1'] - m['td'] / 2 for m in plan if m['name'] == 'fast']
        end = plan[-1]['t1'] if plan else 0.
        markers.append({'name': 'slow', 'sweep': -1, 't0': rel(first['t0']),
                        't1': rel(ups[0] if ups else end)})
        for k, (u, d) in enumerate(zip(ups, downs)):
            nxt = ups[k + 1] if k + 1 < len(ups) else end
            markers.append({'name': 'fast', 'sweep': k, 't0': rel(u), 't1': rel(d)})
            markers.append({'name': 'slow', 'sweep': k, 't0': rel(d), 't1': rel(nxt)})
            self._log_leg(t_base + u, t_base + d, s.fast_flow, 'fast')
            self._log_leg(t_base + d, t_base + nxt, s.slow_flow, 'slow')
        self.phase = 'fast'
        return markers, partial

    def _motion_check(self, col, s, ln, gcmd):
        """One empty pass along the line: warn when moving makes the load
        cell much noisier than standing still."""
        th = self.toolhead
        axis, a, b = self._line_ends(s, ln)
        self._line_start(s, ln)
        th.dwell(0.6)
        t0 = th.get_last_move_time()
        th.dwell(0.6)
        t1 = th.get_last_move_time()
        x, y = (b, s.test_y) if axis == 'X' else (s.test_x, b)
        self._g("G1 X%.3f Y%.3f F%.1f" % (x, y, ln['v_fast'] * 60.))
        t2 = th.get_last_move_time()
        th.wait_moves()
        try:
            smp = self._collect(col, t0, t2)
        except PAError:
            return
        t = [p[0] for p in smp]
        f = [p[1] for p in smp]
        still = analysis._detrended_sigma(t, f, t0 + 0.05, t1 - 0.05)
        moving = analysis._detrended_sigma(t, f, t1 + 0.1, t2 - 0.1)
        self.motion_noise = (still, moving)
        if still and moving and moving > 2 * still:
            gcmd.respond_info("PIRO-EDGE: load cell noise while moving is "
                              "%.1fx the still noise. Results may be less "
                              "certain" % (moving / still))

    # -- PIRO-EDGE adaptive search -------------------------------------
    def _search(self, ctx, gcmd):
        col, s, area, sm, ln = ctx
        steps, segs = [], []
        self.search = {'steps': steps, 'result': None}
        area_mode = s.mode in ('area', 'fall')
        typical = s.typical()
        snap = lambda x: max(0.0005, round(x * 2000.) / 2000.)
        st = {'floor': 0.}

        def say(text):
            self.note = text
            gcmd.respond_info("PIRO-EDGE: " + text)

        def point(k):
            return [p for p in analysis.pool(segs)
                    if abs(p['pa'] - k) < 1e-6][0]

        def too_high(p):
            return p['pa'] > 0 and -p['dip'] > max(p['limit'], st['floor'])

        def distinct():
            return len({round(g['pa'], 6) for g in segs})

        def measure(k, stage, repeat=False, pred=None):
            if self.cancel_flag:
                raise _Cancelled()
            if len(segs) >= SEARCH_MAX_TRAINS:
                raise _Budget()
            k = round(k, 5)
            # edge: MAX_VALUES PA values; a repeat of a tested value is
            # free (area and fall stop at MAX_VALUES in _area_search)
            if not area_mode and distinct() >= s.max_values and \
                    not any(abs(g['pa'] - k) < 1e-6 for g in segs):
                raise _Budget()
            idx, sc = self._measure(ctx, k, stage, repeat)
            if sc is None:
                if self.cancel_flag:
                    raise _Cancelled()
                raise PAError("No pressure step at PA %.4f. Check filament "
                              "and nozzle temperature" % k)
            segs.append({'idx': idx, 'pa': k, 'piro': sc, 'stage': stage})
            p = point(k)
            lim = max(p['limit'], st['floor'])
            dips = too_high(p)
            bal = balance(k)
            steps.append({'pa': k, 'stage': stage, 'idx': idx,
                          'dip': p['dip'], 'limit': round(lim, 4),
                          'balance': bal,
                          'verdict': ('over' if (bal or 0) > 0 else 'under')
                          if area_mode else ('dips' if dips else 'ok'),
                          'prediction': pred})
            if area_mode:
                say("PA %.4f %s: %s %s"
                    % (k, stage, 'fall area' if s.mode == 'fall' else 'area',
                       "-" if bal is None else "%+.1f ms" % bal))
            else:
                say("PA %.4f %s: %s (dip %+.0f%%, limit %.0f%%)"
                    % (k, stage, "too high" if dips else "no dip",
                       100. * p['dip'], 100. * lim))
            self.progress = min(0.95, distinct() / float(typical + 1))
            return dips

        def balance(k):
            """Mean area balance (ms) of all pulses at PA k (fall: the
            slow-down area alone), or None."""
            v = [(d if s.mode == 'fall' else r + d)
                 for g in segs if abs(g['pa'] - k) < 1e-6
                 for r, d in ((g['piro'] or {}).get('area') or {})
                 .get('pulses', [])]
            return round(sum(v) / len(v), 2) if v else None

        def bracket():
            pts = analysis.pool(segs)
            bad = [p['pa'] for p in pts if too_high(p)]
            k_bad = min(bad) if bad else None
            ok = [p['pa'] for p in pts if not too_high(p)
                  and (k_bad is None or p['pa'] < k_bad)]
            return (max(ok) if ok else 0.), k_bad, pts

        def tested(x):
            return any(abs(x - g['pa']) < 0.0012 for g in segs)

        def room():
            """edge: a new value in steps 3 and 4 only while one more is
            left after it, for step 5 (a clean value close below the
            corner: without it the gap, and the +-, stays wide)"""
            return s.max_values - distinct() > 1
        try:
            # 1. reference (area: once, prime_steady fills the nozzle;
            #    edge: repeated while the step is not settled, it sets the floor)
            for attempt in range(1 if area_mode else 3):
                say("reference PA 0%s" % (" (repeat)" if attempt else ""))
                measure(0., 'reference', attempt > 0)
                if segs[-1]['piro']['spread'] <= 1.35:
                    break
                say("pressure step not settled, repeating reference")
            st['floor'] = analysis.floor_from_zero(point(0.)['dip'])
            self.search['floor'] = round(st['floor'], 4)
            if area_mode:
                self._area_search(s, segs, measure, balance, tested, snap, say)
                raise _Done()
            # 2. upper limit
            k = s.pa_max
            while not measure(k, 'upper'):
                k = round(k + s.upper_step, 5)
                if k > s.pa_limit + 1e-9:
                    raise PAError("No dip up to PA %.3f. Check the filament "
                                  "path or raise PA_LIMIT" % (k - s.upper_step))
            # 3. predict and bracket (higher value first)
            for _ in range(3):
                k_ok, k_bad, pts = bracket()
                pred = analysis.predict(pts, st['floor'])
                if pred is None:
                    pred = 0.5 * (k_ok + k_bad)
                gap = k_bad - k_ok
                pred = min(max(pred, k_ok + 0.15 * gap), k_bad - 0.15 * gap)
                self.search['prediction'] = round(pred, 5)
                if k_ok > 0 and gap <= max(0.006, 0.12 * pred):
                    break
                say("estimate %.4f" % pred)
                d = max(0.004, 0.1 * pred)
                for x in (pred + d, pred - d):
                    x = snap(x)
                    k_ok, k_bad, _ = bracket()
                    if k_ok < x < k_bad and not tested(x) and room():
                        measure(x, 'bracket', pred=round(pred, 5))
            # 4. refine inside the gap (upper third first), make sure the
            #    line has two values above the corner, re-measure close calls
            k_ok, k_bad, _ = bracket()
            gap = k_bad - k_ok
            if gap > 0.003:
                for x in (k_ok + 2. * gap / 3., k_ok + gap / 3.):
                    x = snap(x)
                    a, b, _ = bracket()
                    if a < x < b and not tested(x) and room():
                        measure(x, 'refine')
            k_ok, k_bad, pts = bracket()
            reach = k_bad + max(0.012, 2. * (k_bad - k_ok))
            if sum(1 for p in pts if too_high(p) and p['pa'] <= reach) < 2:
                x = snap(k_bad + max(0.006, k_bad - k_ok))
                if not tested(x) and x <= s.pa_limit and room():
                    measure(x, 'confirm')
            k_ok, k_bad, pts = bracket()
            close = [p for p in pts if p['times'] == 1 and p['pa'] > 0
                     and k_ok - 0.01 <= p['pa'] <= k_bad + 0.012
                     and abs(-p['dip'] - max(p['limit'], st['floor']))
                     < 0.5 * max(p['limit'], st['floor'])]
            close.sort(key=lambda p: abs(p['pa'] - 0.5 * (k_ok + k_bad)))
            for p in close[:2]:
                say("PA %.4f close to limit, repeating" % p['pa'])
                measure(p['pa'], 'repeat', True)
            # 5. a re-measurement can turn the last clean value into a dip:
            #    make sure a clean value sits close below the corner
            # (a clean value far below the corner sits on the flat part and
            #  would pull the fitted line, and the answer, low)
            for _ in range(3):
                k_ok, k_bad, _ = bracket()
                if k_ok > 0 and k_bad - k_ok <= max(0.004, 0.1 * k_bad):
                    break
                x = snap(k_bad - max(0.005, 0.12 * k_bad))
                if x <= k_ok or tested(x):
                    x = snap(0.5 * (k_ok + k_bad))
                if x <= k_ok or tested(x):
                    break
                measure(x, 'confirm')
        except _Done:
            pass
        except _Cancelled:
            return
        except _Budget:
            say("search limit reached (%d PA values, max_values %d)"
                % (len({round(g['pa'], 6) for g in segs}), s.max_values))
            self.search['limit'] = True
        # the answer itself comes from the full analysis when the run is
        # finalised (in the background), see run()
        say("measuring done, analysing")

    def _area_search(self, s, segs, measure, balance, tested, snap, say):
        """MODE=area after the reference: PA_MAX (+UPPER_STEP) until the
        area balance is above 0, then values beside the crossing of the
        line so far (see the top of this file)."""
        k = s.pa_max
        while True:
            measure(k, 'upper')
            b = balance(k)
            if b is not None and b > 0:
                break
            k = round(k + s.upper_step, 5)
            if k > s.pa_limit + 1e-9:
                raise PAError("Area below 0 up to PA %.3f. Check the "
                              "filament path or raise PA_LIMIT"
                              % (k - s.upper_step))
        top = k
        near = analysis.AREA_NEAR
        # MAX_VALUES counts PA values: repeats of PA 0 do not use it up
        count = lambda: len({round(g['pa'], 6) for g in segs})
        while count() < s.max_values:
            est = analysis.area_estimate(
                segs, 'fall' if s.mode == 'fall' else 'both')
            if est is None:
                break
            est = min(max(est, 0.0005), top)
            self.search['prediction'] = round(est, 5)
            pas = sorted({round(g['pa'], 6) for g in segs})
            above = sum(1 for p in pas if est < p <= est + near)
            below = sum(1 for p in pas if est - near <= p < est)
            if above >= 2 and below >= 2:
                break
            sides = [x for x, n in ((1, above), (-1, below)) if n < 2]
            if len(sides) == 2 and below < above:
                sides.reverse()
            x = None
            for sg in sides:
                n_side = above if sg > 0 else below
                # first value on a side close in, the second far out: a
                # wide spread makes the line surer
                offs = AREA_OFFSETS if n_side == 0 else AREA_OFFSETS_2ND
                for o in offs:
                    c = snap(est + sg * o)
                    if 0 < c <= s.pa_limit and abs(c - est) <= near + 1e-9 \
                            and all(abs(c - p) >= AREA_GAP - 1e-9 for p in pas):
                        x = c
                        break
                if x is not None:
                    break
            if x is None:
                break
            say("estimate %.4f" % est)
            measure(x, 'area', pred=round(est, 5))

    def _collect(self, col, t0, t1, wait=None):
        deadline = self.reactor.monotonic() + (wait or self.limits['timeout'])
        while col.latest < t1:
            if self.cancel_flag and self.dead:
                raise PAError("Klipper shut down")
            if self.reactor.monotonic() > deadline:
                raise PAError("Sensor data timed out (no samples for %.0f s)"
                              % (wait or self.limits['timeout']))
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        out = col.take(t0, t1)
        if not out:
            raise PAError("No load cell samples in this PA window")
        return out

    def _cleanup(self, s, orig_pa, saved, col, orig_sm=None, orig_lim=None):
        """Every way out of a test comes here: each step on its own (one
        failing does not skip the rest). After a Klipper shutdown only the
        in-memory steps run; the restart restores the rest from config."""
        if getattr(self, 'col2', None) is not None:
            try:
                self.bus.unsubscribe(self.col2.on_samples)
            except Exception:
                pass
            self.col2 = None
        G = [lambda: self.bus.set_mode(None)]    # Eddy back to its idle rate
        M = [lambda: self.bus.unsubscribe(col.on_samples)]   # in memory only
        G.append(lambda: self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f%s"
                                 % (orig_pa, (" SMOOTH_TIME=%.4f" % orig_sm)
                                    if s.smooth_time and orig_sm else "")))
        od = getattr(self, 'orig_dual', None)
        if od is not None:
            G.append(lambda: self._g(self._dual_cmd(od[0], od[1])))
        if orig_lim:
            G.append(lambda: self._restore_limits(orig_lim))
        if getattr(self, 'e_lim', None):
            M.append(lambda: self._e_limits(self.toolhead.get_extruder(),
                                            False))
        if s.restrict or s.method == 'klipper':   # free + klipper: back to the start
            G.append(lambda: self._g("G1 X%.2f Y%.2f F6000"
                                     % (s.park_x, s.park_y)))
        if saved:
            G.append(lambda: self._g("RESTORE_GCODE_STATE NAME=PA_LC"))
        for fn in M + ([] if self.dead else G):   # each step on its own
            try:
                fn()
            except Exception:
                pass
