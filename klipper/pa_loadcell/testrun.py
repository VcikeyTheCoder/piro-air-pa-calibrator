"""PA_LOADCELL_TEST (PIRO-EDGE): safety checks, a prime (PRIME_LENGTH mm at
PRIME_FLOW, extrude-only, so the melt is steady), then an adaptive search.
Every PA value is one slow lead-in leg and CYCLES fast/slow pulses, saved as
its own segment.

Pulses use artificial PA (as in OznLab sensor, github.com/ozancs/oznlab-
sensor, GPL-3.0): extrude-only moves; Klipper's PA is held at 0 and the PA
charge K*(fast-slow) mm is put in by hand over the smoothing window centred
on every speed-up (and taken back around every slow-down), the way Klipper's
own smoothed PA would. Needs no XY motion.

Search (_search):
  1. PA 0: reference (noise floor, pressure step size)
  2. upper limit: PA_MAX, then +UPPER_STEP until the force dips after the
     slow-down; no dip at PA_LIMIT -> the test fails
  3. prediction from how deep the dips are, then values just above and
     just below it (higher one first: a mixed order spreads slow drift)
  4. two values inside the remaining gap, one more above it if the line
     needs it, and a second measurement of values close to their limit
  5. the answer: analysis.piro_pick over everything measured

PA_METHOD=klipper: Klipper's own PA and smoothing act instead. The nozzle
runs back and forth on a short line along MOVE_AXIS with a constant amount
of filament per mm (85 % of max_extrude_cross_section); fast and slow come
from the toolhead speed, the PA value from SET_PRESSURE_ADVANCE. The turn of
the line sits inside the slow leg, after the dip window. Move times are
computed from the planned trapezoids (MINIMUM_CRUISE_RATIO=0 during the test)
so the lookahead queue is never flushed in the middle of a train. The result
is Klipper's value directly: no smooth time conversion."""
import collections
import math
from dataclasses import asdict
from .errors import PAError
from . import analysis
from .settings import SEARCH_MAX_TRAINS, SEARCH_TYPICAL

DEFAULT_AREA = math.pi * 0.875 ** 2      # 1.75 mm filament


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


class _Cancelled(Exception):
    pass


