"""Tests for PIRO-LC.

  python3 loadcell/tests/test_piro_lc.py
      unit tests (no Klipper needed)
  KLIPPER_DIR=~/klipper python3 loadcell/tests/test_piro_lc.py
      also runs a batch print through real Klipper (needs out/klipper.dict
      from a linux MCU build in that Klipper, and the module linked to
      klippy/extras/piro_lc)"""
import collections
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, '..', 'klipper', 'piro_lc')


def load_pkg():
    # load piro_lc as a package without Klipper (relative imports of
    # Klipper modules fall back to None)
    name = 'piro_lc'
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PKG, '__init__.py'), submodule_search_locations=[PKG])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lc_mod = load_pkg()
homing = sys.modules['piro_lc.homing']
thermal = sys.modules['piro_lc.thermal']
collect = sys.modules['piro_lc.collect']

Res = collections.namedtuple('probe_result', ['bed_x', 'bed_y', 'bed_z',
                                              'test_x', 'test_y', 'test_z'])


class Err(Exception):
    pass


class FakeGcmd:
    def __init__(self, cmd, params):
        self.cmd, self.params = cmd, dict(params)

    def get_command(self):
        return self.cmd

    def get(self, k, default=None):
        return self.params.get(k, default)

    def get_command_parameters(self):
        return self.params


class FakeGcode:
    def create_gcode_command(self, cmd, raw, params):
        return FakeGcmd(cmd, params)


class FakeToolhead:
    def __init__(self):
        self.pos = [150., 150., 5., 0.]
        self.moves = []

    def get_position(self):
        return list(self.pos)

    def manual_move(self, pos, speed):
        self.moves.append((pos, speed))
        self.pos[:len(pos)] = pos


class FakePrinter:
    command_error = Err

    def __init__(self):
        self.objs = {'gcode': FakeGcode(), 'toolhead': FakeToolhead()}

    def lookup_object(self, n, default=None):
        return self.objs.get(n, default)


class FakeInner:
    """Klipper's probe session: returns the given Z values in order and
    remembers the HOME_ATTEMPT_NUM of every tap."""
    def __init__(self, zs):
        self.zs = list(zs)
        self.res = []
        self.attempts = []
        self.ended = False

    def run_probe(self, gcmd):
        self.attempts.append(gcmd.get('HOME_ATTEMPT_NUM'))
        z = self.zs.pop(0)
        self.res.append(Res(150., 150., z, 150., 150., z))

    def pull_probed_results(self):
        r, self.res = self.res, []
        return r

    def end_probe_session(self):
        self.ended = True


class FakeProbe:
    def get_probe_params(self, gcmd=None):
        return {'sample_retract_dist': 2., 'lift_speed': 2.}


def fake_lc(**opts):
    o = types.SimpleNamespace(first_speed=10., second_speed=2.,
                              first_samples=2, second_samples=2,
                              tolerance=0.01, retries=2)
    for k, v in opts.items():
        setattr(o, k, v)
    lc = types.SimpleNamespace(printer=FakePrinter(), opts=o,
                               probe=FakeProbe(), paper_offset=0.,
                               msgs=[], homed=None)
    lc.respond = lc.msgs.append
    lc.on_z_homed = lambda t: setattr(lc, 'homed', t)
    return lc


def test_two_samples_median_and_first_tap_only():
    lc = fake_lc()
    inner = FakeInner([0.300, 0.302, 0.250, 0.252])
    s = homing.HomeSession(lc, inner)
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '1'}))
    r1 = s.pull_probed_results()[0]
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '2'}))
    r2 = s.pull_probed_results()[0]
    s.end_probe_session()
    # Klipper's re-zero only on the first tap of touch 1
    assert inner.attempts == ['1', '1.1', '2', '2'], inner.attempts
    assert abs(r1.bed_z - 0.301) < 1e-9 and abs(r2.bed_z - 0.251) < 1e-9
    assert inner.ended and lc.homed is not None


def test_speeds_per_touch():
    lc = fake_lc(first_speed=10., second_speed=2.)
    speeds = []
    inner = FakeInner([0.3, 0.3, 0.2, 0.2])
    orig = inner.run_probe

    def rp(g):
        speeds.append(float(g.get('PROBE_SPEED')))
        orig(g)
    inner.run_probe = rp
    s = homing.HomeSession(lc, inner)
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '1'}))
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '2'}))
    assert speeds == [10., 10., 2., 2.], speeds


def test_retry_then_ok():
    lc = fake_lc()
    inner = FakeInner([0.30, 0.35, 0.31, 0.312])   # first pair too far apart
    s = homing.HomeSession(lc, inner)
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '2'}))
    r = s.pull_probed_results()[0]
    assert abs(r.bed_z - 0.311) < 1e-9 and any('retrying' in m for m in lc.msgs)


