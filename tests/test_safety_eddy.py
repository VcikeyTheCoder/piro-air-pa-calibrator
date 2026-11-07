"""Safety and sensor tests: the BTT Eddy path (Hz, sign, both sensors), the
webhook guard, checked imports, crash-safe storage, Klipper shutdown,
dropped samples, the status line and background analysis.
Run: python3 tests/test_safety_eddy.py"""
import json, math, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'klipper'))
import test_backend as tb
import harness as h
from pa_loadcell.feed import SampleBus
from pa_loadcell.storage import RunStore
from pa_loadcell.testrun import PATestRunner
from pa_loadcell.errors import PAError
from pa_loadcell import fileio, cfgfile

LIM = {'min_temp': 200, 'required_z': 20, 'z_tol': 0.5, 'timeout': 3}
D = '/mnt/user-data/attachments/05627bcc-6f43-550a-acab-7fa6df17b8cb/'


class FakeEddy(tb.LagCell):
    """Mimics ldc1612.py through [pa_eddy]: add_client gets (time, Hz, z)
    rows at 400/s. More nozzle pressure moves the target closer, so the
    frequency goes DOWN (the plugin must flip the sign)."""
    RATE = 400.0
    def __init__(s, p, tau, hz_per=-25.0, drop=0.0):
        super().__init__(p, tau, noise=0.3)
        s.client, s.sent, s.hz_per, s.drop, s.n = None, 1000.0, hz_per, drop, 0
        s.errors = 0
    def add_client(s, cb):
        s.client = cb; s.p.hooks.append(s.pump)
    def pump(s):
        p = s.p
        if not s.client: return
        n0, n1 = int((s.sent - 1000) * s.RATE), int((p.pt - 1000) * s.RATE) + 2
        if n1 <= n0: return
        data, dt = [], 1 / s.RATE
        k = math.exp(-dt / s.tau)
        for i in range(n0, n1):
            t = 1000 + i / s.RATE
            u = s.vel(t - dt, t)
            s.q = u + (s.q - u) * k
            s.n += 1
            if s.drop and (s.n % int(1 / s.drop)) == 0:
                continue                         # a lost sample
            data.append((t, 3.1e6 + s.hz_per * s.q + s.rng.gauss(0, s.noise), 999.9))
        s.sent = 1000 + n1 / s.RATE
        if not s.client({'data': data, 'errors': s.errors, 'overflows': 0}):
            s.client = None


def make(sensor='eddy', primary='load_cell', tau=0.045, drop=0.0):
    p = tb.Printer()
    p.objs['load_cell'] = tb.LagCell(p, tau)
    p.objs['pa_eddy'] = FakeEddy(p, tau, drop=drop)
    root = tempfile.mkdtemp()
    bus = SampleBus(p, 'load_cell', 60000, 15.0, sensor=sensor,
                    primary=primary, state_dir=root)
    bus.set_ready()
    store = RunStore(root)
    return p, bus, store, PATestRunner(p, bus, store, LIM)


def test_eddy_alone_flips_sign_and_finds_pa():
    p, bus, store, r = make('eddy')
    g = tb.Gcmd()
    m = store.get_run(r.run(g, tb.S(prime_steady=1, max_filament=1000)))
    res = m['analysis']['result']
    assert m['status'] == 'complete', m.get('error')
    assert m['sensor'] == 'eddy' and m['unit'] == 'Hz', (m['sensor'], m['unit'])
    assert res['method'] == 'PIRO-FALL' and abs(res['pa'] - 0.049) < 0.004, res['pa']
    assert any('sign flipped' in x for x in g.out), g.out
    assert bus.main.sign == -1
    st = json.load(open(os.path.join(store.root, 'sensor_state.json')))
    assert st['eddy']['sign'] == -1                  # remembered
    seg = store.get_segment(m['id'], 1)
    assert seg['unit'] == 'Hz' and seg['sensor'] == 'eddy'
    # the pushing force reads higher than rest after the flip
    fast = [mk for mk in seg['markers'] if mk['name'] == 'fast'][1]
    lv = lambda a, b: sum(f for t, f in zip(seg['t'], seg['f']) if a <= t <= b) / \
        max(1, sum(1 for t in seg['t'] if a <= t <= b))
    assert lv(fast['t1'] - 0.2, fast['t1']) > lv(fast['t1'] + 0.6, fast['t1'] + 0.9)
    # next start: the sign is read back, no flip needed
    bus2 = SampleBus(p, 'load_cell', 1000, 15.0, sensor='eddy', state_dir=store.root)
    assert bus2.main.sign == -1


def test_eddy_needs_a_pressure_change():
    p, bus, store, r = make('eddy')
    p.objs['pa_eddy'].hz_per = 0.0                   # mount does not move
    try:
        r.run(tb.Gcmd(), tb.S(prime_steady=1, max_filament=1000)); assert False
    except PAError as e:
        assert 'barely changes' in str(e), e


