"""Offline tests with a fake printer: no Klipper needed.
Run: python3 tests/test_backend.py"""
import os, re, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klipper'))
from pa_loadcell.feed import SampleBus
from pa_loadcell.recorder import ManualRecorder
from pa_loadcell.settings import Settings
from pa_loadcell.storage import RunStore, decimate
from pa_loadcell.testrun import PATestRunner
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
    def __init__(s, p): s.p, s.n_e = p, 0
    def run_script_from_command(s, script):
        for line in script.split('\n'):
            s.p.log.append(line)
            m = re.match(r'G1 E([\d.]+) F([\d.]+)', line)
            if m:
                L, F = float(m.group(1)), float(m.group(2)) / 60
                s.p.windows.append((s.p.pt, s.p.pt + L / F))
                s.p.pt += L / F
                s.n_e += 1

class LoadCell:
    def __init__(s, p): s.p = p
    def get_sensor(s): return s
    def add_client(s, cb): s.p.client = cb; s.p.hooks.append(s.pump)
    def counts_to_grams(s, c): return c / 100.0
    def pump(s):
        p = s.p
        if not p.client: return
        n0 = int((p.sent - 1000) * 320)
        n1 = int((p.pt + 0.3 - 1000) * 320)  # stream runs past the last move
        if n1 <= n0: return
        data = []
        for i in range(n0, n1):
            t = 1000 + i / 320.0
            on = any(a <= t <= b for a, b in p.windows)
            data.append((t, 10000.0 if on else 0.0))
        p.sent = 1000 + n1 / 320.0
        if p.client({'data': data}):
            p.client = None

class Gcmd:
    def __init__(s): s.out = []
    def respond_info(s, m): s.out.append(m)


def make(temp=210.0, z=20.0):
    p = Printer(temp, z)
    bus = SampleBus(p, 'load_cell', 60000, 15.0)
    store = RunStore(tempfile.mkdtemp())
    lim = {'min_temp': 200, 'required_z': 20, 'z_tol': 0.5, 'timeout': 3}
    return p, bus, store, PATestRunner(p, bus, store, lim)

def S(**kw):
    d = dict(pa_start=0, pa_end=0.004, pa_step=0.002, sweeps=2,
             extrude_len=10, slow_speed=5, fast_speed=20, test_x=100,
             test_y=100, park_x=10, park_y=10)
    d.update(kw); s = Settings(**d); s.validate(); return s


def test_full_run():
    p, bus, store, r = make()
    rid = r.run(Gcmd(), S())
    m = store.get_run(rid)
    assert m['status'] == 'complete' and len(m['segments']) == 3, m
    seg = store.get_segment(rid, 1)
    assert seg['pa'] == 0.002 and len(seg['markers']) == 4
    assert seg['samples'] > 500 and abs(len(seg['t']) - len(seg['f'])) == 0
    assert max(seg['f']) > 90 and min(seg['f']) < 1       # force pulses seen
    assert any('SET_PRESSURE_ADVANCE ADVANCE=0.05000' in l for l in p.log)
    assert 'G1 X10.00 Y10.00 F6000' in p.log                # parked at end

def test_cancel_saves_partial():
    p, bus, store, r = make()
    g = p.objs['gcode']; orig = g.run_script_from_command
    def hook(script):
        orig(script)
        if g.n_e == 5: r.cancel()
    g.run_script_from_command = hook
    rid = r.run(Gcmd(), S(pa_end=0.02))
    m = store.get_run(rid)
    assert m['status'] == 'cancelled' and 1 <= len(m['segments']) < 11, m

def test_safety():
    for kw, msg in ((dict(temp=150.0), 'nozzle'), (dict(z=5.0), 'Z must')):
        p, bus, store, r = make(**kw)
        try: r.run(Gcmd(), S()); assert False
        except PAError as e: assert msg in str(e), e
    p, bus, store, r = make(); p.objs.pop('load_cell')
    try: r.run(Gcmd(), S()); assert False
    except PAError as e: assert 'load cell' in str(e)

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

if __name__ == '__main__':
    for n, fn in list(globals().items()):
        if n.startswith('test_'): fn(); print('ok', n)
