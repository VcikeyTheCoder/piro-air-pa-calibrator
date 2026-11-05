"""Offline tests with a fake printer: no Klipper needed.
Run: python3 tests/test_backend.py"""
import math, os, re, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klipper'))
from pa_loadcell.feed import SampleBus
from pa_loadcell.recorder import ManualRecorder
from pa_loadcell.settings import Settings
from pa_loadcell.storage import RunStore, decimate
from pa_loadcell.testrun import PATestRunner
from pa_loadcell.settings import SEARCH_MAX_TRAINS
from pa_loadcell.errors import PAError


class Printer:
    def __init__(self, temp=210.0, z=20.0):
        self.now = 1000.0
        self.pt = 1000.5           # toolhead print_time
        self.windows = []          # (t0, t1[, rate]) of XY+E moves -> force
        self.emoves, self.eends = [], []   # extrude-only moves (a, b, d, h)
        self.log, self.hooks = [], []
        self.temp, self.z = temp, z
        self.sent = 1000.0
        self.client = None
        self.objs = {'gcode': Gcode(self), 'toolhead': Toolhead(self),
                     'load_cell': LoadCell(self)}
    def get_reactor(self): return self
    def monotonic(self): return self.now
    def pause(self, t):
        self.now = max(t, self.now)
        for h in self.hooks: h()
    def lookup_object(self, n, d=None): return self.objs.get(n, d)
    def lookup_objects(self, module=None):
        return [(n, o) for n, o in self.objs.items()
                if module is None or n == module or n.startswith(module + ' ')]
    def register_event_handler(self, ev, cb):
        self.__dict__.setdefault('events', []).append((ev, cb))
    def send_event(self, ev, *a):
        for e, cb in self.__dict__.get('events', []):
            if e == ev: cb(*a)
    # extrude-only moves, each at constant speed over its Klipper duration,
    # smoothed like Klipper does (triangle over smooth_time) while PA != 0
    @staticmethod
    def _R(x, h):                   # triangle-smoothed max(x, 0)
        if h <= 0: return max(x, 0.)
        if x <= -h: return 0.
        if x >= h: return x
        if x <= 0: return (x + h) ** 3 / (6 * h * h)
        return x + (h - x) ** 3 / (6 * h * h)
    @staticmethod
    def _F(x, h):                   # its slope
        if h <= 0: return 1. if x > 0 else 0.
        if x <= -h: return 0.
        if x >= h: return 1.
        if x <= 0: return (x + h) ** 2 / (2 * h * h)
        return 1 - (h - x) ** 2 / (2 * h * h)
    def _near(self, t0, t1):
        import bisect
        i = bisect.bisect_right(self.eends, t0 - 0.11)
        while i < len(self.emoves) and self.emoves[i][0] - 0.11 < t1:
            yield self.emoves[i]; i += 1
    def e_delta(self, t0, t1):
        """filament pushed by extrude-only moves over [t0, t1)"""
        out = 0.
        for a, b, d, h in self._near(t0, t1):
            k = d / (b - a)
            out += k * (self._R(t1 - a, h) - self._R(t1 - b, h)
                        - self._R(t0 - a, h) + self._R(t0 - b, h))
        return out
    def e_rate(self, t):
        return sum(d / (b - a) * (self._F(t - a, h) - self._F(t - b, h))
                   for a, b, d, h in self._near(t, t))

class Extruder:
    max_e_velocity, max_e_accel, max_e_dist = 100.0, 5000.0, 50.0
    filament_area = 3.14159265 * 0.875 ** 2
    max_extrude_ratio = 120.0 / filament_area     # max_extrude_cross_section 120
    def __init__(s, p): s.p = p
    def get_status(s, now):
        return {'temperature': s.p.temp, 'target': s.p.temp, 'pressure_advance': 0.05}

class Toolhead:
    def __init__(s, p): s.p, s.ext = p, Extruder(p)
    def _fl(s): s.p.objs['gcode'].flush()
    def get_last_move_time(s): s._fl(); return s.p.pt
    def dwell(s, d): s._fl(); s.p.pt += d
    def wait_moves(s): s._fl()
    def get_position(s): return [0, 0, s.p.z, 0]
    def get_extruder(s): return s.ext
    def get_status(s, now):
        return {'homed_axes': 'xyz', 'axis_minimum': [0, 0, 0],
                'axis_maximum': [250, 250, 250], 'max_accel': 4000,
                'max_accel_to_decel': 2000, 'max_velocity': 300,
                'minimum_cruise_ratio': 0.5}

class Gcode:
    """Times E moves like Klipper: an XY+E move runs at F over the XY
    distance, an extrude-only move at F over the E distance. XY+E moves are
    planned like Klipper's lookahead (same direction: the lower speed at the
    junction, a turn: 0, accel from SET_VELOCITY_LIMIT) and get Klipper's
    pressure advance: position x + K * dx/dt, smoothed with Klipper's
    triangle weighting over smooth_time (kin_extruder.c) while PA != 0.
    Extrude-only moves get the smoothing but no PA term, as in Klipper."""
    PIECE = 0.0002
    def __init__(s, p):
        s.p, s.n_e, s.pos = p, 0, {'X': 0.0, 'Y': 0.0}
        s.pa, s.sm, s.accel, s.mcr, s.pending = 0.0, 0.04, 4000.0, 0.5, []
    def flush(s):
        mv, s.pending = s.pending, []
        if not mv: return
        n = len(mv)
        for i, m in enumerate(mv):          # junction speeds
            prev = mv[i - 1] if i else None
            m['v0'] = 0. if not prev or prev['dir'] != m['dir'] else min(prev['v'], m['v'])
        for i, m in enumerate(mv):
            m['v1'] = mv[i + 1]['v0'] if i + 1 < n else 0.
        a, t, segs = s.accel, s.p.pt, []
        for m in mv:
            v0, vc, v1 = m['v0'], m['v'], m['v1']
            ta, td = (vc - v0) / a, (vc - v1) / a
            tc = max(0., m['d'] - (vc * vc - v0 * v0) / (2 * a) - (vc * vc - v1 * v1) / (2 * a)) / vc
            segs.append((t, ta, tc, td, v0, vc, v1, m['r'])); t += ta + tc + td
        # extruder target x(t) + K * u(t) (u = filament speed), cut into
        # short linear pieces through the ramps; Klipper's triangle
        # smoothing (on while PA != 0) is then applied exactly per piece
        h, K, P = (s.sm / 2. if s.pa else 0.), s.pa, s.PIECE
        for (ts, ta, tc, td, v0, vc, v1, r) in segs:
            for t_a, dur, ua, acc in ((ts, ta, v0, a), (ts + ta, tc, vc, 0.),
                                      (ts + ta + tc, td, vc, -a)):
                if dur <= 1e-12: continue
                n = max(1, int(dur / P + 0.999)) if acc else 1
                for i in range(n):
                    x0, x1 = dur * i / n, dur * (i + 1) / n
                    dx = r * (ua * (x1 - x0) + 0.5 * acc * (x1 * x1 - x0 * x0))
                    dv = r * acc * (x1 - x0)
                    s.p.emoves.append((t_a + x0, t_a + x1, dx + K * dv, h))
                    s.p.eends.append(t_a + x1)
        t1 = t
        s.p.pt = t1
    def run_script_from_command(s, script):
        for line in script.split('\n'):
            s.p.log.append(line)
            m = re.match(r'SET_PRESSURE_ADVANCE ADVANCE=([-\d.]+)(?: SMOOTH_TIME=([\d.]+))?', line)
            if m:
                s.pa = float(m.group(1))
                if m.group(2): s.sm = float(m.group(2))
            m = re.match(r'SET_VELOCITY_LIMIT ACCEL=([\d.]+)(?: MINIMUM_CRUISE_RATIO=([\d.]+))?', line)
            if m:
                s.accel = float(m.group(1))
                if m.group(2) is not None: s.mcr = float(m.group(2))
            w = dict((k, float(v)) for k, v in re.findall(r'([XYEF])([-\d.]+)', line)) if line.startswith('G1') else {}
            xy = [(a, w[a] - s.pos[a]) for a in 'XY' if a in w and abs(w[a] - s.pos[a]) > 0]
            if 'E' in w and 'F' in w and xy and w['E'] > 0:
                ax, d = xy[0]
                s.pending.append({'d': abs(d), 'dir': (ax, d > 0), 'v': w['F'] / 60.,
                                  'r': w['E'] / abs(d)})
                s.pos[ax] = w[ax]; s.n_e += 1
                continue
            s.flush()
            if 'E' in w and 'F' in w:
                # extrude-only, as Klipper's lookahead plans it: from and to
                # rest at max_extrude_only_accel, cruise at F (<= max_e_velocity)
                # and <= what max_accel x (1 - minimum_cruise_ratio) reaches
                ext = s.p.objs['toolhead'].ext
                d = abs(w['E'])
                if d > ext.max_e_dist:
                    raise PAError("Extrude only move too long (%.3fmm vs %.3fmm)"
                                  % (d, ext.max_e_dist))
                ea = ext.max_e_accel
                v2 = min(min(w['F'] / 60., ext.max_e_velocity) ** 2, d * ea,
                         d * s.accel * (1 - s.mcr))
                v = v2 ** .5
                dur = 2 * v / ea + (d - v2 / ea) / v
                h = s.sm / 2. if s.pa else 0.
                s.p.emoves.append((s.p.pt, s.p.pt + dur, w['E'], h))
                s.p.eends.append(s.p.pt + dur)
                s.p.pt += dur
                s.n_e += 1
            m = re.match(r'G4 P([\d.]+)', line)
            if m: s.p.pt += float(m.group(1)) / 1000.
            for a in 'XY':
                if a in w: s.pos[a] = w[a]