def test_both_sensors_record_and_compare():
    p, bus, store, r = make('both', 'load_cell')
    g = tb.Gcmd()
    m = store.get_run(r.run(g, tb.S(max_filament=1000)))
    a = m['analysis']
    assert m['sensor'] == 'both' and m['primary'] == 'load_cell'
    assert a['result']['pa'] is not None and 'eddy' in a['channels'], a.get('channels')
    other = a['channels']['eddy']
    assert abs(other['pa'] - a['result']['pa']) < 0.004, (other, a['result']['pa'])
    seg = store.get_segment(m['id'], 1)
    assert seg['channels']['eddy']['unit'] == 'Hz' and len(seg['channels']['eddy']['t']) > 1000
    assert any('compared, not applied' in x for x in g.out), g.out
    # the eddy can be the judge instead
    p, bus, store, r = make('both', 'eddy')
    m = store.get_run(r.run(tb.Gcmd(), tb.S(max_filament=1000)))
    assert m['primary'] == 'eddy' and 'load_cell' in m['analysis']['channels']


def test_dropped_samples_stop_the_run():
    p, bus, store, r = make('eddy', drop=0.1)        # every 10th sample lost
    try:
        r.run(tb.Gcmd(), tb.S(max_filament=1000)); assert False
    except PAError as e:
        assert 'dropped' in str(e), e


def test_status_line_and_no_m73():
    p, bus, store, r = tb.make(tau=0.045)
    r.run(tb.Gcmd(), tb.S())                       # no [display_status]: silent
    assert not [l for l in p.log if l.startswith('SET_DISPLAY_TEXT')]
    p, bus, store, r = tb.make(tau=0.045)
    p.objs['display_status'] = object()
    r.run(tb.Gcmd(), tb.S())
    disp = [l for l in p.log if l.startswith('SET_DISPLAY_TEXT')]
    assert disp[0] == 'SET_DISPLAY_TEXT MSG="PA test: priming"', disp
    assert any(l.startswith('SET_DISPLAY_TEXT MSG="PA test 1/7: PA 0.0000"') for l in disp), disp
    assert disp[-1] == 'SET_DISPLAY_TEXT'                            # cleared
    assert not any(l.startswith('M73') for l in p.log)


def test_shutdown_stops_without_gcode():
    p, bus, store, r = tb.make(tau=0.045)
    g = p.objs['gcode']; orig = g.run_script_from_command
    state = {'n': 0, 'cut': None}
    def hook(script):
        if r.dead:
            raise Exception("Klipper is shut down")        # like gcode.py after a shutdown
        orig(script)
        state['n'] += 1
        if state['n'] == 40:
            r.on_shutdown(); state['cut'] = len(p.log)
    g.run_script_from_command = hook
    try:
        r.run(tb.Gcmd(), tb.S()); assert False
    except PAError as e:
        assert 'shut down' in str(e), e
    m = store.get_run(r.run_id)
    assert m['status'] == 'error' and 'shut down' in m['error'], (m['status'], m['error'])
    assert len(p.log) == state['cut'], p.log[state['cut']:]          # nothing sent after
    assert r.state == 'idle'


def test_storage_index_interrupted_and_damage():
    root = tempfile.mkdtemp()
    st = RunStore(root)
    rid = st.new_run('test', {'settings': {}})              # left 'running'
    st2 = RunStore(root)                                    # Klipper restarted
    assert st2.mark_interrupted() == 1
    assert st2.get_run(rid)['status'] == 'interrupted'
    assert st2.list_runs()[0]['status'] == 'interrupted'
    os.remove(os.path.join(root, 'index.json'))             # cache lost
    assert [x['id'] for x in RunStore(root).list_runs()] == [rid]
    open(os.path.join(root, 'index.json'), 'w').write('{broken')
    assert [x['id'] for x in RunStore(root).list_runs()] == [rid]
    open(os.path.join(root, 'run_%04d' % rid, 'meta.json'), 'w').write('{broken')
    st3 = RunStore(root)
    for fn in (lambda: st3.get_run(rid), lambda: st3.get_run(99),
               lambda: st3.get_segment(rid, 0), lambda: st3.export_run(77)):
        try:
            fn(); assert False
        except PAError:
            pass
    assert not [n for n in os.listdir(root) if n.endswith('.tmp')]    # no leftovers


def test_import_is_checked():
    st = RunStore(tempfile.mkdtemp())
    good = json.load(open([os.path.join(D, n) for n in os.listdir(D) if 'Run_79' in n][0]))
    evil = json.loads(json.dumps(good))
    evil['meta']['pa_pick'] = '<img src=x onerror=alert(1)>'
    evil['meta']['auto'] = {'applied': '<script>x</script>', 'reason': 'r', 'fallback': 0}
    evil['meta']['search']['steps'][0]['pa'] = '"><svg onload=alert(1)>'
    evil['meta']['name'] = 'x' * 5000
    rid = st.import_run(evil)
    m = st.get_run(rid)
    assert m['pa_pick'] is None and m['auto']['applied'] is None, (m['pa_pick'], m['auto'])
    assert m['search']['steps'][0]['pa'] is None and len(m['name']) <= 220
    assert m['analysis']['result']['pa'] is not None                 # data intact
    for bad in ({'version': 'x'}, {'version': 9}):
        d = dict(good, **bad)
        try:
            st.import_run(d); assert False, bad
        except PAError:
            pass
    d = json.loads(json.dumps(good)); d['segments'][0]['f'][5] = 'abc'
    try:
        st.import_run(d); assert False
    except PAError as e:
        assert 'damaged' in str(e)
    d = json.loads(json.dumps(good)); d['segments'][0]['pa'] = 'abc'
    rid2 = st.import_run(d)                                          # pa -> None, no crash
    assert st.get_run(rid2)['segments'][0]['pa'] is None


