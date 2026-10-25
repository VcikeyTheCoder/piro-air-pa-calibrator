"""PA_LOADCELL_TEST: safety checks, a prime (PRIME_LENGTH mm at PRIME_FLOW,
extrude-only, so the melt is steady), then every PA value: one slow lead-in
leg followed by CYCLES fast/slow pulses, each PA value saved as its own
segment. PA values come from a grid or from OznLab's adaptive search.

METHOD=artificial (OznLab, github.com/ozancs/oznlab-sensor, GPL-3.0):
  extrude-only moves; Klipper's PA is held at 0 and the PA charge
  K*(fast-slow) mm is put in by hand over the smoothing window centred on
  every speed-up (and taken back around every slow-down), the way
  Klipper's own smoothed PA would. Needs no XY motion.

Pulse shape (defaults from KAPAT, github.com/vzagranichnyy/KAPAT):
  every leg is ONE move "G1 <axis><target> E<mm> F<feed>" where the axis
  moves WOBBLE mm (alternating forward / back) and the feed is chosen so
  the leg lasts exactly its planned time. Klipper only applies pressure
  advance to moves with X or Y motion (klippy/kinematics/extruder.py:
  can_pressure_advance), so without the wobble every PA value would give
  the same force curve. WOBBLE=0 falls back to extrude-only moves.

  ACCEL is applied with SET_VELOCITY_LIMIT, i.e. to the toolhead (XY).
  The extruder's own limits are left as configured: they only govern
  extrude-only moves, which the wobble mode does not use."""
import collections
import math
from dataclasses import asdict
from .errors import PAError
from . import analysis

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
            'smooth_time': smooth_time(ext)}


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