class LoadCell:
    """Mimics klippy/extras/load_cell.py: add_client callbacks receive
    [time, grams, counts, tare_counts] rows and must return TRUE to stay."""
    def __init__(s, p): s.p = p
    def get_sensor(s): return s
    def is_calibrated(s): return True
    def is_tared(s): return True
    def add_client(s, cb): s.p.client = cb; s.p.hooks.append(s.pump)
    def pump(s):
        p = s.p
        if not p.client: return
        n0 = int((p.sent - 1000) * 320)
        n1 = int((p.pt - 1000) * 320) + 2  # stream runs past the last move
        if n1 <= n0: return
        data = []
        for i in range(n0, n1):
            t = 1000 + i / 320.0
            v = sum((w[2] if len(w) > 2 else 10.0)
                    for w in p.windows if w[0] <= t < w[1]) + p.e_rate(t)
            counts = v * 1000.0          # 8 mm/s fast leg -> 80 g
            data.append((t, counts / 100.0, counts, 0))
        p.sent = 1000 + n1 / 320.0
        if not p.client({'data': data}):      # falsy = unregister
            p.client = None

class LagCell(LoadCell):
    """Load cell on an extruder with melt lag: the nozzle flow follows the
    pushed filament speed with time constant `tau`, so the right PA is tau.
    Force = 20 g per mm/s of nozzle flow + a little noise."""
    def __init__(s, p, tau, noise=0.15):
        super().__init__(p); s.tau, s.q, s.wi = tau, 0.0, 0
        import random; s.rng, s.noise = random.Random(1), noise
    def vel(s, t0, t1):
        """Mean filament speed over [t0, t1): exact, so a 40 ms charge move
        is not rounded to whole samples."""
        w = s.p.windows
        while s.wi < len(w) and w[s.wi][1] <= t0: s.wi += 1
        e, i = 0.0, s.wi
        while i < len(w) and w[i][0] < t1:
            a, b = max(w[i][0], t0), min(w[i][1], t1)
            if b > a: e += (w[i][2] if len(w[i]) > 2 else 10.0) * (b - a)
            i += 1
        return (e + s.p.e_delta(t0, t1)) / (t1 - t0)
    def pump(s):
        p = s.p
        if not p.client: return
        n0, n1 = int((p.sent - 1000) * 320), int((p.pt - 1000) * 320) + 2
        if n1 <= n0: return
        data, dt = [], 1 / 320.0
        import math
        k = math.exp(-dt / s.tau)
        for i in range(n0, n1):
            t = 1000 + i / 320.0
            s.q = s.vel(t - dt, t) + (s.q - s.vel(t - dt, t)) * k
            g = 20.0 * s.q + s.rng.gauss(0, s.noise)
            data.append((t, g, g * 100, 0))
        p.sent = 1000 + n1 / 320.0
        if not p.client({'data': data}): p.client = None

class FillCell(LagCell):
    """A nozzle that starts nearly empty: the pushing force only reaches
    its full size after `fill_mm` of filament (Run 64)."""
    def __init__(s, p, tau, fill_mm, start=0.05):
        super().__init__(p, tau); s.fill_mm, s.start, s.cum = fill_mm, start, 0.0
    def pump(s):
        p = s.p
        if not p.client: return
        n0, n1 = int((p.sent - 1000) * 320), int((p.pt - 1000) * 320) + 2
        if n1 <= n0: return
        data, dt = [], 1 / 320.0
        k = math.exp(-dt / s.tau)
        for i in range(n0, n1):
            t = 1000 + i / 320.0
            u = s.vel(t - dt, t)
            s.cum += u * dt
            s.q = u + (s.q - u) * k
            fill = min(1.0, s.start + (1 - s.start) * s.cum / s.fill_mm)
            g = 20.0 * s.q * fill + s.rng.gauss(0, s.noise)
            data.append((t, g, g * 100, 0))
        p.sent = 1000 + n1 / 320.0
        if not p.client({'data': data}): p.client = None

class Gcmd:
    def __init__(s): s.out = []
    def respond_info(s, m): s.out.append(m)


from pa_loadcell import analysis


def make(temp=210.0, z=20.0, tau=None, noise=0.15, fill=None):
    p = Printer(temp, z)
    if tau: p.objs['load_cell'] = FillCell(p, tau, fill) if fill else LagCell(p, tau, noise)
    bus = SampleBus(p, 'load_cell', 60000, 15.0)
    store = RunStore(tempfile.mkdtemp())
    lim = {'min_temp': 200, 'required_z': 20, 'z_tol': 0.5, 'timeout': 3}
    return p, bus, store, PATestRunner(p, bus, store, lim)

def S(**kw):
    d = dict(cycles=3, test_x=100, test_y=100, park_x=10, park_y=10, method='push',
             prime_steady=0)
    d.update(kw); s = Settings(**d); s.validate(); return s


def test_cancel_saves_partial():
    p, bus, store, r = make()
    g = p.objs['gcode']; orig = g.run_script_from_command
    def hook(script):
        orig(script)
        if g.n_e >= 60 and r.state == 'running': r.cancel()
    g.run_script_from_command = hook
    rid = r.run(Gcmd(), S())
    m = store.get_run(rid)
    assert m['status'] == 'cancelled' and 1 <= len(m['segments']) < 3, m

def test_sensor_timeout_is_an_error():
    p, bus, store, r = make()
    p.objs['load_cell'].pump = lambda: None   # sensor goes silent
    p.objs['load_cell'].add_client = lambda cb: None
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'timed out' in str(e), e
    assert store.get_run(1)['status'] == 'error'

def test_manual_record_and_tags():
    p, bus, store, r = make()
    rec = ManualRecorder(p, bus, store, 300)
    rec.start('PLA')
    p.windows.append((1001, 1002)); p.pt = 1003; p.pause(p.now + 1)
    rid = rec.stop()
    m = store.get_run(rid)
    assert m['kind'] == 'manual' and m['segments'][0]['samples'] > 100
    store.set_tag(rid, pa_pick=0.04, notes='looks good', name='Mine')
    assert store.list_runs()[0]['pa_pick'] == 0.04
    store.delete(rid); assert store.list_runs() == []

def test_live_cursor_and_decimate():
    p, bus, store, r = make()
    a = bus.live(0); p.pt = 1002; p.pause(p.now + 1); b = bus.live(a['seq'])
    assert b['seq'] > a['seq'] and b['gap'] is False
    c = bus.live(b['seq']); assert c['samples'] == []
    t = [i / 100 for i in range(10000)]; f = [i % 7 for i in range(10000)]
    dt, df = decimate(t, f, 1000); assert len(dt) <= 1000 and max(df) == 6


def _synth(tau, ring=0.0, rate=320.0, sweeps=4, fast=0.6, slow=0.8):
    """slow/fast/slow force trace through a lag (optionally 2nd order)."""
    import math
    t, f, markers, x, y, v = [], [], [], 0.0, 0.0, 0.0
    plan, tt = [], 0.0
    for k in range(sweeps):
        plan.append(('slow', k, tt, tt + slow, 1.0)); tt += slow + 0.3
        plan.append(('fast', k, tt, tt + fast, 4.0)); tt += fast + 0.3
    for n in range(int(tt * rate)):
        now = n / rate
        u = 0.0
        for nm, k, a, b, val in plan:
            if a <= now < b: u = val
        # 1st order lag; ring>0 adds a derivative kick = overshoot
        dy = (u - y) / tau
        y += dy / rate
        out = y + ring * (u - y) if ring else y
        t.append(round(now, 4)); f.append(out * 25.0)
    for nm, k, a, b, val in plan:
        markers.append({'name': nm, 'sweep': k, 't0': a, 't1': b})
    return t, f, markers

def test_edge_summary_first_order():
    from pa_loadcell import analysis
    t, f, m = _synth(0.05)
    r = analysis.edge_summary(t, f, m, 'rise')
    assert r and r['n'] == 4, r
    assert abs(r['rise'] - 2.197 * 0.05) < 0.02, r['rise']      # 10-90 %
    assert r['overshoot'] < 0.03, r['overshoot']
    slow = analysis.edge_summary(*_synth(0.15)[:2], _synth(0.15)[2], 'rise')
    assert slow['rise'] > r['rise'] * 2                         # lag shows up
    assert analysis.edge_summary(t, f, [], 'rise') is None

def test_edge_overshoot_detected():
    from pa_loadcell import analysis
    t, f, m = _synth(0.05, ring=1.3)
    r = analysis.edge_summary(t, f, m, 'rise')
    assert r["overshoot"] > 0.15, r["overshoot"]