def test_endpoints_never_crash_klipper():
    class ReqError(Exception):
        pass
    class Req(h.Req):
        error = ReqError
    p, plug = h.build()
    def call(name, **kw):
        r = Req(kw); p.objs['webhooks'].eps['pa_loadcell/' + name](r); return r.out
    bad = [('run', {'run': 999}), ('segment', {'run': 999, 'idx': 0}),
           ('export', {'run': 5}), ('reanalyze', {'run': 5}),
           ('tag', {'run': 999, 'pa_pick': '0.05'}),
           ('import_end', {'token': 'nope'}), ('tare', {'sensor': 'bogus'})]
    for name, kw in bad:
        try:
            call(name, **kw); assert False, name
        except ReqError:
            pass
    plug.store.list_runs = lambda: 1 / 0               # an unexpected bug
    try:
        call('runs'); assert False
    except ReqError as e:
        assert 'pa_loadcell' in str(e)
    # a typed comma is accepted, text is refused, range checked
    rid = plug.store.new_run('manual', {})
    call('tag', run=rid, pa_pick='0,045')
    assert plug.store.get_run(rid)['pa_pick'] == 0.045
    for v in ('abc', '5'):
        try:
            call('tag', run=rid, pa_pick=v); assert False
        except ReqError:
            pass
    # config: no line breaks of any kind, no sections
    for v in ('PLA\r[gcode_macro X]', 'PLA\n', 'a[b]', 'x\x00'):
        try:
            call('config_save', values={'filament': v}); assert False, repr(v)
        except ReqError:
            pass
    st = call('status')
    assert st['state'] == 'idle' and 'last' in st and st['sensor'] == 'load_cell'


def test_background_work_runs_in_another_process():
    p = tb.Printer()
    pid = fileio.in_background(p, os.getpid)
    assert pid != os.getpid()
    try:
        fileio.in_background(p, int, ('x',)); assert False
    except RuntimeError as e:
        assert 'invalid literal' in str(e)


def test_recording_saved_outside_the_sensor_callback():
    p, bus, store, r = tb.make()
    cbs = []
    p.register_callback = lambda cb, *a: cbs.append(cb)
    from pa_loadcell.recorder import ManualRecorder
    rec = ManualRecorder(p, bus, store, 0.5)
    rec.start('x')
    rec._on([(1000.0, 1.0), (1000.3, 1.0)])
    rec._on([(1000.6, 1.0)])                           # past max: no save yet
    assert rec.active and len(cbs) == 1 and not store.list_runs()
    cbs[0](0)                                          # the reactor runs it later
    assert not rec.active and store.list_runs()[0]['kind'] == 'manual'


def test_second_eddy_sign_fixed_and_no_prime_steady():
    p, bus, store, r = make('both', 'load_cell')
    g = tb.Gcmd()
    m = store.get_run(r.run(g, tb.S(max_filament=1000)))          # prime_steady 0
    assert bus.other.sign == -1 and any('sign flipped' in x for x in g.out), g.out
    p, bus, store, r = make('eddy')
    m = store.get_run(r.run(tb.Gcmd(), tb.S(max_filament=1000)))   # main, no steady
    assert bus.main.sign == -1 and m['analysis']['result']['pa'] is not None


def test_result_is_set_before_idle_and_unknown_mode():
    p, bus, store, r = tb.make(tau=0.045)
    seen = {}
    orig = store.set_search
    def spy(rid, search):
        seen['state'], seen['result'] = r.state, search.get('result')
        orig(rid, search)
    store.set_search = spy
    rid = r.run(tb.Gcmd(), tb.S())
    assert seen['state'] != 'idle' and seen['result'] is not None, seen
    assert r.last['pa'] == store.get_run(rid)['analysis']['result']['pa']
    # a mode this version does not know: analysed as edge, never a crash
    good = json.load(open([os.path.join(D, n) for n in os.listdir(D) if 'Run_79' in n][0]))
    good['meta']['settings']['mode'] = 'rise'
    st = RunStore(tempfile.mkdtemp())
    assert st.get_run(st.import_run(good))['analysis']['result']['method'] == 'PIRO-EDGE'
    # no edits while a run is being analysed in the background
    st.busy.add(1)
    try:
        st.set_tag(1, notes='x'); assert False
    except PAError as e:
        assert 'being analysed' in str(e)


if __name__ == '__main__':
    import time
    for n, fn in list(globals().items()):
        if n.startswith('test_'):
            t0 = time.time(); fn(); print('ok %-45s %.1fs' % (n, time.time() - t0))