def test_retries_exhausted_is_error():
    lc = fake_lc(retries=1)
    inner = FakeInner([0.30, 0.40, 0.30, 0.40])
    s = homing.HomeSession(lc, inner)
    try:
        s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '2'}))
    except Err as e:
        assert 'homing_samples_tolerance' in str(e)
    else:
        raise AssertionError("no error")


def test_paper_offset_added_to_result():
    lc = fake_lc(second_samples=1)
    lc.paper_offset = 0.1
    s = homing.HomeSession(lc, FakeInner([0.25]))
    s.run_probe(FakeGcmd('G28', {'HOME_ATTEMPT_NUM': '2'}))
    assert abs(s.pull_probed_results()[0].bed_z - 0.35) < 1e-9


def test_growth_offset_and_fit():
    assert abs(thermal.growth_offset(0.00005, 240, 150) - 0.0045) < 1e-12
    a, b, worst = thermal.fit_line([20, 120, 220], [0.10, 0.105, 0.11])
    assert abs(b - 0.00005) < 1e-12 and worst < 1e-12
    assert thermal.steps(0, 200, 50) == [0, 50, 100, 150, 200]


def test_summary():
    s = collect.summary('accuracy', [{'z': 0.1, 'nozzle': 150},
                                     {'z': 0.104, 'nozzle': 152}])
    assert s['n'] == 2 and abs(s['range'] - 0.004) < 1e-9 and s['nozzle'] == 151


def test_store_roundtrip():
    store_mod = sys.modules['piro_lc.store']
    d = tempfile.mkdtemp()
    try:
        st = store_mod.Store(d)
        st.set('home_temp', 151.5)
        sess = {'id': st.new_id(), 'kind': 'accuracy', 'start': 1., 'end': 2.,
                'n': 1, 'summary': {}, 'ok': True, 'touches': [{'z': 0.1}]}
        st.save_session(sess)
        import time
        for _ in range(100):
            if st.writer.q.empty() and os.path.exists(
                    os.path.join(d, 'sessions', 'index.json')):
                break
            time.sleep(0.02)
        time.sleep(0.1)
        st2 = store_mod.Store(d)
        assert st2.get('home_temp') == 151.5
        assert st2.list()[0]['kind'] == 'accuracy'
        assert st2.load(sess['id'])['touches'][0]['z'] == 0.1
    finally:
        shutil.rmtree(d)


# -- real Klipper (batch mode) ---------------------------------------------
BATCH_CFG = os.path.join(HERE, 'batch', 'printer.cfg')
BATCH_GCODE = os.path.join(HERE, 'batch', 'test.gcode')


def test_real_klipper():
    kd = os.environ.get('KLIPPER_DIR')
    if not kd:
        print("SKIP real Klipper: set KLIPPER_DIR")
        return
    kd = os.path.expanduser(kd)
    dict_ = os.path.join(kd, 'out', 'klipper.dict')
    if not os.path.exists(dict_):
        print("SKIP real Klipper: no %s (build the linux MCU)" % dict_)
        return
    if not os.path.exists(os.path.join(kd, 'klippy', 'extras', 'piro_lc')):
        print("SKIP real Klipper: link klippy/extras/piro_lc first")
        return
    tmp = tempfile.mkdtemp()
    try:
        tmpl = open(os.path.join(HERE, '..', 'piro_lc.cfg')).read().replace(
            '#data_dir: ~/printer_data/prio_edge/loadcell (default)',
            'data_dir: ' + os.path.join(tmp, 'd'))
        cfg = open(BATCH_CFG).read() + '\n' + tmpl
        with open(os.path.join(tmp, 'p.cfg'), 'w') as f:
            f.write(cfg)
        log = os.path.join(tmp, 'k.log')
        subprocess.call([sys.executable, os.path.join(kd, 'klippy', 'klippy.py'),
                         os.path.join(tmp, 'p.cfg'), '-i', BATCH_GCODE,
                         '-o', os.path.join(tmp, 'out'), '-v', '-d', dict_,
                         '-l', log], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=600)
        text = open(log).read()
        assert 'Exiting' in text, text[-2000:]
        assert 'Traceback' not in text.split('=======================')[-1], \
            text[-3000:]
        assert 'paper offset +0.1000 mm' in text
        assert 'homing test saved (8 touches)' in text
        assert 'accuracy test saved (4 touches)' in text
        assert 'nozzle_growth is 0, no growth correction' in text   # macro ran
        idx = json.load(open(os.path.join(tmp, 'd', 'sessions', 'index.json')))
        assert [r['kind'] for r in idx] == ['accuracy', 'homing']
    finally:
        shutil.rmtree(tmp)


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("%d tests passed" % len(tests))
