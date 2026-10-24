"""PA_LOADCELL_TEST: safety checks, then for every PA value a lead-in slow
leg followed by CYCLES fast/slow pulses, each PA value saved as its own
segment.

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
import math
from dataclasses import asdict
from .errors import PAError

DEFAULT_AREA = math.pi * 0.875 ** 2      # 1.75 mm filament


def filament_area(ext):
    return getattr(ext, 'filament_area', None) or DEFAULT_AREA


def extruder_info(ext):
    """What the UI needs to check a test before starting it."""
    area = filament_area(ext)
    ratio = getattr(ext, 'max_extrude_ratio', None)
    return {'filament_area': round(area, 4),
            'max_cross_section': round(ratio * area, 3) if ratio else None,
            'max_e_velocity': getattr(ext, 'max_e_velocity', None)}


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
        self.run_id = None
        self.started = 0.0

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
            raise PAError("this test would use %.0f mm of filament, above "
                          "MAX_FILAMENT %.0f mm: narrow the PA range, raise "
                          "PA_STEP or lower CYCLES" % (used, s.max_filament))
        if s.wobble > 0:
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
            vmax = getattr(ext, 'max_e_velocity', None)
            v = s.fast_flow / area
            if vmax is not None and v > vmax:
                raise PAError("fast leg needs %.1f mm/s of filament, above "
                              "max_extrude_only_velocity %.1f" % (v, vmax))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside the axis limits" % name)
            i = 'XY'.index(s.wobble_axis)
            end = (s.test_x, s.test_y)[i] + s.wobble
            if end > hi[i]:
                raise PAError("test position + WOBBLE is outside the %s limit"
                              % s.wobble_axis)
        return temp, area

    # -- main ---------------------------------------------------------
    def run(self, gcmd, s):
        if self.state != 'idle':
            raise PAError("a test is already running")
        temp, area = self.precheck(s)
        values = s.pa_values()
        if s.wobble <= 0:
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
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'pa_values': values,
            'filament_area': round(area, 4),
            'leg_mm': {'fast': round(fast_mm, 4), 'slow': round(slow_mm, 4)},
            'filament_mm': round(s.filament_mm(area), 1)})
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
            if s.wobble > 0:
                base = s.test_x if s.wobble_axis == 'X' else s.test_y
                wob = Wobble(s.wobble_axis, base, s.wobble)
            for i, pa in enumerate(values):
                if self.cancel_flag:
                    break
                self.current_pa = pa
                self._segment(col, pa, s, area, wob, first=(i == 0))
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
            self.pa_t = None
        gcmd.respond_info("Run %d %s: %d PA values saved"
                          % (self.run_id, status,
                             len(self.store.get_run(self.run_id)['segments'])))
        if status == 'error':
            raise PAError(err)
        return self.run_id

    def _leg(self, e_mm, dur, wob):
        """One leg lasting `dur` seconds. Returns (t0, t1) in print_time."""
        th = self.toolhead
        t0 = th.get_last_move_time()
        if wob is not None:
            self._g("G1 %s%.4f E%.5f F%.4f"
                    % (wob.axis, wob.next(), e_mm, (wob.hi - wob.lo) / dur * 60.))
        else:
            self._g("G1 E%.5f F%.4f" % (e_mm, e_mm / dur * 60.))
        return t0, th.get_last_move_time()

    def _segment(self, col, pa, s, area, wob, first):
        th = self.toolhead
        fast_mm, slow_mm = s.leg_mm(area)
        self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f" % pa)
        t_base = th.get_last_move_time()      # recording starts at PA change
        self.pa_t = t_base
        col.discard_before(t_base - 0.05)
        markers, partial = [], False

        def mark(name, k, t):
            markers.append({'name': name, 'sweep': k,
                            't0': round(t[0] - t_base, 4),
                            't1': round(t[1] - t_base, 4)})
        # lead-in: slow flow so the melt is at a steady state before the
        # first fast pulse (longer before the very first PA value)
        n_lead = s.warmup_legs() if first else 1
        lead_t = s.slow_time * (s.warmup if first else 1.0) / n_lead
        self.phase = 'lead-in'
        t_lead = None
        for _ in range(n_lead):
            t = self._leg(slow_mm * lead_t / s.slow_time, lead_t, wob)
            t_lead = (t_lead[0] if t_lead else t[0], t[1])
        mark('slow', -1, t_lead)
        for k in range(s.cycles):
            if self.cancel_flag:
                partial = True
                break
            self.phase = 'fast'
            mark('fast', k, self._leg(fast_mm, s.fast_time, wob))
            self.phase = 'slow'
            mark('slow', k, self._leg(slow_mm, s.slow_time, wob))
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not any(m['name'] == 'fast' for m in markers):
            return
        self.store.add_segment(self.run_id, {
            'pa': pa, 'unit': self.bus.unit, 'rate': round(self.bus.rate, 1),
            'markers': markers, 'partial': partial,
            't': [round(t - t_base, 4) for t, _ in samples],
            'f': [round(v, 3) for _, v in samples]})

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