def test_unit_flip_and_reset():
    p, bus, store, r = make()
    bus.ensure()
    cb = p.client
    bus._on_batch({'data': [(1000.0 + i / 320, None, 5000 + i, 0)
                            for i in range(50)]}, bus.gen)
    assert bus.unit == 'counts' and bus.total == 50
    a = bus.live(50)
    bus._on_batch({'data': [(1001.0 + i / 320, 1.5, 5000, 0)
                            for i in range(50)]}, bus.gen)
    assert bus.unit == 'g' and bus.total == 50 and bus.unit_gen == 1
    b = bus.live(9999)                       # stale cursor from before reset
    assert b['reset'] is True and len(b['samples']) == 50
    assert bus.info()['source'] == 'load_cell'

def test_manual_marks():
    p, bus, store, r = make()
    rec = ManualRecorder(p, bus, store, 300)
    rec.start('PLA'); p.windows.append((1001, 1002)); p.pt = 1003
    p.pause(p.now + 1); rec.mark('pa 0.04'); rec.mark()
    rid = rec.stop()
    seg = store.get_segment(rid, 0)
    assert [m['label'] for m in seg['markers']] == ['pa 0.04', 'mark #2']
    assert seg['markers'][0]['t0'] > 0

def test_falling_edge_stays_inside_rest_period():
    from pa_loadcell import analysis
    t, f, m = _synth(0.05)
    r = analysis.edge_summary(t, f, m, 'fall')
    assert r and r['post'] <= 0.3 and r['pre'] >= 0.1, r
    assert abs(r['rise'] - 2.197 * 0.05) < 0.02, r['rise']
    # short rest + short fast phase must shrink the windows, not leak into
    # the next phase: baseline stays flat (no step inside the window)
    t2, f2, m2 = _synth(0.05, fast=0.35)
    r2 = analysis.edge_summary(t2, f2, m2, 'rise')
    assert r2 and r2['overshoot'] < 0.05, r2

def test_sharp_edge_still_gets_rise_time():
    from pa_loadcell import analysis
    t, f, m = _synth(0.004)          # edge faster than the 10 ms analysis grid
    r = analysis.edge_summary(t, f, m, 'rise')
    assert r and r['rise'] is not None and r['rise'] < 0.03, r

def test_segment_time_range():
    import tempfile
    from pa_loadcell.storage import RunStore
    st = RunStore(tempfile.mkdtemp())
    rid = st.new_run('manual', {})
    tt = [i / 320.0 for i in range(320 * 20)]
    st.add_segment(rid, {'t': tt, 'f': [float(i % 7) for i in range(len(tt))]})
    full = st.get_segment(rid, 0)
    assert len(full['t']) == len(tt) and full['span'] == [0.0, tt[-1]]
    part = st.get_segment(rid, 0, 0, 5.0, 7.0)
    assert part['t'][0] >= 5.0 and part['t'][-1] <= 7.0
    assert abs(len(part['t']) - 641) <= 1 and not part['decimated']
    thin = st.get_segment(rid, 0, 200, 5.0, 7.0)
    assert len(thin['t']) <= 200 and thin['decimated']
    assert st.get_segment(rid, 0, 0, 30.0, 40.0)['t'] == []

def test_back_to_back_windows():
    from pa_loadcell import analysis
    m = [{'name': 'slow', 't0': 0, 't1': 1.0}, {'name': 'fast', 't0': 1.0, 't1': 1.25},
         {'name': 'slow', 't0': 1.25, 't1': 2.25}]
    (te, pre, post), = analysis._windows(m, 'rise', 2.25)
    assert te == 1.0 and abs(pre - 0.3) < 1e-9 and abs(post - 0.21) < 1e-9
    (te, pre, post), = analysis._windows(m, 'fall', 2.25)
    assert te == 1.25 and abs(pre - 0.21) < 1e-9 and abs(post - 0.96) < 1e-9

def test_skip_first_pulse():
    from pa_loadcell import analysis
    t, f, m = _synth(0.05)
    a = analysis.edge_summary(t, f, m, 'rise')
    b = analysis.edge_summary(t, f, m, 'rise', skip_first=True)
    assert a['n'] == 4 and b['n'] == 3 and b['skipped_first']

def test_command_fit():
    from pa_loadcell import analysis
    t = [i / 320 for i in range(320 * 4)]
    mk = [{'name': 'slow', 't0': 0, 't1': 1}, {'name': 'fast', 't0': 1, 't1': 2},
          {'name': 'slow', 't0': 2, 't1': 4}]
    f = [7 + 3 * (19.24 if 1 <= x < 2 else 1.92) for x in t]
    fit = analysis.command_fit(t, f, mk, {'fast': 19.24, 'slow': 1.92})
    assert abs(fit['gain'] - 3) < 0.01 and abs(fit['offset'] - 7) < 0.05, fit
    assert analysis.command_fit(t, f, mk, None) is None

def _synth_b2b(tau, ring=0.0, loops=6, fast=0.6, slow=1.0, rate=320.0):
    """back-to-back legs like the real test: lead-in slow, then fast/slow."""
    plan, tt = [('slow', -1, 0.0, slow)], slow
    for k in range(loops):
        plan.append(('fast', k, tt, tt + fast)); tt += fast
        plan.append(('slow', k, tt, tt + slow)); tt += slow
    t, f, y = [], [], 1.0
    for n in range(int(tt * rate)):
        now = n / rate
        u = 4.0 if any(nm == 'fast' and a <= now < b for nm, k, a, b in plan) else 1.0
        y += (u - y) / tau / rate
        t.append(round(now, 4)); f.append((y + ring * (u - y)) * 25.0)
    return t, f, [{'name': nm, 'sweep': k, 't0': a, 't1': b} for nm, k, a, b in plan]


# ---------------------------------------------------------------------
# PIRO-EDGE
def test_full_search_finds_pa():
    """Defaults: push method, MODE=fall. With Klipper's smoothing the zero
    fall-area PA sits a little above the melt lag (0.045 here)"""
    p, bus, store, r = make(tau=0.045)
    g = Gcmd()
    rid = r.run(g, S())
    m = store.get_run(rid)
    assert m['status'] == 'complete', m.get('error')
    res = m['analysis']['result']
    assert res['method'] == 'PIRO-FALL' and abs(res['pa'] - 0.049) < 0.003, res['pa']
    assert res['area_result']['pa'] is not None and res['area_fall']['k'] is not None
    assert res['grade'] == 'good' and res['unc'] > 0, (res['grade'], res['warnings'])
    pas = [x['pa'] for x in m['segments']]
    assert pas[:2] == [0.0, 0.1] and len(pas) <= 7, pas          # MAX_VALUES
    st = [x['stage'] for x in m['search']['steps']]
    assert st[:2] == ['reference', 'upper'] and set(st[2:]) == {'area'}, st
    k = res['pa']
    assert sum(1 for x in pas if k < x <= k + 0.015) >= 2 and \
        sum(1 for x in pas if k - 0.015 <= x < k) >= 2, pas
    assert m['search']['result'] == res['pa']
    assert m['search']['steps'][1]['balance'] > 0 > m['search']['steps'][0]['balance']
    ar = res['area']
    assert ar['r2'] > 0.99 and [n['n'] for n in ar['narrow']] == list(range(2, len(pas) + 1))
    assert res['edge_result']['pa'] is not None                   # both kept
    # Klipper PA tiny (smoothing on) during the test, restored after; no XY
    assert [l for l in p.log if l.startswith('SET_PRESSURE_ADVANCE')] == [
        'SET_PRESSURE_ADVANCE ADVANCE=0.000001',
        'SET_PRESSURE_ADVANCE ADVANCE=0.05000']
    xy = [l for l in p.log if re.match(r'G1 [XY]', l)]
    assert xy == ['G1 X100.00 Y100.00 F6000', 'G1 X10.00 Y10.00 F6000'], xy
    # accel and cruise ratio lifted for every train only (no XY then)
    vl = [l for l in p.log if l.startswith('SET_VELOCITY_LIMIT') or re.match(r'G1 [XY]', l)]
    up, back = ('SET_VELOCITY_LIMIT ACCEL=10000000 MINIMUM_CRUISE_RATIO=0',
                'SET_VELOCITY_LIMIT ACCEL=4000.0 MINIMUM_CRUISE_RATIO=0.500')
    assert vl == [xy[0]] + [up, back] * len(pas) + [back, xy[1]], vl
    assert p.objs['gcode'].accel == 4000 and p.objs['gcode'].mcr == 0.5
    ext = p.objs['toolhead'].ext                                  # limits put back
    assert (ext.max_e_velocity, ext.max_e_accel) == (100.0, 5000.0)
    # prime: extrude-only moves totalling 20 mm before the first pulses
    i_pa = p.log.index('SET_PRESSURE_ADVANCE ADVANCE=0.000001')
    prime, j = 0., i_pa + 1                 # pieces of at most ~2 s
    while p.log[j].startswith('G1 E') and prime < 20 - 1e-6:
        prime += float(p.log[j].split()[1][1:]); j += 1
    assert abs(prime - 20) < 1e-3 and j - i_pa - 1 == 3, (prime, p.log[i_pa + 1:j])   # 3 equal pieces
    seg = store.get_segment(rid, 1)
    assert seg['method'] == 'push'
    names = [k['name'] for k in seg['markers']]
    assert names == ['slow'] + ['fast', 'slow'] * 3, names
    f = [k for k in seg['markers'] if k['name'] == 'fast'][0]
    assert abs(f['t1'] - f['t0'] - 0.6) < 0.0002, f              # FAST_TIME
    e = m['segments'][1]
    assert e['piro']['n'] == 2 and e['piro']['area']['n'] == 2    # first pulse skipped
    assert 'PIRO-FALL PA' in g.out[-1], g.out[-1]
    assert m['push']['accel'] == 4000 and abs(m['push']['ramp'] - 0.00469) < 1e-4