class _Budget(Exception):
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

    @property
    def toolhead(self):
        # Looked up lazily: Klipper creates 'toolhead' only after all config
        # sections are loaded, so it does not exist yet inside __init__.
        return self.printer.lookup_object('toolhead')

    def cancel(self):
        if self.state == 'running':
            self.cancel_flag, self.state = True, 'cancelling'

    def _g(self, script):
        self.gcode.run_script_from_command(script)

    # -- safety -------------------------------------------------------
    def precheck(self, s):
        now = self.reactor.monotonic()
        L = self.limits
        if not self.bus.available():
            raise PAError("Load cell not found. Check load_cell: in [pa_loadcell]")
        st = self.toolhead.get_status(now)
        homed = st.get('homed_axes', '')
        if not all(a in homed for a in 'xyz'):
            raise PAError("Must home all axes first")
        z = self.toolhead.get_position()[2]
        if abs(z - L['required_z']) > L['z_tol']:
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
        if s.pa_method == 'klipper':
            flows = flows[2:]                # only the prime is extrude-only
            ln = s.line(area, getattr(ext, 'max_extrude_ratio', None), sm)
            vmx = st.get('max_velocity')
            if vmx is not None and ln['v_fast'] > vmx:
                raise PAError("Klipper method needs %.0f mm/s toolhead speed, "
                              "above max_velocity %.0f. Lower FAST_FLOW"
                              % (ln['v_fast'], vmx))
            lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
            if lo is not None:
                i = 'XY'.index(s.move_axis)
                c = (s.test_x, s.test_y)[i]
                if c - ln['length'] / 2 < lo[i] or c + ln['length'] / 2 > hi[i]:
                    raise PAError("Klipper method line (%.1f mm along %s) does "
                                  "not fit around the test position"
                                  % (ln['length'], s.move_axis))
        for name, flow in flows:
            if vmax is not None and flow / area > vmax:
                raise PAError("%s %.1f mm3/s needs %.1f mm/s, above "
                              "max_extrude_only_velocity %.1f"
                              % (name, flow, flow / area, vmax))
        v = s.charge_speed(s.pa_max, area, s.fast_flow, s.slow_flow, sm)
        if s.pa_method == 'push' and vmax is not None and v > vmax:
            raise PAError("PA_MAX %.3f needs a %.0f mm/s charge move, above "
                          "max_extrude_only_velocity %.1f. Lower PA_MAX "
                          "or FAST_FLOW" % (s.pa_max, v, vmax))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside axis limits" % name)
        return temp, area

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
        temp, area = self.precheck(s)
        self.state, self.cancel_flag, self.progress = 'running', False, 0.0
        self.started = self.reactor.monotonic()
        now = self.reactor.monotonic()
        ext = self.toolhead.get_extruder()
        est = ext.get_status(now)
        orig_pa = est.get('pressure_advance', 0.)
        orig_sm = est.get('smooth_time')
        fast_mm, slow_mm = s.leg_mm(area)
        sm = self.window(ext, s)
        klip = s.pa_method == 'klipper'
        ln = s.line(area, getattr(ext, 'max_extrude_ratio', None), sm) \
            if klip else None
        tst = self.toolhead.get_status(now)
        orig_lim = {k: tst.get(k) for k in ('max_accel', 'minimum_cruise_ratio',
                                            'max_accel_to_decel')}
        self.note, self.used, self.search = '', 0.0, None
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'method': 'PIRO-EDGE',
            'filament_area': round(area, 4),
            'leg_mm': {'fast': round(fast_mm, 4), 'slow': round(slow_mm, 4)},
            'filament_mm': round(s.filament_mm(area), 1),
            'smooth_time': sm, 'pa_method': s.pa_method,
            'line': ({k: round(v, 4) for k, v in ln.items()}
                     if ln else None),
            'analysis': {'skip_first': bool(s.skip_first)}})
        col = SegmentCollector()
        status, err, saved = 'complete', '', False
        try:
            self.bus.subscribe(col.on_samples)
            # 100 % speed and flow so each leg lasts exactly its planned time
            self._g("SAVE_GCODE_STATE NAME=PA_LC\nG90\nM83\nM220 S100\nM221 S100")
            saved = True
            self._g("G1 X%.2f Y%.2f F6000" % (s.test_x, s.test_y))
            if s.smooth_time:
                self._g("SET_PRESSURE_ADVANCE ADVANCE=0 SMOOTH_TIME=%.4f" % sm)
            self._set_klipper_pa(0.)         # push: the charge is put in by hand
            if klip:
                self._set_limits(s, orig_lim)
            self._prime(s, area, ext)
            if s.prime_rest > 0:
                self.toolhead.dwell(s.prime_rest)
            if klip:
                self._motion_check(col, s, ln, gcmd)
            self._search((col, s, area, sm, ln), gcmd)
            if self.cancel_flag:
                status = 'cancelled'
        except Exception as e:
            status, err = 'error', str(e)
            gcmd.respond_info("PA_LOADCELL_TEST aborted: %s" % err)
        finally:
            self._cleanup(s, orig_pa, saved, col, orig_sm, orig_lim if klip else None)
            self.store.finalize(self.run_id, status, err)
            self.state, self.current_pa, self.phase = 'idle', None, ''
            self.pa_t, self.note = None, ''
        if self.search:
            self.store.set_search(self.run_id, self.search)
        res = (self.store.get_run(self.run_id).get('analysis') or {}) \
            .get('result') or {}
        gcmd.respond_info("Run %d %s: %d PA values%s"
                          % (self.run_id, status,
                             len(self.store.get_run(self.run_id)['segments']),
                             (", PIRO-EDGE PA %.4f +- %.4f (%s)"
                              % (res['pa'], res['unc'], res['grade']))
                             if res.get('pa') is not None else ''))
        if status == 'error':
            raise PAError(err)
        return self.run_id

    def _log_leg(self, t0, t1, flow, name):
        self.leg_seq += 1
        self.legs.append((self.leg_seq, round(t0, 4), round(t1, 4),
                          flow, name))

    def legs_since(self, seq):
        return [list(x) for x in self.legs if x[0] > seq]

    def _prime(self, s, area, ext):
        """Push PRIME_LENGTH mm at PRIME_FLOW, extrude-only (PA does not act
        and does not matter here), split into pieces that respect
        max_extrude_only_distance. The first PA value starts right after."""
        if s.prime_length <= 0:
            return
        self.phase = 'prime'
        v = s.prime_flow / area                       # mm/s of filament
        limit = getattr(ext, 'max_e_dist', None) or 50.
        piece = max(1., min(limit * 0.9, 25.))
        left = s.prime_length
        while left > 1e-6 and not self.cancel_flag:
            e = min(piece, left)
            th = self.toolhead
            t0 = th.get_last_move_time()
            self._g("G1 E%.5f F%.4f" % (e, v * 60.))
            self._log_leg(t0, th.get_last_move_time(), s.prime_flow, 'prime')
            left -= e

    def _set_klipper_pa(self, value):
        self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f" % value)

    def _emove(self, e, dur, vmax=None):
        """Artificial PA charge / pull-back window: E move lasting `dur`.
        Klipper refuses F0, so an almost empty window becomes a dwell."""
        if abs(e) < 0.002:
            self.toolhead.dwell(dur)
            return
        v = abs(e) / dur
        if vmax:
            v = min(v, vmax)
        self._g("G1 E%.5f F%.4f" % (e, v * 60.))

    def _measure(self, ctx, pa, stage=None, repeat=False):
        """Run one PA value and save it. Returns (segment index, score)."""
        col, s, area, sm, ln = ctx
        th = self.toolhead
        fil = s.per_pa_mm(area)
        if self.used + fil > s.max_filament - s.prime_length + 1e-6:
            raise PAError("Stopped at MAX_FILAMENT %.0f mm" % s.max_filament)
        self.used += fil
        self.current_pa = pa
        if ln:
            self._set_klipper_pa(pa)          # Klipper's own PA from here on
            self._line_start(s, ln)
        if s.rest_time > 0:
            th.dwell(s.rest_time)
        t_base = th.get_last_move_time()      # recording starts here
        self.pa_t = t_base
        col.discard_before(t_base - 0.05)
        if ln:
            markers, partial = self._train_klipper(s, ln, area, t_base)
        else:
            markers, partial = self._train_artificial(pa, s, area, sm, t_base)
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not any(m['name'] == 'fast' for m in markers):
            return None, None
        seg = {'pa': pa, 'unit': self.bus.unit,
               'rate': round(self.bus.rate, 1), 'markers': markers,
               'partial': partial,
               'method': 'klipper' if ln else 'artificial',
               'flows': {'fast': s.fast_flow, 'slow': s.slow_flow},
               't': [round(t - t_base, 4) for t, _ in samples],
               'f': [round(v, 3) for _, v in samples]}
        if stage:
            seg['stage'] = stage
        if repeat:
            seg['repeat'] = True
        idx = self.store.add_segment(self.run_id, seg)
        if partial:
            return idx, None
        return idx, analysis.edge_score(seg['t'], seg['f'], markers, sm,
                                        skip_first=bool(s.skip_first))

    def _train_artificial(self, pa, s, area, sm, t_base):
        """OznLab's pulse train: slow, then (charge, fast, pull-back, slow)
        x CYCLES, all extrude-only. The charge window is sm long and centred
        on the nominal speed change; it carries the flow of that window plus
        PA * (fast - slow) mm. Markers are the nominal step times."""
        th = self.toolhead
        vs, vf = s.slow_flow / area, s.fast_flow / area
        dv, h = vf - vs, sm / 2.
        vmax = getattr(th.get_extruder(), 'max_e_velocity', None)
        markers, partial = [], False
        rel = lambda x: round(x - t_base, 4)
        self.phase = 'lead-in'
        t0 = th.get_last_move_time()
        self._g("G1 E%.5f F%.4f" % (vs * (s.slow_time - h), vs * 60.))
        t_prev = th.get_last_move_time() + h       # nominal first speed-up
        markers.append({'name': 'slow', 'sweep': -1, 't0': rel(t0),
                        't1': rel(t_prev)})
        self._log_leg(t0, t_prev, s.slow_flow, 'slow')
        for k in range(s.cycles):
            if self.cancel_flag:
                partial = True
                break
            self.phase = 'fast'
            c0 = th.get_last_move_time()
            self._emove(pa * dv + (vs + vf) * h, sm, vmax)
            up = 0.5 * (c0 + th.get_last_move_time())
            self._g("G1 E%.5f F%.4f" % (vf * (s.fast_time - sm), vf * 60.))
            self.phase = 'slow'
            d0 = th.get_last_move_time()
            self._emove(-pa * dv + (vs + vf) * h, sm, vmax)
            down = 0.5 * (d0 + th.get_last_move_time())
            self._g("G1 E%.5f F%.4f" % (vs * (s.slow_time - sm), vs * 60.))
            end = th.get_last_move_time() + (h if k < s.cycles - 1 else 0.)
            markers.append({'name': 'fast', 'sweep': k, 't0': rel(up),
                            't1': rel(down)})
            markers.append({'name': 'slow', 'sweep': k, 't0': rel(down),
                            't1': rel(end)})
            self._log_leg(up, down, s.fast_flow, 'fast')
            self._log_leg(down, end, s.slow_flow, 'slow')
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

    def _line_ends(self, s, ln):
        i = 'XY'.index(s.move_axis)
        c = (s.test_x, s.test_y)[i]
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
        pos, sign, partial = a, 1., False
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
        vmax = getattr(self.toolhead.get_extruder(), 'max_e_velocity', None)
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

        def measure(k, stage, repeat=False, pred=None):
            if self.cancel_flag:
                raise _Cancelled()
            if len(segs) >= SEARCH_MAX_TRAINS:
                raise _Budget()
            k = round(k, 5)
            if not ln and vmax is not None and s.charge_speed(
                    k, area, s.fast_flow, s.slow_flow, sm) > vmax:
                raise PAError("PA %.3f charge move exceeds "
                              "max_extrude_only_velocity %.1f. "
                              "Lower FAST_FLOW" % (k, vmax))
            idx, sc = self._measure(ctx, k, stage, repeat)
            if sc is None:
                if self.cancel_flag:
                    raise _Cancelled()
                raise PAError("No pressure step at PA %.4f. Check filament "
                              "and nozzle temperature" % k)
            segs.append({'idx': idx, 'pa': k, 'piro': sc})
            p = point(k)
            lim = max(p['limit'], st['floor'])
            dips = too_high(p)
            steps.append({'pa': k, 'stage': stage, 'idx': idx,
                          'dip': p['dip'], 'limit': round(lim, 4),
                          'verdict': 'dips' if dips else 'ok',
                          'prediction': pred})
            say("PA %.4f %s: %s (dip %+.0f%%, limit %.0f%%)"
                % (k, stage, "too high" if dips else "no dip",
                   100. * p['dip'], 100. * lim))
            self.progress = min(0.95, len(segs) / float(SEARCH_TYPICAL + 1))
            return dips

        def bracket():
            pts = analysis.pool(segs)
            bad = [p['pa'] for p in pts if too_high(p)]
            k_bad = min(bad) if bad else None
            ok = [p['pa'] for p in pts if not too_high(p)
                  and (k_bad is None or p['pa'] < k_bad)]
            return (max(ok) if ok else 0.), k_bad, pts

        def tested(x):
            return any(abs(x - g['pa']) < 0.0012 for g in segs)
        try:
            # 1. reference
            for attempt in range(3):
                say("reference PA 0%s" % (" (repeat)" if attempt else ""))
                measure(0., 'reference', attempt > 0)
                if segs[-1]['piro']['spread'] <= 1.35:
                    break
                say("pressure step not settled, repeating reference")
            st['floor'] = analysis.floor_from_zero(point(0.)['dip'])
            self.search['floor'] = round(st['floor'], 4)
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
                    if k_ok < x < k_bad and not tested(x):
                        measure(x, 'bracket', pred=round(pred, 5))
            # 4. refine inside the gap (upper third first), make sure the
            #    line has two values above the corner, re-measure close calls
            k_ok, k_bad, _ = bracket()
            gap = k_bad - k_ok
            if gap > 0.003:
                for x in (k_ok + 2. * gap / 3., k_ok + gap / 3.):
                    x = snap(x)
                    a, b, _ = bracket()
                    if a < x < b and not tested(x):
                        measure(x, 'refine')
            k_ok, k_bad, pts = bracket()
            reach = k_bad + max(0.012, 2. * (k_bad - k_ok))
            if sum(1 for p in pts if too_high(p) and p['pa'] <= reach) < 2:
                x = snap(k_bad + max(0.006, k_bad - k_ok))
                if not tested(x) and x <= s.pa_limit:
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
        except _Cancelled:
            return
        except _Budget:
            say("search limit reached (%d PA values)" % SEARCH_MAX_TRAINS)
        res = analysis.piro_pick(segs, None if ln else sm)
        self.search.update(result=res.get('pa'), unc=res.get('unc'),
                           grade=res.get('grade'))
        if res.get('pa') is not None:
            say("PA %.4f +- %.4f (%s)" % (res['pa'], res['unc'],
                                          res['grade']))
        else:
            say("no result: %s" % res.get('why', ''))

    def _collect(self, col, t0, t1):
        deadline = self.reactor.monotonic() + self.limits['timeout']
        while col.latest < t1:
            if self.reactor.monotonic() > deadline:
                raise PAError("Load cell data timed out")
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        out = col.take(t0, t1)
        if not out:
            raise PAError("No load cell samples in this PA window")
        return out

    def _cleanup(self, s, orig_pa, saved, col, orig_sm=None, orig_lim=None):
        steps = [lambda: self.bus.unsubscribe(col.on_samples),
                 lambda: self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f%s"
                                 % (orig_pa, (" SMOOTH_TIME=%.4f" % orig_sm)
                                    if s.smooth_time and orig_sm else ""))]
        if orig_lim:
            steps.append(lambda: self._restore_limits(orig_lim))
        steps.append(lambda: self._g("G1 X%.2f Y%.2f F6000"
                                     % (s.park_x, s.park_y)))
        if saved:
            steps.append(lambda: self._g("RESTORE_GCODE_STATE NAME=PA_LC"))
        for fn in steps:        # one failing step must not skip the rest
            try:
                fn()
            except Exception:
                pass
