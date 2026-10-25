"""Offline tests with a fake printer: no Klipper needed.
Run: python3 tests/test_backend.py"""
import os, re, sys, tempfile
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
        self.windows = []          # (t0, t1) of E moves -> force
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

class Extruder:
    max_e_velocity, max_e_accel = 100.0, 5000.0
    filament_area = 3.14159265 * 0.875 ** 2
    max_extrude_ratio = 120.0 / filament_area     # max_extrude_cross_section 120
    def __init__(s, p): s.p = p
    def get_status(s, now):
        return {'temperature': s.p.temp, 'pressure_advance': 0.05}

class Toolhead:
    def __init__(s, p): s.p, s.ext = p, Extruder(p)
    def get_last_move_time(s): return s.p.pt
    def dwell(s, d): s.p.pt += d
    def wait_moves(s): pass
    def get_position(s): return [0, 0, s.p.z, 0]
    def get_extruder(s): return s.ext
    def get_status(s, now):
        return {'homed_axes': 'xyz', 'axis_minimum': [0, 0, 0],
                'axis_maximum': [250, 250, 250], 'max_accel': 4000,
                'max_accel_to_decel': 2000}

class Gcode:
    """Times E moves like Klipper: an XY+E move runs at F over the XY
    distance, an extrude-only move at F over the E distance."""
    def __init__(s, p): s.p, s.n_e, s.pos = p, 0, {'X': 0.0, 'Y': 0.0}
    def run_script_from_command(s, script):
        for line in script.split('\n'):
            s.p.log.append(line)
            w = dict((k, float(v)) for k, v in re.findall(r'([XYEF])([-\d.]+)', line)) if line.startswith('G1') else {}
            if 'E' in w and 'F' in w:
                xy = [abs(w[a] - s.pos[a]) for a in 'XY' if a in w]
                d = max(xy) if xy and max(xy) > 0 else abs(w['E'])
                dur = d / (w['F'] / 60)
                s.p.windows.append((s.p.pt, s.p.pt + dur, w['E'] / dur))
                s.p.pt += dur
                s.n_e += 1
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
        n1 = int((p.pt + 0.3 - 1000) * 320)  # stream runs past the last move
        if n1 <= n0: return
        data = []
        for i in range(n0, n1):
            t = 1000 + i / 320.0
            v = sum((w[2] if len(w) > 2 else 10.0)
                    for w in p.windows if w[0] <= t < w[1])
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
        return e / (t1 - t0)
    def pump(s):
        p = s.p
        if not p.client: return
        n0, n1 = int((p.sent - 1000) * 320), int((p.pt + 0.3 - 1000) * 320)
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

class Gcmd:
    def __init__(s): s.out = []
    def respond_info(s, m): s.out.append(m)


from pa_loadcell import analysis


def make(temp=210.0, z=20.0, tau=None):
    p = Printer(temp, z)
    if tau: p.objs['load_cell'] = LagCell(p, tau)
    bus = SampleBus(p, 'load_cell', 60000, 15.0)
    store = RunStore(tempfile.mkdtemp())
    lim = {'min_temp': 200, 'required_z': 20, 'z_tol': 0.5, 'timeout': 3}
    return p, bus, store, PATestRunner(p, bus, store, lim)

def S(**kw):
    d = dict(cycles=3, test_x=100, test_y=100, park_x=10, park_y=10)
    d.update(kw); s = Settings(**d); s.validate(); return s


def test_cancel_saves_partial():
    p, bus, store, r = make()
    g = p.objs['gcode']; orig = g.run_script_from_command
    def hook(script):
        orig(script)
        if g.n_e == 12: r.cancel()
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
    p, bus, store, r = make(tau=0.045)
    g = Gcmd()
    rid = r.run(g, S())
    m = store.get_run(rid)
    assert m['status'] == 'complete', m
    res = m['analysis']['result']
    assert res['pa'] is not None and abs(res['pa'] - 0.045) < 0.005, res
    assert res['grade'] in ('good', 'fair', 'poor') and res['unc'] > 0
    pas = [x['pa'] for x in m['segments']]
    assert pas[:2] == [0.0, 0.1], pas                      # reference, upper
    assert 4 <= len(pas) <= SEARCH_MAX_TRAINS, pas
    st = [x['stage'] for x in m['search']['steps']]
    assert st[0] == 'reference' and st[1] == 'upper' and 'bracket' in st, st
    assert m['search']['result'] == res['pa']
    assert 0.03 < m['search']['prediction'] < 0.07, m['search']
    # Klipper PA held at 0, restored after; no XY moves during the pulses
    assert [l for l in p.log if l.startswith('SET_PRESSURE_ADVANCE')] == [
        'SET_PRESSURE_ADVANCE ADVANCE=0.00000',
        'SET_PRESSURE_ADVANCE ADVANCE=0.05000']
    xy = [l for l in p.log if re.match(r'G1 [XY]', l)]
    assert xy == ['G1 X100.00 Y100.00 F6000', 'G1 X10.00 Y10.00 F6000'], xy
    assert not any('SET_VELOCITY_LIMIT' in l for l in p.log)
    # prime: extrude-only moves totalling 20 mm before the first pulses
    i_pa = p.log.index('SET_PRESSURE_ADVANCE ADVANCE=0.00000')
    assert p.log[i_pa + 1].startswith('G1 E20.00000 F'), p.log[i_pa + 1]
    seg = store.get_segment(rid, 1)
    names = [k['name'] for k in seg['markers']]
    assert names == ['slow'] + ['fast', 'slow'] * 3, names
    f = [k for k in seg['markers'] if k['name'] == 'fast'][0]
    assert abs(f['t1'] - f['t0'] - 0.6) < 0.002, f        # nominal step times
    e = m['segments'][1]
    assert e['piro']['n'] == 2 and e['cmd']['gain'] > 0   # first pulse skipped
    assert 'PIRO-EDGE PA' in g.out[-1], g.out[-1]