def test_klipper_method_finds_pa():
    """PA_METHOD=klipper: back-and-forth line, Klipper's own PA, no conversion"""
    p, bus, store, r = make(tau=0.045)
    p.windows.sort()
    rid = r.run(Gcmd(), S(method='klipper', mode='edge'))
    m = store.get_run(rid)
    assert m['status'] == 'complete' and m['pa_method'] == 'klipper', m['status']
    res = m['analysis']['result']
    assert res['pa'] is not None and abs(res['pa'] - 0.045) < 0.004, res
    assert 'measured' not in res, res                     # no smooth time conversion
    ln = m['line']
    assert 15 < ln['length'] < 18 and abs(ln['cross_section'] - 0.544) < 0.01, ln
    # the line: only Y moves around test_y, inside the line ends
    ys = [float(x) for l in p.log for x in re.findall(r'G1 Y([-\d.]+) E', l)]
    assert ys and min(ys) >= 100 - ln['length'] / 2 - 1e-3 and max(ys) <= 100 + ln['length'] / 2 + 1e-3
    # PA set by Klipper for every value, accel limits set and restored
    pas = [l for l in p.log if l.startswith('SET_PRESSURE_ADVANCE')]
    assert pas[0] == 'SET_PRESSURE_ADVANCE ADVANCE=0.000000' and pas[-1] == 'SET_PRESSURE_ADVANCE ADVANCE=0.05000', pas
    assert len(pas) == len(m['segments']) + 2, (len(pas), len(m['segments']))
    vl = [l for l in p.log if l.startswith('SET_VELOCITY_LIMIT')]
    assert vl == ['SET_VELOCITY_LIMIT ACCEL=4000.0 MINIMUM_CRUISE_RATIO=0',
                  'SET_VELOCITY_LIMIT ACCEL=4000.0 MINIMUM_CRUISE_RATIO=0.500'], vl
    seg = store.get_segment(rid, 1)
    assert seg['method'] == 'klipper'
    f = [k for k in seg['markers'] if k['name'] == 'fast'][0]
    assert abs(f['t1'] - f['t0'] - 0.6) < 0.01, f

def test_klipper_method_checks():
    p, bus, store, r = make(tau=0.045)
    try: r.run(Gcmd(), S(method='klipper', test_y=3)); assert False
    except PAError as e: assert 'does not fit' in str(e), e
    p, bus, store, r = make(tau=0.045)
    p.objs['toolhead'].get_status = lambda now: {'homed_axes': 'xyz', 'axis_minimum': [0, 0, 0],
        'axis_maximum': [250, 250, 250], 'max_accel': 4000, 'max_velocity': 10}
    try: r.run(Gcmd(), S(method='klipper')); assert False
    except PAError as e: assert 'max_velocity' in str(e), e
    for bad in (dict(method='wobble'), dict(move_axis='Z'), dict(smooth_time=0.5)):
        try: S(**bad); assert False, bad
        except PAError: pass

def test_auto_apply_and_reuse():
    """APPLY=1 + RESTRICT=0: test where the nozzle is (no Z check, no XY
    moves), set the result, reuse it for the next print"""
    p, bus, store, r = make(tau=0.045, z=5.0)
    g = Gcmd()
    s = S(apply=1, restrict=0, reuse_hours=1, filament='Fibreel red', fast_flow=14)
    v = r.auto(g, s)
    assert abs(v - 0.049) < 0.004, (v, g.out)
    assert not [l for l in p.log if re.match(r'G1 [XY]', l)], 'no XY moves'
    pas = [l for l in p.log if l.startswith('SET_PRESSURE_ADVANCE')]
    assert pas[-1] == 'SET_PRESSURE_ADVANCE ADVANCE=%.5f' % v, pas
    assert 'applied' in g.out[-1] and 'PIRO-EDGE: PA' in g.out[-1], g.out
    rid, m = store.last_auto()
    a = m['auto']
    assert a['applied'] == round(v, 4) and not a['fallback'] and a['wall_flow'] == 14
    assert a['target_temp'] == 210.0 and a['filament'] == 'Fibreel red'
    assert any(x['auto'] for x in store.list_runs())
    # same conditions: reused, no new run
    n_runs, n_log = len(store.list_runs()), len(p.log)
    g2 = Gcmd()
    assert r.auto(g2, S(apply=1, restrict=0, reuse_hours=1,
                        filament=' fibreel  RED', fast_flow=14.3)) == a['applied']
    assert len(store.list_runs()) == n_runs and 'reused from Run %d' % rid in g2.out[-1]
    assert p.log[n_log:] == ['SET_PRESSURE_ADVANCE ADVANCE=%.5f' % a['applied']]
    assert store.last_auto()[1]['auto']['reused'] == 1
    # anything different, too old, or REUSE_HOURS=0: a new test
    tgt, sm = 210.0, 0.04
    for kw in (dict(filament='PETG'), dict(fast_flow=16), dict(method='klipper'),
               dict(filament='')):
        d = dict(apply=1, restrict=0, reuse_hours=1, filament='Fibreel red', fast_flow=14)
        d.update(kw)
        assert r._reuse(S(**d), tgt, sm) is None, kw
    assert r._reuse(S(apply=1, reuse_hours=1, filament='Fibreel red', fast_flow=14), 214.0, sm) is None
    assert r._reuse(S(apply=1, reuse_hours=1, filament='Fibreel red', fast_flow=14), 211.0, sm)
    store.update_auto(rid, {'time': a['time'] - 2 * 3600})
    assert r._reuse(S(apply=1, reuse_hours=1, filament='Fibreel red', fast_flow=14), tgt, sm) is None

def test_auto_fallback_never_raises():
    # cannot start (too cold): FAIL_PA, no run, no error
    p, bus, store, r = make(temp=150.0, z=5.0)
    g = Gcmd()
    assert r.auto(g, S(apply=1, restrict=0, fail_pa=0.02)) == 0.02
    assert 'test failed' in g.out[-1] and 'min_temp' in g.out[-1], g.out
    assert p.log[-1] == 'SET_PRESSURE_ADVANCE ADVANCE=0.02000' and not store.list_runs()
    # no pressure response at all: the run ends in an error -> FAIL_PA, recorded
    p, bus, store, r = make(z=5.0)
    g = Gcmd()
    assert r.auto(g, S(apply=1, restrict=0, fail_pa=0.03, filament='x', reuse_hours=1,
                       mode='edge')) == 0.03
    rid, m = store.last_auto()
    assert m['status'] == 'error' and m['auto']['fallback'] and m['auto']['applied'] == 0.03
    assert p.log[-1] == 'SET_PRESSURE_ADVANCE ADVANCE=0.03000', p.log[-3:]
    assert r._reuse(S(apply=1, filament='x', reuse_hours=1, mode='edge'), 210.0, 0.04) is None
    # cancelled from the page: FAIL_PA, the print goes on
    p, bus, store, r = make(tau=0.045, z=5.0)
    gc = p.objs['gcode']; orig = gc.run_script_from_command
    def hook(script):
        orig(script)
        if gc.n_e >= 100 and r.state == 'running': r.cancel()
    gc.run_script_from_command = hook
    g = Gcmd()
    assert r.auto(g, S(apply=1, restrict=0)) == 0.02
    assert 'cancelled' in g.out[-1], g.out
    # a test already running: FAIL_PA, the running test is left alone
    p, bus, store, r = make(tau=0.045)
    r.state, r.run_id = 'running', 7
    g = Gcmd()
    assert r.auto(g, S(apply=1)) == 0.02 and r.run_id == 7 and r.state == 'running'
    # without APPLY a failure still raises
    p, bus, store, r = make(temp=150.0)
    try: r.run(Gcmd(), S()); assert False
    except PAError: pass

def test_restrict_klipper_line_slides_inside():
    """RESTRICT=0 + klipper method next to the axis edge: the line is moved
    inside the limits, centred as close to the nozzle as possible"""
    p, bus, store, r = make(tau=0.045, z=5.0)       # nozzle at X0 Y0
    rid = r.run(Gcmd(), S(method='klipper', restrict=0))
    m = store.get_run(rid)
    assert m['status'] == 'complete', m.get('error')
    ln = m['line']['length']
    ys = [float(x) for l in p.log for x in re.findall(r'G1 Y([-\d.]+) E', l)]
    assert ys and min(ys) >= 0 and max(ys) <= ln + 1.01, (min(ys), max(ys))
    assert abs(m['settings']['test_y'] - (ln / 2 + 0.5)) < 0.01, m['settings']['test_y']

