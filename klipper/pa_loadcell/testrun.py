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
  5. the answer: analysis.piro_pick over everything measured"""
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
            raise PAError("this test could use up to %.0f mm of filament, above "
                          "MAX_FILAMENT %.0f mm: lower CYCLES, FAST_TIME or "
                          "FAST_FLOW, or raise MAX_FILAMENT"
                          % (used, s.max_filament))
        vmax = getattr(ext, 'max_e_velocity', None)
        sm = self.window(ext)
        for name, flow in (('FAST_FLOW', s.fast_flow),
                           ('SLOW_FLOW', s.slow_flow),
                           ('PRIME_FLOW', s.prime_flow)):
            if vmax is not None and flow / area > vmax:
                raise PAError("%s %.1f mm3/s needs %.1f mm/s of filament, above "
                              "max_extrude_only_velocity %.1f"
                              % (name, flow, flow / area, vmax))
        v = s.charge_speed(s.pa_max, area, s.fast_flow, s.slow_flow, sm)
        if vmax is not None and v > vmax:
            raise PAError("artificial PA %.3f needs a %.0f mm/s charge move, "
                          "above max_extrude_only_velocity %.1f: lower PA_MAX "
                          "or FAST_FLOW" % (s.pa_max, v, vmax))
        lo, hi = st.get('axis_minimum'), st.get('axis_maximum')
        if lo is not None:
            for name, (x, y) in (("park", (s.park_x, s.park_y)),
                                 ("test", (s.test_x, s.test_y))):
                if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
                    raise PAError("%s position outside the axis limits" % name)
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
        self.state, self.cancel_flag, self.progress = 'running', False, 0.0
        self.started = self.reactor.monotonic()
        now = self.reactor.monotonic()
        ext = self.toolhead.get_extruder()
        orig_pa = ext.get_status(now).get('pressure_advance', 0.)
        fast_mm, slow_mm = s.leg_mm(area)
        sm = self.window(ext)
        self.note, self.used, self.search = '', 0.0, None
        self.run_id = self.store.new_run('test', {
            'filament': s.filament, 'nozzle_temp': round(temp, 1),
            'settings': asdict(s), 'method': 'PIRO-EDGE',
            'filament_area': round(area, 4),
            'leg_mm': {'fast': round(fast_mm, 4), 'slow': round(slow_mm, 4)},
            'filament_mm': round(s.filament_mm(area), 1),
            'smooth_time': sm,
            'analysis': {'skip_first': bool(s.skip_first)}})
        col = SegmentCollector()
        status, err, saved = 'complete', '', False
        try:
            self.bus.subscribe(col.on_samples)
            # 100 % speed and flow so each leg lasts exactly its planned time
            self._g("SAVE_GCODE_STATE NAME=PA_LC\nG90\nM83\nM220 S100\nM221 S100")
            saved = True
            self._g("G1 X%.2f Y%.2f F6000" % (s.test_x, s.test_y))
            self._set_klipper_pa(0.)         # the charge is put in by hand
            self._prime(s, area, ext)
            if s.prime_rest > 0:
                self.toolhead.dwell(s.prime_rest)
            self._search((col, s, area, sm), gcmd)
            if self.cancel_flag:
                status = 'cancelled'
        except Exception as e:
            status, err = 'error', str(e)
            gcmd.respond_info("PA_LOADCELL_TEST stopped: %s" % err)
        finally:
            self._cleanup(s, orig_pa, saved, col)
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
        col, s, area, sm = ctx
        th = self.toolhead
        fil = s.per_pa_mm(area)
        if self.used + fil > s.max_filament - s.prime_length + 1e-6:
            raise PAError("stopped at MAX_FILAMENT (%.0f mm)" % s.max_filament)
        self.used += fil
        self.current_pa = pa
        if s.rest_time > 0:
            th.dwell(s.rest_time)
        t_base = th.get_last_move_time()      # recording starts here
        self.pa_t = t_base
        col.discard_before(t_base - 0.05)
        markers, partial = self._train_artificial(pa, s, area, sm, t_base)
        th.wait_moves()
        t_end = th.get_last_move_time()
        samples = self._collect(col, t_base, t_end)
        if not any(m['name'] == 'fast' for m in markers):
            return None, None
        seg = {'pa': pa, 'unit': self.bus.unit,
               'rate': round(self.bus.rate, 1), 'markers': markers,
               'partial': partial, 'method': 'artificial',
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

    # -- PIRO-EDGE adaptive search -------------------------------------
    def _search(self, ctx, gcmd):
        col, s, area, sm = ctx
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
            if vmax is not None and s.charge_speed(
                    k, area, s.fast_flow, s.slow_flow, sm) > vmax:
                raise PAError("PA %.3f needs a faster artificial PA charge "
                              "move than max_extrude_only_velocity %.1f "
                              "allows: lower FAST_FLOW" % (k, vmax))
            idx, sc = self._measure(ctx, k, stage, repeat)
            if sc is None:
                if self.cancel_flag:
                    raise _Cancelled()
                raise PAError("no pressure step at PA %.4f: filament "
                              "loaded and the nozzle hot?" % k)
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
                say("PA 0 reference%s" % (" (again)" if attempt else ""))
                measure(0., 'reference', attempt > 0)
                if segs[-1]['piro']['spread'] <= 1.35:
                    break
                say("pressure step not steady yet, one more reference")
            st['floor'] = analysis.floor_from_zero(point(0.)['dip'])
            self.search['floor'] = round(st['floor'], 4)
            # 2. upper limit
            k = s.pa_max
            while not measure(k, 'upper'):
                k = round(k + s.upper_step, 5)
                if k > s.pa_limit + 1e-9:
                    raise PAError("no dip up to PA %.3f: the force never "
                                  "falls below the line after a slow-down. "
                                  "Check the filament path, or raise "
                                  "PA_LIMIT" % (k - s.upper_step))
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
                say("prediction %.4f, testing around it" % pred)
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
                say("PA %.4f is a close call, measuring it again" % p['pa'])
                measure(p['pa'], 'repeat', True)
            # 5. a re-measurement can turn the last clean value into a dip:
            #    make sure a clean value sits close below the corner
            for _ in range(2):
                k_ok, k_bad, _ = bracket()
                if k_ok > 0 and k_bad - k_ok <= max(0.008, 0.25 * k_bad):
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
            say("search budget used up (%d PA values)" % SEARCH_MAX_TRAINS)
        res = analysis.piro_pick(segs, sm)
        self.search.update(result=res.get('pa'), unc=res.get('unc'),
                           grade=res.get('grade'))
        if res.get('pa') is not None:
            say("PA %.4f +- %.4f (%s)" % (res['pa'], res['unc'],
                                          res['grade']))
        else:
            say("no answer: %s" % res.get('why', ''))

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

    def _cleanup(self, s, orig_pa, saved, col):
        steps = [lambda: self.bus.unsubscribe(col.on_samples),
                 lambda: self._g("SET_PRESSURE_ADVANCE ADVANCE=%.5f"
                                 % orig_pa)]
        steps.append(lambda: self._g("G1 X%.2f Y%.2f F6000"
                                     % (s.park_x, s.park_y)))
        if saved:
            steps.append(lambda: self._g("RESTORE_GCODE_STATE NAME=PA_LC"))
        for fn in steps:        # one failing step must not skip the rest
            try:
                fn()
            except Exception:
                pass