class Wobble:
    """Alternates the wobble axis between base and base + amount."""
    def __init__(self, axis, base, amount):
        self.axis, self.lo, self.hi, self.up = axis, base, base + amount, False

    def next(self):
        self.up = not self.up
        return self.hi if self.up else self.lo


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
            raise PAError("no load cell found (check [load_cell])")
        st = self.toolhead.get_status(now)
        homed = st.get('homed_axes', '')
        if not all(a in homed for a in 'xyz'):
            raise PAError("home all axes first (G28)")
        z = self.toolhead.get_position()[2]
        if abs(z - L['required_z']) > L['z_tol']:
            raise PAError("Z must be at %.1f mm (now %.2f)"
                          % (L['required_z'], z))
        ext = self.toolhead.get_extruder()
        temp = ext.get_status(now).get('temperature', 0.)
        if temp < L['min_temp']:
            raise PAError("nozzle %.0fC is below the %.0fC minimum"
                          % (temp, L['min_temp']))
        area = filament_area(ext)
        used = s.filament_mm(area)
        if used > s.max_filament:
            raise PAError("this test could use %.0f mm of filament, above "
                          "MAX_FILAMENT %.0f mm: narrow the PA range, raise "
                          "PA_STEP or lower CYCLES" % (used, s.max_filament))
        vmax = getattr(ext, 'max_e_velocity', None)
        if s.method == 'artificial':
            sm = self.window(ext)
            for name, flow in (('FAST_FLOW', s.fast_flow),
                               ('SLOW_FLOW', s.slow_flow)):
                if vmax is not None and flow / area > vmax:
                    raise PAError("%s needs %.1f mm/s of filament, above "
                                  "max_extrude_only_velocity %.1f"
                                  % (name, flow / area, vmax))
            top = s.pa_max if s.pa_mode == 'search' else \
                max(s.pa_values() or [0.])
            v = s.max_charge_speed(area, sm, top)
            if vmax is not None and v > vmax:
                raise PAError("artificial PA %.3f needs a %.0f mm/s charge "
                              "move, above max_extrude_only_velocity %.1f: "
                              "lower the PA range or FAST_FLOW"
                              % (top, v, vmax))
        elif s.wobble > 0:
            need = s.cross_section_needed(area)
            ratio = getattr(ext, 'max_extrude_ratio', None)
            if ratio is not None and need > ratio * area:
                raise PAError(
                    "each pulse extrudes %.1f mm2 per mm of XY travel but "
                    "[extruder] max_extrude_cross_section allows %.2f mm2. "
                    "Add 'max_extrude_cross_section: %d' to [extruder] "
                    "(or use a larger WOBBLE)"
                    % (need, ratio * area, int(math.ceil(need / 10.) * 10)))
        else:
            v = s.fast_flow / area
            if vmax is not None and v > vmax:
                raise PAError("fast leg needs %.1f mm/s of filament, above "
                              "max_extrude_only_velocity %.1f" % (v, vmax))
        if s.prime_length > 0:
            vmax = getattr(ext, 'max_e_velocity', None)
            v = s.prime_flow / area
            if vmax is not None and v > vmax:
                raise PAError("PRIME_FLOW %.1f mm3/s needs %.1f mm/s of "
                              "filament, above max_extrude_only_velocity %.1f:"
                              " lower PRIME_FLOW" % (s.prime_flow, v, vmax))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside the axis limits" % name)
            i = 'XY'.index(s.wobble_axis)
            end = (s.test_x, s.test_y)[i] + s.wobble
            if s.method == 'wobble' and end > hi[i]:
                raise PAError("test position + WOBBLE is outside the %s limit"
                              % s.wobble_axis)
        return temp, area

    @staticmethod
    def window(ext):
        """Artificial PA charge window: Klipper's pressure_advance_smooth_time
        (clamped 0.01 .. 0.1 s like OznLab), so the K found is the K Klipper's
        own smoothed PA needs."""
        st = smooth_time(ext)
        return min(max(st if st else 0.04, 0.01), 0.1)

    # -- main ---------------------------------------------------------
    def run(self, gcmd, s):
        if self.state != 'idle':
            raise PAError("a test is already running")
        temp, area = self.precheck(s)
        values = s.pa_values() if s.pa_mode == 'grid' else []
        if s.method == 'wobble' and s.wobble <= 0:
            gcmd.respond_info("warning: WOBBLE=0 uses extrude-only moves, and "
                              "Klipper does not apply pressure advance to "
                              "those: every PA value will look the same")
        self.state, self.cancel_flag, self.progress = 'running', False, 0.0
        self.started = self.reactor.monotonic()
        now = self.reactor.monotonic()
        ext = self.toolhead.get_extruder()
        orig_pa = ext.get_status(now).get('pressure_advance', 0.)
        orig_acc = self.toolhead.get_status(now).get('max_accel')
        fast_mm, slow_mm = s.leg_mm(area)
        sm = self.window(ext) if s.method == 'artificial' else \
            smooth_time(ext)
        self.note, self.used = '', 0.0
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'pa_values': values,
            'filament_area': round(area, 4),
            'leg_mm': {'fast': round(fast_mm, 4), 'slow': round(slow_mm, 4)},
            'filament_mm': round(s.filament_mm(area), 1),
            'smooth_time': sm, 'method': s.method, 'pa_mode': s.pa_mode,
            'analysis': {'model': s.model, 'skip_first': bool(s.skip_first)}})
        col = SegmentCollector()
        status, err, saved = 'complete', '', False
        try:
            self.bus.subscribe(col.on_samples)
            # 100 % speed and flow so each leg lasts exactly its planned time
            self._g("SAVE_GCODE_STATE NAME=PA_LC\nG90\nM83\nM220 S100\nM221 S100")
            saved = True
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f" % s.accel)
            self._g("G1 X%.2f Y%.2f F6000" % (s.test_x, s.test_y))
            wob = None
            if s.method == 'wobble' and s.wobble > 0:
                base = s.test_x if s.wobble_axis == 'X' else s.test_y
                wob = Wobble(s.wobble_axis, base, s.wobble)
            if s.method == 'artificial':
                self._set_klipper_pa(0.)     # the charge is put in by hand
            self._prime(s, area, ext)
            if s.prime_rest > 0:
                self.toolhead.dwell(s.prime_rest)
            ctx = (col, s, area, wob, sm)
            if s.pa_mode == 'search':
                self._search(ctx, gcmd)
            else:
                for i, pa in enumerate(values):
                    if self.cancel_flag:
                        break
                    self._measure(ctx, pa)
                    self.progress = (i + 1) / len(values)
            if self.cancel_flag:
                status = 'cancelled'
        except Exception as e:
            status, err = 'error', str(e)
            gcmd.respond_info("PA_LOADCELL_TEST stopped: %s" % err)
        finally:
            self._cleanup(s, orig_pa, orig_acc, saved, col)
            self.store.finalize(self.run_id, status, err)
            self.state, self.current_pa, self.phase = 'idle', None, ''
            self.pa_t, self.note = None, ''
        if s.pa_mode == 'search' and getattr(self, 'search', None):
            self.store.set_search(self.run_id, self.search)
        gcmd.respond_info("Run %d %s: %d PA values saved"
                          % (self.run_id, status,
                             len(self.store.get_run(self.run_id)['segments'])))
        if status == 'error':
            raise PAError(err)
        return self.run_id

    def _log_leg(self, t0, t1, flow, name):
        self.leg_seq += 1
        self.legs.append((self.leg_seq, round(t0, 4), round(t1, 4),
                          flow, name))

    def legs_since(self, seq):
        return [list(x) for x in self.legs if x[0] > seq]

    def _leg(self, e_mm, dur, wob, flow, name):
        """One leg lasting `dur` seconds. Returns (t0, t1) in print_time."""
        th = self.toolhead
        t0 = th.get_last_move_time()
        if wob is not None:
            self._g("G1 %s%.4f E%.5f F%.4f"
                    % (wob.axis, wob.next(), e_mm, (wob.hi - wob.lo) / dur * 60.))
        else:
            self._g("G1 E%.5f F%.4f" % (e_mm, e_mm / dur * 60.))
        t1 = th.get_last_move_time()
        self._log_leg(t0, t1, flow, name)
        return t0, t1

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

    def _measure(self, ctx, pa, note=None, repeat=False):
        """Run one PA value and save it. Returns the OznLab score or None."""
        col, s, area, wob, sm = ctx
        th = self.toolhead
        fil = s.filament_mm(area, 1) - s.prime_length
        if self.used + fil > s.max_filament - s.prime_length + 1e-6:
            raise PAError("stopped at MAX_FILAMENT (%.0f mm)" % s.max_filament)
        self.used += fil
        self.current_pa = pa
        if s.rest_time > 0:
            th.dwell(s.rest_time)
        if s.method == 'wobble':
            self._set_klipper_pa(pa)
        t_base = th.get_last_move_time()      # recording starts at PA change
        self.pa_t = t_base
        col.discard_before(t_base - 0.05)
        if s.method == 'artificial':
            markers, partial = self._train_artificial(pa, s, area, sm, t_base)
        else:
            markers, partial = self._train_wobble(s, area, wob, t_base)
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not any(m['name'] == 'fast' for m in markers):
            return None
        seg = {'pa': pa, 'unit': self.bus.unit,
               'rate': round(self.bus.rate, 1), 'markers': markers,
               'partial': partial, 'method': s.method,
               'flows': {'fast': s.fast_flow, 'slow': s.slow_flow},
               't': [round(t - t_base, 4) for t, _ in samples],
               'f': [round(v, 3) for _, v in samples]}
        if note:
            seg['search_note'] = note
        if repeat:
            seg['repeat'] = True
        self.store.add_segment(self.run_id, seg)
        if partial:
            return None
        return analysis.ozn_score(seg['t'], seg['f'], markers, sm or 0.04,
                                  skip_first=True)

    def _train_wobble(self, s, area, wob, t_base):
        fast_mm, slow_mm = s.leg_mm(area)
        markers, partial = [], False

        def mark(name, k, t):
            markers.append({'name': name, 'sweep': k,
                            't0': round(t[0] - t_base, 4),
                            't1': round(t[1] - t_base, 4)})
        # lead-in: one slow leg so the melt settles at the new PA value
        self.phase = 'lead-in'
        mark('slow', -1, self._leg(slow_mm, s.slow_time, wob, s.slow_flow,
                                   'slow'))
        for k in range(s.cycles):
            if self.cancel_flag:
                partial = True
                break
            self.phase = 'fast'
            mark('fast', k, self._leg(fast_mm, s.fast_time, wob, s.fast_flow,
                                      'fast'))
            self.phase = 'slow'
            mark('slow', k, self._leg(slow_mm, s.slow_time, wob, s.slow_flow,
                                      'slow'))
        return markers, partial

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

    # -- OznLab adaptive search -------------------------------------------
    def _search(self, ctx, gcmd):
        """Port of OZNLAB_PA_AUTO's search: PA 0 as the reference (repeated
        while the pressure step is not steady), then PA_MAX, doubling until
        the force dips after a slow-down, then narrowing the bracket to 10 %.
        The answer is the last PA without that dip."""
        col, s, area, wob, sm = ctx
        steps = []
        self.search = {'steps': steps, 'result': None, 'k_bad': None}
        vmax = getattr(self.toolhead.get_extruder(), 'max_e_velocity', None)
        vs, vf = s.slow_flow / area, s.fast_flow / area

        def k_ok_speed(k):
            if s.method != 'artificial' or not vmax:
                return True
            return (k * (vf - vs) + (vf + vs) * sm / 2.) / sm <= vmax

        def say(text):
            self.note = text
            gcmd.respond_info("pa search: " + text)

        def score(k, note, repeat=False):
            if self.cancel_flag:
                raise _Cancelled()
            sc = self._measure(ctx, k, note, repeat)
            if sc is None:
                if self.cancel_flag:
                    raise _Cancelled()
                raise PAError("no pressure step at PA %.4f: filament "
                              "loaded and the nozzle hot?" % k)
            return sc
        floor = {'v': 0.}

        def judge(k):
            sc = score(k, 'judge')
            lim = max(sc['limit'], floor['v'])
            u = sc['dip']
            if -2. * lim < u < -0.7 * lim:          # close call: once more
                say("PA %.4f is borderline, measuring again" % k)
                sc2 = score(k, 'repeat', True)
                u = 0.5 * (u + sc2['dip'])
                lim = max(0.5 * (sc['limit'] + sc2['limit']) / math.sqrt(2.),
                          floor['v'])
            depth = max(0., -u - lim)
            steps.append({'pa': round(k, 5), 'dip': round(u, 4),
                          'limit': round(lim, 4), 'depth': round(depth, 4)})
            say("PA %.4f %s (dip %+.0f%%, limit %.0f%%)"
                % (k, "dips: too much" if depth > 0 else "no dip",
                   100. * u, 100. * lim))
            self.progress = min(0.95, len(steps) / 9.)
            return depth
        try:
            for attempt in range(3):
                say("PA 0 reference%s" % (" (again)" if attempt else ""))
                sc0 = score(0., 'reference', attempt > 0)
                if sc0['spread'] <= 1.35:
                    break
                say("pressure step not steady yet, one more reference")
            floor['v'] = analysis.ozn_floor(sc0['dip'])
            steps.append({'pa': 0., 'dip': sc0['dip'],
                          'limit': sc0['limit'], 'depth': 0.,
                          'floor': round(floor['v'], 4)})
            k_ok, k_bad, k, n_ext = 0., None, s.pa_max, 0
            while True:
                if judge(k) > 0.:
                    k_bad = k
                    break
                k_ok = k
                if n_ext >= 3 or not k_ok_speed(k * 2.):
                    self.search.update(result=round(k_ok, 5))
                    say("no dip up to PA %.4f: check the filament path or "
                        "raise PA_MAX" % k)
                    return
                k *= 2.
                n_ext += 1
            for _ in range(5):                      # narrow the bracket to 10 %
                if (k_bad - k_ok) <= 0.10 * k_bad:
                    break
                bad = sorted((x['pa'], x['depth']) for x in steps
                             if x['depth'] > 0.)
                k_new = None
                if len(bad) >= 2:                   # the dip grows with PA
                    (k1, d1), (k2, d2) = bad[:2]
                    if d2 > d1 and k2 > k1:
                        k_new = k1 - d1 * (k2 - k1) / (d2 - d1)
                lo = k_ok + 0.25 * (k_bad - k_ok)
                hi = k_bad - 0.25 * (k_bad - k_ok)
                if k_new is None or not lo <= k_new <= hi:
                    k_new = 0.5 * (k_ok + k_bad)
                k_new = round(k_new, 5)
                if judge(k_new) > 0.:
                    k_bad = k_new
                else:
                    k_ok = k_new
            self.search.update(result=round(k_ok, 5), k_bad=round(k_bad, 5),
                               floor=round(floor['v'], 4))
            say("result PA %.4f (next %.4f dips)" % (k_ok, k_bad))
        except _Cancelled:
            return

    def _collect(self, col, t0, t1):
        deadline = self.reactor.monotonic() + self.limits['timeout']
        while col.latest < t1:
            if self.reactor.monotonic() > deadline:
                raise PAError("load cell data timed out (no samples up to "
                              "the end of this PA window)")
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        out = col.take(t0, t1)
        if not out:
            raise PAError("no load cell samples in this PA window")
        return out

    def _cleanup(self, s, orig_pa, acc, saved, col):
        steps = [lambda: self.bus.unsubscribe(col.on_samples),
                 lambda: self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f"
                                 % orig_pa)]
        if acc is not None:
            steps.append(lambda: self._g(
                "SET_VELOCITY_LIMIT ACCEL=%.1f" % acc))
        steps.append(lambda: self._g("G1 X%.2f Y%.2f F6000"
                                     % (s.park_x, s.park_y)))
        if saved:
            steps.append(lambda: self._g("RESTORE_GCODE_STATE NAME=PA_LC"))
        for fn in steps:        # one failing step must not skip the rest
            try:
                fn()
            except Exception:
                pass