def test_klipper_line_length_and_direction():
    """LINE_LENGTH sets the line (filament per mm follows), LINE_DIR starts it
    at the test position and runs towards + or -"""
    area = 3.14159265 * 0.875 ** 2
    ln = S(method='klipper', line_length=30).line(area)
    assert abs(ln['length'] - 30) < 1e-9, ln
    assert abs(ln['ratio'] - (12 * 0.6 + 1.8 * 1.0) / (area * 30)) < 1e-12
    assert abs(S(method='klipper').line(area)['length'] - 16.5) < 0.2
    # starts at the nozzle (Y0) and runs towards +
    p, bus, store, r = make(tau=0.045, z=5.0)
    rid = r.run(Gcmd(), S(method='klipper', restrict=0, line_dir=1, line_length=30, mode='edge'))
    m = store.get_run(rid)
    assert m['status'] == 'complete', m.get('error')
    ys = [float(x) for l in p.log for x in re.findall(r'G1 Y([-\d.]+) E', l)]
    starts = [float(x) for l in p.log for x in re.findall(r'G1 X[-\d.]+ Y([-\d.]+) F6000', l)]
    assert min(ys) >= 0.5 - 1e-6 and max(ys) <= 30.5 + 1e-3, (min(ys), max(ys))
    assert abs(starts[0] - 0.5) < 1e-3, starts        # slid 0.5 mm off the edge
    assert abs(m['analysis']['result']['pa'] - 0.045) < 0.005
    # towards - from the edge: slid inside, still runs towards -
    p, bus, store, r = make(tau=0.045, z=5.0)
    rid = r.run(Gcmd(), S(method='klipper', restrict=0, line_dir=-1, line_length=30))
    ys = [float(x) for l in p.log for x in re.findall(r'G1 Y([-\d.]+) E', l)]
    assert min(ys) >= 0.5 - 1e-6 and max(ys) <= 30.5 + 1e-3, (min(ys), max(ys))
    assert ys[0] < 30.5, ys[:3]                        # first move goes towards -
    # too short for max_extrude_cross_section
    p, bus, store, r = make(tau=0.045)
    p.objs['toolhead'].ext.max_extrude_ratio = 0.64 / area
    try: r.run(Gcmd(), S(method='klipper', line_length=10)); assert False
    except PAError as e: assert 'LINE_LENGTH 10.0 mm is too short' in str(e), e
    for bad in (dict(line_dir=2), dict(line_length=-1)):
        try: S(**bad); assert False, bad
        except PAError: pass

def _straight_klipper(r):
    """Klipper's own PA on an endless straight line (no turns): what the
    push method copies, without the XY motion"""
    def train(s, ln, area, t_base):
        vf, vs, a = ln['v_fast'], ln['v_slow'], r.accel
        seq = [(vs * s.slow_time, vs, 0., vs)]
        for k in range(s.cycles):
            seq += [(vf * s.fast_time, vf, vs, vs),
                    (vs * s.slow_time, vs, vs, vs if k < s.cycles - 1 else 0.)]
        y, t, mk, ups = s.test_y, 0., [], []
        for i, (d, vc, v0, v1) in enumerate(seq):
            y += d
            r._g("G1 Y%.4f E%.5f F%.2f" % (y, d * ln['ratio'], vc * 60.))
            ta, td = (vc - v0) / a, (vc - v1) / a
            tc = (d - (vc * vc - v0 * v0) / (2 * a) - (vc * vc - v1 * v1) / (2 * a)) / vc
            if i % 2:                              # fast
                ups.append((t + ta / 2, t + ta + tc + td / 2))
            t += ta + tc + td
        mk.append({'name': 'slow', 'sweep': -1, 't0': 0., 't1': ups[0][0]})
        for k, (u, dn) in enumerate(ups):
            nxt = ups[k + 1][0] if k + 1 < len(ups) else t
            mk += [{'name': 'fast', 'sweep': k, 't0': u, 't1': dn},
                   {'name': 'slow', 'sweep': k, 't0': dn, 't1': nxt}]
        return mk, False
    r._train_klipper = train
    r._line_start = lambda s, ln: r._g("G1 X%.3f Y%.3f F6000" % (s.test_x, s.test_y))
    r._motion_check = lambda *a: None


def _fixed_values(meth, pas, straight=False, **kw):
    """One run measuring exactly `pas` (no search) -> result"""
    p, bus, store, r = make(tau=0.045, noise=0.)
    if straight:
        _straight_klipper(r)
    def search(ctx, gcmd):
        for k in pas: r._measure(ctx, k)
    r._search = search
    rid = r.run(Gcmd(), S(method=meth, test_y=20, **kw))
    return store.get_run(rid)['analysis']['result']

def test_push_equals_klipper_pa():
    """The push method gives what Klipper's own PA gives on a straight line
    with the same filament per mm, at every accel and smooth time: no
    conversion"""
    pas = [0., 0.035, 0.045, 0.055, 0.065, 0.08, 0.1]
    seen = []
    for kw in (dict(), dict(test_accel=1500), dict(smooth_time=0.025),
               dict(test_accel=800, smooth_time=0.06)):
        a = _fixed_values('push', pas, **kw)
        b = _fixed_values('klipper', pas, straight=True, **kw)
        assert abs(a['area']['k'] - b['area']['k']) < 0.0002, (kw, a['area']['k'], b['area']['k'])
        assert abs(a['edge_result']['pa'] - b['edge_result']['pa']) < 0.0006, kw
        assert abs(a['edge_result']['pa'] - 0.045) < 0.001, kw   # edge: the lag
        seen.append(a['area']['k'])
    assert max(seen) - min(seen) > 0.002          # area moves with smoothing

def test_push_train_moves():
    """Klipper-exact push: E total = the planned filament, legs FAST_TIME /
    SLOW_TIME long, pieces through the ramps carry K * a * dt (negative =
    pulled back at high PA), every move inside max_extrude_only_distance,
    limits raised for the train only"""
    p, bus, store, r = make()
    A = Extruder.filament_area
    s = S(cycles=2)
    ext = p.objs['toolhead'].ext
    r.pp = r.push_plan(s, A, ext)
    r.e_lim = (ext.max_e_velocity, ext.max_e_accel)
    r.orig_lim = {'max_accel': 4000., 'minimum_cruise_ratio': 0.5}
    for pa in (0.0, 0.15):
        n0 = len(p.log)
        r._e_limits(ext, True); r._push_accel(True)
        mk, partial = r._train_push(pa, s, r.pp, p.pt)
        r._push_accel(False); r._e_limits(ext, False)
        es = [float(re.search(r'E([-\d.]+)', l).group(1)) for l in p.log[n0:] if l.startswith('G1 E')]
        assert abs(sum(es) - s.per_pa_mm(A)) < 0.003, (sum(es), s.per_pa_mm(A))   # ramps
        assert max(abs(e) for e in es) <= 0.9 * ext.max_e_dist + 1e-9
        assert (min(es) < 0) == (pa > 0.1), min(es)   # fall ramp pulls back
        f = [x for x in mk if x['name'] == 'fast']
        sl = [x for x in mk if x['name'] == 'slow']
        assert all(abs(x['t1'] - x['t0'] - 0.6) < 2e-4 for x in f), f
        assert abs(sl[1]['t1'] - sl[1]['t0'] - 1.0) < 2e-4, sl
        ramps = [l for l in p.log[n0:] if l.startswith('G1 E')]
        assert len(ramps) > 4 * 2 * 2, len(ramps)    # pieces through every ramp
    assert (ext.max_e_velocity, ext.max_e_accel) == (100.0, 5000.0)
    # without lifting max_accel / minimum_cruise_ratio Klipper would stretch
    # every piece (the cap above): the legs would come out too long
    t0 = p.pt; r._e_limits(ext, True)
    mk2, _ = r._train_push(0.15, s, r.pp, p.pt)
    r._e_limits(ext, False)
    f2 = [x for x in mk2 if x['name'] == 'fast'][0]
    assert f2['t1'] - f2['t0'] > 0.61, f2
    # one ramp, piece by piece: x(t) + K * v(t), cruise speed picked so the
    # piece (from and to rest at the raised accel) lasts exactly its time
    vs, vf, ea = r.pp['v_slow'], r.pp['v_fast'], r.pp['e_accel']
    dur = (vf - vs) / ea
    n = max(2, int(math.ceil(dur / 0.001)))
    lines = [l for l in p.log[n0:] if l.startswith('G1 E')]
    lead = 2 + max(1, int(math.ceil(vs * (s.slow_time - dur / 2) / 45.)))
    up = lines[lead:lead + n]
    dt = dur / n
    for i, l in enumerate(up):
        e = float(re.search(r'E([-\d.]+)', l).group(1))
        want = vs * dt + 0.5 * ea * ((i + 1) ** 2 - i ** 2) * dt * dt + 0.15 * ea * dt
        assert abs(e - want) < 2e-5, (i, e, want)
        v = float(re.search(r'F([-\d.]+)', l).group(1)) / 60.
        assert abs(abs(e) / v + v / 1e7 - dt) < 1e-7

