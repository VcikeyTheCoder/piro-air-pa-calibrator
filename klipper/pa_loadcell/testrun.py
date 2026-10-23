"""PA_LOADCELL_TEST: safety checks, then for every PA value N slow/fast
sweeps, each PA value saved as its own segment."""
from dataclasses import asdict
from .errors import PAError


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


class PATestRunner:
    def __init__(self, printer, bus, store, limits):
        self.printer, self.bus, self.store = printer, bus, store
        self.limits = limits        # dict: min_temp, required_z, z_tol, timeout
        self.reactor = printer.get_reactor()
        self.gcode = printer.lookup_object('gcode')
        self.toolhead = printer.lookup_object('toolhead')
        self.state = 'idle'         # idle | running | cancelling
        self.cancel_flag = False
        self.progress = 0.0
        self.current_pa = None
        self.run_id = None

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
        vmax = getattr(ext, 'max_e_velocity', None)
        if vmax is not None and s.fast_speed > vmax:
            raise PAError("FAST_SPEED %.1f exceeds max_extrude_only_velocity "
                          "%.1f" % (s.fast_speed, vmax))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside the axis limits" % name)
        return temp

    # -- main ---------------------------------------------------------
    def run(self, gcmd, s):
        if self.state != 'idle':
            raise PAError("a test is already running")
        temp = self.precheck(s)
        values = s.pa_values()
        if s.fast_duration() < 0.3:
            gcmd.respond_info("warning: fast phase lasts only %.2fs; use a "
                              "longer EXTRUDE_LEN or lower FAST_SPEED"
                              % s.fast_duration())
        self.state, self.cancel_flag, self.progress = 'running', False, 0.0
        now = self.reactor.monotonic()
        ext = self.toolhead.get_extruder()
        orig_pa = ext.get_status(now).get('pressure_advance', 0.)
        th_st = self.toolhead.get_status(now)
        orig_acc = th_st.get('max_accel')
        orig_a2d = th_st.get('max_accel_to_decel')
        orig_e_acc = getattr(ext, 'max_e_accel', None)
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'pa_values': values})
        col = SegmentCollector()
        status, err, saved = 'complete', '', False
        try:
            self.bus.subscribe(col.on_samples)
            self._g("SAVE_GCODE_STATE NAME=PA_LC\nG90\nM83")
            saved = True
            self._g("SET_VELOCITY_LIMIT ACCEL=%.1f ACCEL_TO_DECEL=%.1f"
                    % (s.accel, s.accel))
            if orig_e_acc is not None:
                ext.max_e_accel = s.accel
            self._g("G1 X%.2f Y%.2f F6000" % (s.test_x, s.test_y))
            for i, pa in enumerate(values):
                if self.cancel_flag:
                    break
                self.current_pa = pa
                self._segment(col, pa, s)
                self.progress = (i + 1) / len(values)
            if self.cancel_flag:
                status = 'cancelled'
        except Exception as e:
            status, err = 'error', str(e)
            gcmd.respond_info("PA_LOADCELL_TEST stopped: %s" % err)
        finally:
            self._cleanup(s, orig_pa, orig_acc, orig_a2d, orig_e_acc, ext,
                          saved, col)
            self.store.finalize(self.run_id, status, err)
            self.state, self.current_pa = 'idle', None
        gcmd.respond_info("Run %d %s: %d PA values saved"
                          % (self.run_id, status,
                             len(self.store.get_run(self.run_id)['segments'])))
        if status == 'error':
            raise PAError(err)
        return self.run_id

    def _segment(self, col, pa, s):
        th = self.toolhead
        self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f" % pa)
        t_base = th.get_last_move_time()      # recording starts at PA change
        col.discard_before(t_base - 0.05)
        markers, partial = [], False
        for k in range(s.sweeps):
            for name, speed in (('slow', s.slow_speed),
                                ('fast', s.fast_speed)):
                if self.cancel_flag:
                    partial = True
                    break
                t0 = th.get_last_move_time()
                self._g("G1 E%.4f F%.2f" % (s.extrude_len, speed * 60.))
                t1 = th.get_last_move_time()
                markers.append({'name': name, 'sweep': k,
                                't0': round(t0 - t_base, 4),
                                't1': round(t1 - t_base, 4)})
                th.dwell(s.settle)
            if partial:
                break
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not markers:
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

    def _cleanup(self, s, orig_pa, acc, a2d, e_acc, ext, saved, col):
        steps = [lambda: self.bus.unsubscribe(col.on_samples),
                 lambda: self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f"
                                 % orig_pa)]
        if acc is not None:
            steps.append(lambda: self._g(
                "SET_VELOCITY_LIMIT ACCEL=%.1f ACCEL_TO_DECEL=%.1f"
                % (acc, a2d if a2d is not None else acc)))
        if e_acc is not None:
            steps.append(lambda: setattr(ext, 'max_e_accel', e_acc))
        steps.append(lambda: self._g("G1 X%.2f Y%.2f F6000"
                                     % (s.park_x, s.park_y)))
        if saved:
            steps.append(lambda: self._g("RESTORE_GCODE_STATE NAME=PA_LC"))
        for fn in steps:        # one failing step must not skip the rest
            try:
                fn()
            except Exception:
                pass