def test_upper_limit_steps_up_then_fails():
    # no melt lag: the force never dips, so the search climbs by 0.02 and
    # gives up after PA_LIMIT
    p, bus, store, r = make()
    try: r.run(Gcmd(), S(prime_length=0)); assert False
    except PAError as e: assert 'no dip up to PA 0.200' in str(e), e
    m = store.get_run(1)
    assert m['status'] == 'error'
    assert [x['pa'] for x in m['segments']] == [0.0, 0.1, 0.12, 0.14, 0.16,
                                                  0.18, 0.2]
    assert [x['stage'] for x in m['search']['steps']][1:] == ['upper'] * 6

def test_upper_limit_raised_until_dip():
    p, bus, store, r = make(tau=0.13)
    rid = r.run(Gcmd(), S(prime_length=0))
    m = store.get_run(rid)
    up = [x['pa'] for x in m['search']['steps'] if x['stage'] == 'upper']
    assert up[0] == 0.1 and len(up) >= 2 and up[-1] <= 0.2, up
    assert abs(m['analysis']['result']['pa'] - 0.13) < 0.012, m['analysis']['result']

def test_safety():
    p, bus, store, r = make()
    try: r.run(Gcmd(), S(max_filament=100)); assert False
    except PAError as e: assert 'MAX_FILAMENT' in str(e), e
    for kw, msg in ((dict(temp=150.0), 'nozzle'), (dict(z=5.0), 'Z must')):
        p, bus, store, r = make(**kw)
        try: r.run(Gcmd(), S()); assert False
        except PAError as e: assert msg in str(e), e
    p, bus, store, r = make(); p.objs.pop('load_cell')
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'load cell' in str(e)
    # artificial PA needs no max_extrude_cross_section ...
    p, bus, store, r = make(tau=0.045); p.objs['toolhead'].ext.max_extrude_ratio = 0.3
    assert store.get_run(r.run(Gcmd(), S()))['status'] == 'complete'
    # ... but the charge move must fit max_extrude_only_velocity
    p, bus, store, r = make(); p.objs['toolhead'].ext.max_e_velocity = 10.
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'charge' in str(e), e
    for bad in (dict(pa_max=0.3), dict(cycles=1), dict(fast_time=0.1),
                dict(slow_flow=20)):
        try: S(**bad); assert False, bad
        except PAError: pass

def test_settings_amounts():
    s = Settings()
    area = 3.14159265 * 0.875 ** 2
    fast, slow = s.leg_mm(area)
    assert abs(fast - 12 / area * 0.6) < 1e-9 and abs(slow - 1.8 / area) < 1e-9
    assert abs(s.per_pa_mm(area) - (slow + 6 * (fast + slow))) < 1e-9
    assert abs(s.filament_mm(area) - (s.per_pa_mm(area) * SEARCH_MAX_TRAINS + 20)) < 1e-9
    assert abs(s.per_pa_s() - (0.3 + 1 + 6 * 1.6)) < 1e-9
    assert s.filament_mm(area) < s.max_filament        # defaults fit the cap

def test_prime_limits():
    p, bus, store, r = make()
    p.objs['toolhead'].ext.max_e_velocity = 1.0
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'FLOW' in str(e), e
    p, bus, store, r = make(tau=0.045)
    r.run(Gcmd(), S(prime_length=0))
    i = p.log.index('SET_PRESSURE_ADVANCE ADVANCE=0.00000')
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

def test_artificial_train_amounts():
    p, bus, store, r = make()
    A = Extruder.filament_area
    n0 = len(p.log)
    mk, partial = r._train_artificial(0.04, S(cycles=2), A, 0.04, p.pt)
    es = [float(l.split()[1][1:]) for l in p.log[n0:]]
    assert len(es) == 9, es                 # lead, (charge, fast, pull, slow) x 2
    vs, vf, h = 1.8 / A, 12 / A, 0.02
    charge, pull = 0.04 * (vf - vs) + (vs + vf) * h, -0.04 * (vf - vs) + (vs + vf) * h
    assert abs(es[1] - charge) < 1e-4 and abs(es[3] - pull) < 1e-4, (es, charge, pull)
    assert abs(es[2] - vf * (0.6 - 0.04)) < 1e-4
    f = [x for x in mk if x['name'] == 'fast'][0]
    assert abs(f['t1'] - f['t0'] - 0.6) < 0.002, f

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
    assert r['pa'] is None and 'higher' in r['why'], r
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