def test_area_pulse_first_order():
    """A plain lag (no PA): the force lags tau behind every speed change, so
    each edge leaves a gap of tau (in ms of step)"""
    tau = 0.03
    t, f, m = _synth_b2b(tau)
    ar = analysis.pulse_area(t, f, m, True, 0.15)
    dt = 1 / 320.                    # _synth_b2b: Euler lag, one sample ahead
    te = -dt / math.log(1 - dt / tau)
    want = -1000 * (te * (1 - math.exp(-0.15 / te)) - dt)
    assert ar['n'] == 5 and abs(ar['rise'] - want) < 0.6 and abs(ar['fall'] - want) < 0.6, (ar['rise'], ar['fall'], want)
    assert abs(ar['balance'] - (ar['rise'] + ar['fall'])) < 0.01
    assert ar['up'][-1] > 0.99 and ar['down'][-1] > 0.99      # 0 -> 1 shape
    # ring: overshoot after both changes makes both areas positive
    t, f, m = _synth_b2b(tau, ring=1.5)
    ar2 = analysis.pulse_area(t, f, m, True, 0.15)
    assert ar2['rise'] > 0 and ar2['fall'] > 0, ar2
    # the window: shorter window, smaller gap
    ar3 = analysis.pulse_area(*_synth_b2b(0.06), True, 0.06)
    assert ar3['win'] == 0.06 and ar3['rise'] > -60 * (1 - math.exp(-1)) - 0.8

def _area_segs(rows, noise=0.):
    """rows (pa, mean balance) -> segments with 5 pulses each"""
    import random
    rng = random.Random(4)
    out = []
    for i, (pa, b) in enumerate(rows):
        pul = [[0.5 * b + rng.gauss(0, noise), 0.5 * b + rng.gauss(0, noise)] for _ in range(5)]
        out.append({'idx': i, 'pa': pa, 'partial': False,
                    'piro': {'area': {'pulses': pul}, 'dip': 0.0, 'dip_se': 0.01,
                             'limit': 0.03, 'n': 5, 'snr': 60.0, 'spread': 1.0,
                             'drift': 0.0, 'pulse_dips': [0.0] * 5,
                             'over': None, 'over_se': None}})
    return out

def test_piro_area_rule():
    line = lambda pa: 1100. * (pa - 0.062)
    order = (0.0, 0.1, 0.066, 0.058, 0.07, 0.054)
    ar = analysis.piro_area(_area_segs([(pa, line(pa)) for pa in order], noise=0.3))
    assert abs(ar['k'] - 0.062) < 0.0005 and ar['r2'] > 0.999, ar
    assert ar['used'][0] == 0.054 and 0.0 not in ar['used']      # PA 0 left out
    assert ar['above'] == 2 and ar['below'] == 2 and not ar['warnings'], ar
    assert ar['se'] >= 0.0003 and abs(ar['se'] - max(ar['fit'], ar['bootstrap'], 0.0003)) < 1e-5
    nar = ar['narrow']
    assert [x['n'] for x in nar] == [2, 3, 4, 5, 6] and [x['pa'] for x in nar] == list(order[1:])
    assert nar[0]['k'] is not None and abs(nar[-1]['k'] - ar['k']) < 1e-4
    res = analysis.run_result(_area_segs([(pa, line(pa)) for pa in order], 0.3), 'area')
    assert res['method'] == 'PIRO-AREA' and res['grade'] == 'good' and res['pa'] == round(ar['k'], 4)
    # PA 0 is used while there are fewer than 3 others
    ar = analysis.piro_area(_area_segs([(pa, line(pa)) for pa in (0.0, 0.1, 0.066)]))
    assert ar['used'] == [0.0, 0.066, 0.1] and abs(ar['k'] - 0.062) < 1e-4
    assert ar['warnings'] == ['Fewer than 2 values within 0.015 on each side of the result'], ar['warnings']
    # no result: two values only, a flat / falling line, a crossing far outside
    assert analysis.piro_area(_area_segs([(0.0, -40), (0.1, 30)]))['k'] is None
    assert analysis.piro_area(_area_segs([(0.05, 5), (0.06, 4), (0.07, 3)]))['k'] is None
    far = analysis.piro_area(_area_segs([(0.01, -50), (0.02, -40), (0.03, -30)]))
    assert far['k'] is None and 'outside' in far['why'], far
    res = analysis.run_result(_area_segs([(0.01, -50), (0.02, -40), (0.03, -30)]), 'area')
    assert res['pa'] is None and res['why'] and res['method'] == 'PIRO-AREA'
    # bent line: a warning, the grade drops
    bent = [(pa, line(pa) * (4 if pa < 0.062 else 1))
            for pa in (0.05, 0.055, 0.058, 0.066, 0.07, 0.1)]
    ar = analysis.piro_area(_area_segs(bent))
    assert any('not straight' in w for w in ar['warnings']), ar
    # the two edges on their own disagreeing: a warning
    segs = _area_segs([(pa, line(pa)) for pa in order])
    for sg in segs:
        sg['piro']['area']['pulses'] = [[r + 300 * (sg['pa'] - 0.062) + 3, d - 300 * (sg['pa'] - 0.062) - 3]
                                        for r, d in sg['piro']['area']['pulses']]
    assert any('disagree' in w for w in analysis.piro_area(segs)['warnings'])

def test_area_leaves_out_aiming_values():
    """At high flow the balance bends far from the answer: the result uses
    only the values beside it, not PA 0 / PA_MAX (Runs 61, 62)"""
    rows = [(0.0, -93.7, 'reference'), (0.1, 56.0, 'upper'), (0.0655, 24.9, 'area'),
            (0.055, 0.5, 'area'), (0.047, -16.7, 'area'), (0.0515, -3.8, 'area')]
    segs = _area_segs([(pa, b) for pa, b, _ in rows], noise=0.5)
    for sg, (_, _, st) in zip(segs, rows): sg['stage'] = st
    ar = analysis.piro_area(segs)
    assert ar['used'] == [0.047, 0.0515, 0.055, 0.0655], ar['used']
    assert abs(ar['k'] - 0.0542) < 0.0005 and not ar['warnings'], ar
    plain = analysis.piro_area([{k: v for k, v in sg.items() if k != 'stage'} for sg in segs])
    assert 0.1 in plain['used'] and plain['r2'] < ar['r2']   # older runs: as before
    nar = ar['narrow']
    assert [x['early'] for x in nar] == [True, True, True, False, False], nar
    # an aiming value close to the answer stays in the line
    rows2 = [(0.0, -90, 'reference'), (0.1, 6, 'upper'), (0.09, -6, 'area'),
             (0.085, -12, 'area'), (0.105, 12, 'area')]
    segs2 = _area_segs([(pa, b) for pa, b, _ in rows2])
    for sg, (_, _, st) in zip(segs2, rows2): sg['stage'] = st
    assert 0.1 in analysis.piro_area(segs2)['used']

def test_empty_nozzle_prime_steady():
    """Run 64: a nearly empty nozzle. Area mode measures PA 0 once and warns
    when its pressure is still rising; PRIME_STEADY fills the nozzle first."""
    p, bus, store, r = make(tau=0.045, fill=110.)
    m = store.get_run(r.run(Gcmd(), S(cycles=6, max_filament=1000)))
    st = [x['stage'] for x in m['search']['steps']]
    assert st.count('reference') == 1, st                     # area: PA 0 once
    assert any('Pressure still rising at PA 0' in w
               for w in m['analysis']['result']['warnings']), m['analysis']['result']['warnings']
    assert 'prime' not in m                                   # prime_steady 0 here
    p, bus, store, r = make(tau=0.045, fill=110.)             # edge keeps its repeats
    m = store.get_run(r.run(Gcmd(), S(cycles=6, max_filament=1000, mode='edge')))
    assert [x['stage'] for x in m['search']['steps']].count('reference') >= 2
    p, bus, store, r = make(tau=0.045, fill=70.)
    g = Gcmd()
    m = store.get_run(r.run(g, S(cycles=6, max_filament=1000, prime_steady=1)))
    st = [x['stage'] for x in m['search']['steps']]
    assert st.count('reference') == 1, st
    pr = m['prime']
    assert pr['steady'] and 60 <= pr['mm'] <= 100, pr
    assert any('pressure steady' in x for x in g.out), g.out
    assert abs(m['analysis']['result']['pa'] - 0.049) < 0.004, m['analysis']['result']['pa']
    # a full nozzle: only a few chunks after the 20 mm
    p, bus, store, r = make(tau=0.045)
    pr = store.get_run(r.run(Gcmd(), S(cycles=6, max_filament=1000, prime_steady=1)))['prime']
    assert pr['steady'] and pr['mm'] <= 45, pr
    # never steady: stops at PRIME_MAX and says so
    p, bus, store, r = make(tau=0.045, fill=400.)
    g = Gcmd()
    m = store.get_run(r.run(g, S(cycles=6, max_filament=1000, prime_steady=1, prime_max=50)))
    assert not m['prime']['steady'] and m['prime']['mm'] <= 50.01, m['prime']
    assert any('PRIME_MAX reached' in x for x in g.out), g.out
    for bad in (dict(prime_steady=2), dict(prime_steady=1, prime_length=30, prime_max=20)):
        try: S(**bad); assert False, bad
        except PAError: pass
    assert S(prime_steady=1).filament_mm(Extruder.filament_area) > S().filament_mm(Extruder.filament_area)

def test_area_placement_and_single_reference():
    """C: values beside the answer about 0.004 and 0.010 from it, never
    closer than 0.003 to each other; PA 0 measured once in area mode"""
    from pa_loadcell.testrun import AREA_GAP
    p, bus, store, r = make(tau=0.045)
    m = store.get_run(r.run(Gcmd(), S(cycles=4)))
    pas = sorted(x['pa'] for x in m['segments'])
    st = [x['stage'] for x in m['search']['steps']]
    assert st.count('reference') == 1 and len(pas) <= 7, st
    assert all(b - a >= AREA_GAP - 1e-9 for a, b in zip(pas, pas[1:])), pas
    ar = m['analysis']['result']['area']
    near = [x for x in ar['used'] if abs(x - ar['k']) <= 0.015]
    assert max(near) - min(near) >= 0.012, near          # wide spread
    assert ar['above'] >= 2 and ar['below'] >= 2, ar

def test_area_tolerance_fix_a():
    """The +- comes from the line and the pulse noise, not from leaving
    values out: a noisy pair (Run 73) no longer inflates it or fails the
    straightness check; a real bend with calm pulses still does"""
    rows = [(0.0, 'reference', [-118, -116, -113, -117, -109]),
            (0.1, 'upper', [85, 71, 95, 61, 82]),
            (0.0575, 'area', [-9, -17, -7, -24, -5]), (0.062, 'area', [6, -7, 1, -8, -1]),
            (0.066, 'area', [25, -4, 10, 5, 10]), (0.0685, 'area', [8, 0, 10, 1, 6])]
    segs = [{'idx': i, 'pa': pa, 'stage': st, 'partial': False,
             'piro': {'area': {'pulses': [[v / 2, v / 2] for v in pul]}}}
            for i, (pa, st, pul) in enumerate(rows)]
    ar = analysis.piro_area(segs)
    assert abs(ar['k'] - 0.0635) < 0.0003 and ar['se'] < 0.0016, ar
    assert not any('straight' in w for w in ar['warnings']), ar['warnings']
    assert ar['influence'] is not None and ar['fit'] > 0
    # calm pulses on a bent curve: R² low and far beyond the pulse noise
    bent = [(0.0, 'reference', -100), (0.1, 'upper', 60), (0.05, 'area', -40),
            (0.055, 'area', -5), (0.06, 'area', 0), (0.065, 'area', 2), (0.07, 'area', 40)]
    segs = [{'idx': i, 'pa': pa, 'stage': st, 'partial': False,
             'piro': {'area': {'pulses': [[v / 2 + d, v / 2 - d] for d in (-.2, 0, .2, .1, -.1)]}}}
            for i, (pa, st, v) in enumerate(bent)]
    assert any('not straight' in w for w in analysis.piro_area(segs)['warnings'])
    # three values close together and one far out that bends the line:
    # leaving that one out moves the result by more than 2 x +-
    odd = [(0.0, 'reference', -100), (0.1, 'upper', 80), (0.056, 'area', -14),
           (0.059, 'area', -8), (0.062, 'area', -2), (0.074, 'area', 6)]
    segs = [{'idx': i, 'pa': pa, 'stage': st, 'partial': False,
             'piro': {'area': {'pulses': [[v / 2 + d, v / 2 - d] for d in (-.2, 0, .2, .1, -.1)]}}}
            for i, (pa, st, v) in enumerate(odd)]
    assert any('One value moves the result' in w for w in analysis.piro_area(segs)['warnings'])

def test_area_search_stops_at_max_values():
    p, bus, store, r = make(tau=0.045)
    m = store.get_run(r.run(Gcmd(), S(max_values=4)))
    assert len(m['segments']) == 4, [x['pa'] for x in m['segments']]
    res = m['analysis']['result']
    assert res['pa'] is not None and res['warnings'], res      # not 2 per side
    assert res['grade'] in ('fair', 'poor')
    for bad in (dict(max_values=2), dict(max_values=15), dict(area_window=0.01)):
        try: S(**bad); assert False, bad
        except PAError: pass

def test_area_upper_limit_fails():
    # a very slow melt: still lagging at PA_LIMIT
    p, bus, store, r = make(tau=0.3)
    try: r.run(Gcmd(), S(prime_length=0, pa_limit=0.14)); assert False
    except PAError as e: assert 'Area below 0 up to PA 0.140' in str(e), e
    assert [x['pa'] for x in store.get_run(1)['segments']] == [0.0, 0.1, 0.12, 0.14]

def test_mode_setting_and_reanalyse():
    p, bus, store, r = make(tau=0.045)
    rid = r.run(Gcmd(), S(method='klipper', mode='edge'))
    res = store.get_run(rid)['analysis']['result']
    assert res['method'] == 'PIRO-EDGE' and res['area']['k'] is not None
    assert not any(x['stage'] == 'area' for x in store.get_run(rid)['search']['steps'])
    m = store.reanalyze(rid, mode='area')
    assert m['analysis']['result']['method'] == 'PIRO-AREA' and m['settings']['mode'] == 'area'
    assert store.reanalyze(rid, mode='fit')['settings']['mode'] == 'area'     # older name
    # a run made before the mode setting (no mode stored) keeps its edge result
    meta = store.get_run(rid)
    meta['settings'].pop('mode')
    import json, os
    json.dump(meta, open(os.path.join(store.root, 'run_%04d' % rid, 'meta.json'), 'w'))
    assert store.reanalyze(rid)['analysis']['result']['method'] == 'PIRO-EDGE'
    d = Settings()
    assert (d.mode, d.method, d.max_values, d.area_window) == ('fall', 'push', 7, 0.15)
    class G:                                    # older parameter names still work
        def __init__(s, p): s.p = p
        def get(s, k, d=None): return s.p.get(k, d)
        def get_float(s, k, d=None): return float(s.p[k]) if k in s.p else d
        def get_int(s, k, d=None): return int(s.p[k]) if k in s.p else d
    base = Settings(test_x=1, test_y=1, park_x=1, park_y=1)
    m2 = base.merged(G({'PA_METHOD': 'klipper', 'PICK': 'edge'}))
    assert m2.method == 'klipper' and m2.mode == 'edge'
    m3 = base.merged(G({'PA_METHOD': 'klipper', 'METHOD': 'push', 'MODE': 'FIT'}))
    assert m3.method == 'push' and m3.mode == 'area'
    try: S(mode='best'); assert False
    except PAError: pass
    # the two rules stay separate: no area data -> no PIRO-AREA result, edge untouched
    from pa_loadcell.analysis import run_result
    segs = [dict(e, piro={k: v for k, v in e['piro'].items() if k != 'area'})
            for e in store.get_run(rid)['segments']]
    res = run_result(segs, 'area')
    assert res['method'] == 'PIRO-AREA' and res['pa'] is None and res['why'], res['why']
    assert res['edge_result']['pa'] is not None
    assert run_result(segs, 'edge')['pa'] == res['edge_result']['pa']

def test_upper_limit_steps_up_then_fails():
    # no melt lag: the force never dips, so the search climbs by 0.02 and
    # gives up after PA_LIMIT
    p, bus, store, r = make()
    try: r.run(Gcmd(), S(prime_length=0, mode='edge')); assert False
    except PAError as e: assert 'No dip up to PA 0.200' in str(e), e
    m = store.get_run(1)
    assert m['status'] == 'error'
    assert [x['pa'] for x in m['segments']] == [0.0, 0.1, 0.12, 0.14, 0.16,
                                                  0.18, 0.2]
    assert [x['stage'] for x in m['search']['steps']][1:] == ['upper'] * 6

def test_upper_limit_raised_until_dip():
    p, bus, store, r = make(tau=0.13)
    rid = r.run(Gcmd(), S(prime_length=0, mode='edge'))
    m = store.get_run(rid)
    up = [x['pa'] for x in m['search']['steps'] if x['stage'] == 'upper']
    assert up[0] == 0.1 and len(up) >= 2 and up[-1] <= 0.2, up
    assert abs(m['analysis']['result']['pa'] - 0.13) < 0.012, m['analysis']['result']

def test_safety():
    p, bus, store, r = make()
    try: r.run(Gcmd(), S(max_filament=100)); assert False
    except PAError as e: assert 'MAX_FILAMENT' in str(e), e
    for kw, msg in ((dict(temp=150.0), 'min_temp'), (dict(z=5.0), 'Z must')):
        p, bus, store, r = make(**kw)
        try: r.run(Gcmd(), S()); assert False
        except PAError as e: assert msg in str(e), e
    p, bus, store, r = make(); p.objs.pop('load_cell')
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'load cell' in str(e).lower()
    # push needs no max_extrude_cross_section ...
    p, bus, store, r = make(tau=0.045); p.objs['toolhead'].ext.max_extrude_ratio = 0.3
    assert store.get_run(r.run(Gcmd(), S()))['status'] == 'complete'
    # ... but a speed change must be short next to the legs
    p, bus, store, r = make(tau=0.045)
    try: r.run(Gcmd(), S(test_accel=50)); assert False
    except PAError as e: assert 'TEST_ACCEL 50' in str(e) and 'too long' in str(e), e
    for bad in (dict(pa_max=0.3), dict(cycles=1), dict(fast_time=0.1),
                dict(slow_flow=20)):
        try: S(**bad); assert False, bad
        except PAError: pass

def test_settings_amounts():
    s = Settings()
    area = 3.14159265 * 0.875 ** 2
    fast, slow = s.leg_mm(area)
    assert abs(fast - 12 / area * 0.6) < 1e-9 and abs(slow - 1.8 / area) < 1e-9
    assert (s.cycles, s.prime_steady, s.prime_max) == (10, 1, 100.)    # defaults
    assert abs(s.per_pa_mm(area) - (slow + 10 * (fast + slow))) < 1e-9
    # area: at most MAX_VALUES (PA 0 measured once) + the longest prime
    assert abs(s.filament_mm(area) - (s.per_pa_mm(area) * 7 + 100)) < 1e-9
    e = Settings(mode='edge')                          # edge: 14 measurements
    assert abs(e.filament_mm(area) - (e.per_pa_mm(area) * SEARCH_MAX_TRAINS + 100)) < 1e-9
    assert abs(s.per_pa_s() - (0.3 + 1 + 10 * 1.6)) < 1e-9
    assert s.filament_mm(area) < s.max_filament        # defaults fit the cap

def test_prime_limits():
    p, bus, store, r = make()
    p.objs['toolhead'].ext.max_e_velocity = 1.0
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'FLOW' in str(e), e
    p, bus, store, r = make(tau=0.045)
    r.run(Gcmd(), S(prime_length=0))
    i = p.log.index('SET_PRESSURE_ADVANCE ADVANCE=0.000001')
    assert not p.log[i + 1].startswith('G1 E20')

def test_live_leg_schedule():
    p, bus, store, r = make(tau=0.045)
    r.run(Gcmd(), S())
    legs = r.legs_since(0)
    names = [x[4] for x in legs]
    assert names[0] == 'prime' and 'fast' in names and 'slow' in names
    assert all(b > a for _, a, b, _, _ in legs)
    assert r.legs_since(legs[-1][0]) == []
    fl = {x[4]: x[3] for x in legs}
    assert fl['fast'] == 12 and fl['slow'] == 1.8 and fl['prime'] == 12

def test_export_import_reanalyze():
    p, bus, store, r = make(tau=0.045)
    rid = r.run(Gcmd(), S())
    n = len(store.get_run(rid)['segments'])
    data = store.export_run(rid)
    assert data['format'] == 'pa_loadcell_run' and len(data['segments']) == n
    import json as _j
    data = _j.loads(_j.dumps(data))                     # through a file
    new = store.import_run(data)
    m = store.get_run(new)
    assert new != rid and m['imported']['from_id'] == rid
    assert m['analysis']['result']['pa'] == store.get_run(rid)['analysis']['result']['pa']
    m2 = store.reanalyze(new, skip_first=False)
    assert m2['segments'][1]['piro']['n'] == 3          # every pulse used
    assert store.get_segment(new, 1)['t'] == store.get_segment(rid, 1)['t']
    for bad in ({'format': 'x'}, {'format': 'pa_loadcell_run', 'meta': {},
                'segments': [{'t': [1], 'f': []}]}):
        try: store.import_run(bad); assert False
        except PAError: pass
    assert len(store.list_runs()) == 2                 # failed imports left nothing
    assert store.list_runs()[0]['result']['pa'] is not None

def test_old_run_is_reanalysed():
    import json as _j
    st = RunStore(tempfile.mkdtemp())
    rid = st.new_run('test', {'settings': {'fast_flow': 4.0, 'slow_flow': 1.0}})
    st.add_segment(rid, dict(zip(('t', 'f', 'markers'), _synth_b2b(0.05)), pa=0.0))
    st.finalize(rid, 'complete')
    path = st._meta_path(rid); m = _j.load(open(path))
    for e in m['segments']: e.pop('piro')
    m['analysis'] = {'model': 'fall_undershoot', 'picks': {}}
    _j.dump(m, open(path, 'w'))                       # like a run from v2
    m = st.get_run(rid)
    assert 'piro' in m['segments'][0] and 'picks' not in m['analysis']
    assert 'result' in m['analysis'] and m['analysed']

def test_edge_score_on_synthetic():
    t, f, m = _synth_b2b(0.05)
    sc = analysis.edge_score(t, f, m, 0.04)
    assert sc and sc['n'] == 5 and sc['dip'] > -0.03, sc       # lag: no dip
    assert sc['over'] < 0.03 and abs(sc['drift']) < 0.02, sc
    t, f, m = _synth_b2b(0.05, ring=1.6)
    sc2 = analysis.edge_score(t, f, m, 0.04)
    assert sc2['dip'] < -0.1 and sc2['over'] > 0.1, sc2        # too much: both edges
    assert len(sc2['fall_curve']) > 20 and sc2['dt'] > 0

def _pts(rows, over=None):
    out = []
    for i, (pa, dip) in enumerate(rows):
        sc = {'dip': dip, 'dip_se': 0.01, 'limit': 0.03, 'n': 5, 'snr': 60.0,
              'spread': 1.05, 'drift': 0.0, 'pulse_dips': [dip] * 5,
              'over': over(pa) if over else None, 'over_se': 0.01 if over else None}
        out.append({'idx': i, 'pa': pa, 'partial': False, 'piro': sc})
    return out

def test_piro_pick_rule():
    # dip falls 10 per 1.0 PA beyond 0.05; clean before
    line = lambda pa: 0.02 if pa <= 0.05 else 0.02 - 10 * (pa - 0.05) - 0.02
    segs = _pts([(pa, line(pa)) for pa in (0.0, 0.03, 0.045, 0.055, 0.06, 0.07, 0.1)])
    r = analysis.piro_pick(segs)
    assert abs(r['pa'] - 0.05) < 0.003 and r['grade'], r
    assert r['fall']['k_ok'] == 0.045 and r['fall']['k_bad'] == 0.055, r['fall']
    assert r['check'] == 'none'
    # one blip that does not persist is ignored
    segs = _pts([(0.0, 0.02), (0.02, -0.05), (0.03, 0.01), (0.05, 0.0),
                 (0.06, -0.1), (0.07, -0.2)])
    assert 0.05 <= analysis.piro_pick(segs)['pa'] <= 0.06
    # never dips: no answer, says the answer is higher
    r = analysis.piro_pick(_pts([(0.0, 0.02), (0.05, 0.01), (0.1, 0.0)]))
    assert r['pa'] is None and 'No dip up to' in r['why'], r
    # a dip at PA 0 raises the floor (OznLab rule): 0.06 at -4 % is not a dip
    segs = _pts([(0.0, -0.03), (0.04, 0.0), (0.06, -0.04), (0.08, -0.2), (0.1, -0.4)])
    assert analysis.piro_pick(segs)['fall']['k_bad'] == 0.08
    # repeats pool: two measurements of 0.055
    segs = _pts([(0.0, 0.02), (0.045, 0.01), (0.055, -0.04), (0.055, -0.06),
                 (0.065, -0.15)])
    pts = analysis.pool([dict(s, idx=i) for i, s in enumerate(segs)])
    p = [x for x in pts if x['pa'] == 0.055][0]
    assert p['times'] == 2 and abs(p['dip'] + 0.05) < 1e-9 and p['n'] == 10
    # speed-up edge overshooting far below the slow-down answer: warning
    segs = _pts([(pa, line(pa)) for pa in (0.0, 0.03, 0.045, 0.055, 0.06, 0.07)],
                over=lambda pa: 0.0 if pa < 0.03 else 0.3)
    r = analysis.piro_pick(segs)
    assert r['check'] == 'lower' and r['warnings'], r

def test_predict_from_one_dip():
    pts = analysis.pool(_pts([(0.0, 0.02), (0.1, -0.5)]))
    assert abs(analysis.predict(pts) - 0.05) < 1e-9
    pts = analysis.pool(_pts([(0.0, 0.02), (0.08, -0.15), (0.1, -0.35)]))
    assert abs(analysis.predict(pts) - 0.065) < 1e-9      # slope from two dips
    assert analysis.predict(analysis.pool(_pts([(0.0, 0.0), (0.1, 0.01)]))) is None

if __name__ == '__main__':
    import time
    for n, fn in list(globals().items()):
        if n.startswith('test_'):
            t0 = time.time(); fn(); print('ok %-40s %.1fs' % (n, time.time() - t0))
