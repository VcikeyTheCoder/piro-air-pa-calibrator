"""UI development harness: runs the REAL pa_loadcell backend on a fake printer
and serves the page with a mock Moonraker websocket, so the UI can be tried
(and screenshotted) without a printer.

  pip install websockets
  python3 tests/harness.py   ->  open http://127.0.0.1:8080/?ws=127.0.0.1:8765
Fake physics: nozzle flow follows the pushed filament with a melt lag of
TAU (0.045 s, so the right PA is 0.045); force = K * flow. Lower PA responds
slowly, higher PA overshoots. REAL_RUNS=<folder with run_000N> adds those
recorded runs to History too."""
import asyncio, json, math, os, random, re, shlex, sys, tempfile, threading
import http.server, socketserver
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'klipper'))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'piro_tap', 'tests'))
import test_backend as tb
import pa_loadcell
from pa_loadcell.errors import PAError

TAU0, K, NOISE = 0.045, 10.0, 1.2
RATE = 320.0
SPEED = int(os.environ.get('SPEED', '1'))


class Gcode(tb.Gcode):
    def __init__(s, p): super().__init__(p); s.cmds = {}; s.cap = 0
    def register_command(s, n, cb, desc=None): s.cmds[n] = cb
    def _sync(s):                       # E speed windows -> the fake load cell
        s.p.speeds.extend(s.p.windows[s.cap:]); s.cap = len(s.p.windows)
    def flush(s):
        super().flush(); s._sync()
    def run_script_from_command(s, script):
        for line in script.split('\n'):
            super().run_script_from_command(line)
            s._sync()
            m = re.match(r'SET_PRESSURE_ADVANCE ADVANCE=([\d.]+)', line)
            if m:
                s.p.pa_tl.append((s.p.pt, float(m.group(1))))


class LoadCell(tb.LoadCell):
    name = 'load_cell_probe'
    def __init__(s, p):
        super().__init__(p); s.z = 0.0; s.t = 1000.0; s.tared = True; s.tau = TAU0
        s.rng = random.Random(3)
    def is_tared(s): return s.tared
    def pa_at(s, t):
        pa = 0.0
        for tt, v in s.p.pa_tl:
            if tt <= t: pa = v
        return pa
    def u_at(s, t0, t1):
        """mean filament speed over [t0, t1): exact, so a 1 ms push piece
        is not rounded to whole samples"""
        e = 0.0
        sp = s.p.speeds
        i = s.wi = min(getattr(s, 'wi', 0), len(sp))
        while i > 0 and sp[i - 1][1] > t0: i -= 1
        while i < len(sp) and sp[i][0] < t1:
            a, b = max(sp[i][0], t0), min(sp[i][1], t1)
            if b > a: e += sp[i][2] * (b - a)
            i += 1
        s.wi = i
        return (e + s.p.e_delta(t0, t1)) / (t1 - t0)
    def pump(s):
        p = s.p
        if not p.client: return
        n1 = int((p.pt + 0.3 - 1000) * RATE)
        n0 = int((p.sent - 1000) * RATE)
        data = []
        for i in range(n0, n1):
            t = 1000 + i / RATE
            u = s.u_at(t - 1 / RATE, t); pa = s.pa_at(t)
            dz = (u - s.z) / s.tau
            s.z = u + (s.z - u) * math.exp(-1 / RATE / s.tau)
            force = K * s.z + s.rng.gauss(0, NOISE)   # Klipper PA is in the moves (test_backend.Gcode)
            counts = force * 95.783 + 345400
            data.append((t, force if s.tared else None, counts, 345400))
        if n1 > n0:
            p.sent = 1000 + n1 / RATE
            if not p.client({'data': data}): p.client = None


class Printer(tb.Printer):
    def __init__(s):
        super().__init__(); s.speeds, s.pa_tl = [], []
        s.objs['gcode'] = Gcode(s); s.objs['load_cell_probe'] = LoadCell(s)
        s.objs.pop('load_cell')
        s.objs['webhooks'] = WH(); s.busy = False

class WH:
    def __init__(s): s.eps = {}
    def register_endpoint(s, path, cb): s.eps[path] = cb


class Cfg:
    def __init__(s, p, data): s.p, s.data = p, data
    def get_printer(s): return s.p
    def get(s, n, default=None):
        return {'load_cell': 'load_cell_probe', 'data_dir': s.data}.get(n, default)
    def getfloat(s, n, default=None, **k):
        return {'park_x': 10., 'park_y': 10., 'test_x': 150., 'test_y': 20.}.get(n, default)
    def getint(s, n, default=None, **k): return default
    def getchoice(s, n, choices, default=None): return choices[default] if default in choices else default


class Req:
    error = Exception
    def __init__(s, params): s.params, s.out = params, None
    def _g(s, k, d, cast):
        v = s.params.get(k, d); return cast(v)
    def get_int(s, k, d=None): return s._g(k, d, int)
    def get_float(s, k, d=None): return s._g(k, d, float)
    def get_str(s, k, d=None): return s._g(k, d, str)
    def get(s, k, d=None): return s.params.get(k, d)
    def send(s, x): s.out = x


class Gcmd:
    def __init__(s, params): s.params, s.out = params, []
    error = Exception
    def respond_info(s, m): s.out.append(m)
    def get(s, k, d=None, **kw): return s.params.get(k, d)
    def get_float(s, k, d=None, **kw): return float(s.params[k]) if k in s.params else d
    def get_int(s, k, d=None, **kw): return int(s.params[k]) if k in s.params else d


def build(data=None):
    p = Printer()
    data = data or tempfile.mkdtemp()
    cfgdir = tempfile.mkdtemp()
    import shutil
    shutil.copy(os.path.join(HERE, '..', 'klipper', 'pa_loadcell.cfg'),
                os.path.join(cfgdir, 'pa_loadcell.cfg'))
    p.get_start_args = lambda: {'config_file': os.path.join(cfgdir, 'printer.cfg')}
    p.cfgdir = cfgdir
    plug = pa_loadcell.load_config(Cfg(p, data))
    plug._ready()                   # as klippy:ready would
    return p, plug


def seed_history(p, plug):
    """Finished PIRO-EDGE tests (different filaments, one that fails) so
    History has data; optionally real recorded runs (REAL_RUNS)."""
    import shutil
    real = os.environ.get('REAL_RUNS')
    if real and os.path.isdir(real):
        for n in sorted(os.listdir(real)):
            if re.fullmatch(r'run_\d+', n):
                shutil.copytree(os.path.join(real, n),
                                os.path.join(plug.store.root, n))
    g = p.objs['gcode']
    lc = p.objs['load_cell_probe']
    for fil, tau in (('PLA grey', 0.045), ('PETG black', 0.07)):
        lc.tau = tau
        g.cmds['PA_LOADCELL_TEST'](Gcmd({'FILAMENT': fil}))
    lc.tau = TAU0
    rid = plug.store.list_runs()[0]['id']
    plug.store.set_tag(rid, pa_pick=0.07, notes='felt right on the corner test')
    # auto PA at print start (PA_AUTO): applied, reused once, and a fallback
    auto = {'APPLY': '1', 'RESTRICT': '0', 'FAST_FLOW': '14.7', 'REUSE_HOURS': '1',
            'FILAMENT': 'Fibreel red PLA', 'MAX_FILAMENT': '1000'}
    g.cmds['PA_LOADCELL_TEST'](Gcmd(auto))
    g.cmds['PA_LOADCELL_TEST'](Gcmd(auto))
    lc.tau = 0.4
    g.cmds['PA_LOADCELL_TEST'](Gcmd(dict(auto, FILAMENT='TPU 95A', PRIME_LENGTH='0')))
    lc.tau = TAU0
    lc.tau = 0.4                   # huge melt lag: no dip up to 0.2 -> fails
    try:
        g.cmds['PA_LOADCELL_TEST'](Gcmd({'FILAMENT': 'TPU 95A',
                                         'PRIME_LENGTH': '0'}))
    except Exception:
        pass
    lc.tau = TAU0
    # a 5 minute manual recording: pushes of varying size, a slow drift and
    # two marks, so long-trace scrolling and zooming can be tried
    rng = random.Random(7)
    t, f = [], []
    for i in range(int(300 * RATE)):
        x = i / RATE
        push = 0.0
        for k in range(0, 300, 11):
            if k + 2 <= x < k + 5:
                push = 30 + (k % 40) * 2
        f.append(round(push + 3 * math.sin(x / 40) + rng.gauss(0, NOISE), 3))
        t.append(round(x, 4))
    st = plug.store
    mid = st.new_run('manual', {'filament': 'PLA grey', 'nozzle_temp': 215.0,
                                'pa_current': 0.04})
    st.add_segment(mid, {'unit': 'g', 'rate': RATE, 't': t, 'f': f,
                         'markers': [{'name': 'mark', 'label': 'nozzle wipe', 't0': 64.0, 't1': 64.0},
                                     {'name': 'mark', 'label': 'retract test', 't0': 201.5, 't1': 201.5}]})
    st.finalize(mid, 'complete')


def demo_taps():
    """PIRO-TAP taps from the simulated printer of test_piro_tap (needs a
    Klipper checkout, KLIPPER_DIR): a clean mesh and two oozing taps."""
    try:
        import test_piro_tap as tp
        if tp.load_package() is None:
            return None
        pkg, w, pp, tap = tp.make({'data_dir': tempfile.mkdtemp()},
                                  growth=0.00023)
        w.z = 3.0
        tp.home_z(pp, tap)
        tp.cmd(pp, 'PIRO_TAP_THERMAL_CALIBRATE', {'START': 0, 'END': 200,
                                                  'STEP': 50, 'SETTLE': 20})
        w.heater.set(150.)
        tp.cmd(pp, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 215, 'REF': 150})
        sess = tap.start_probe_session(tp.Gcmd())
        for i, (x, y) in enumerate([(30, 30), (110, 30), (190, 30), (30, 110),
                                    (110, 110), (190, 110)]):
            w.xy[:] = [x, y]
            w.z_bed = 0.02 * math.sin(x / 50.) + 0.01 * (y / 100.)
            w.z = w.z_bed + 0.6
            sess.run_probe(tp.Gcmd())
        sess.end_probe_session()
        w.ooze, w.relax = 0.05, 4.
        w.z = 0.7
        try:
            tp.cmd(pp, 'NOZZLE_CLEAN_CHECK', {'MAX_TAPS': 3})
        except Exception:
            pass
        return {'eps': pp.objs['webhooks'].eps, 'tap': tap}
    except Exception:
        import traceback
        traceback.print_exc()
        return None


def main(seed=True, host='127.0.0.1', ws_port=8765, http_port=8080):
    import websockets
    p, plug = build()
    if seed:
        seed_history(p, plug)
    eps = p.objs['webhooks'].eps
    lc = p.objs['load_cell_probe']
    state = {'t': 0.0}
    taps = demo_taps()

    async def tick():
        while True:
            await asyncio.sleep(0.1)
            if p.busy: continue
            for _ in range(SPEED):               # SPEED>1 fast-forwards time
                p.now += 0.1; p.pt += 0.1; state['t'] += 0.1
                if int(round(state['t'] * 10)) % 90 == 0:   # a push every 9 s
                    p.speeds.append((p.pt, p.pt + 1.2, 3.0))
                lc.pump()

    def call(method, params):
        if method == 'printer.gcode.script':
            parts = shlex.split(params['script'])
            cmd, kv = parts[0], dict(x.split('=', 1) for x in parts[1:] if '=' in x)
            if cmd == 'PA_LOADCELL_TEST':
                kv = {k.upper(): v for k, v in kv.items()}
                p.busy = True
                try:
                    p.objs['gcode'].cmds[cmd](Gcmd(kv))
                finally:
                    p.busy = False
                return 'ok'
            if cmd == 'LOAD_CELL_TARE':
                lc.tared = True; return 'ok'
            if cmd == 'PA_DEMO_RUNNING':         # UI screenshots: a test in progress
                r = plug.runner
                r.state, r.phase, r.current_pa, r.values, r.max_values = 'running', 'fast', 0.0485, 4, 7
                r.progress, r.pa_t, r.started = 0.55, p.pt - 2.0, p.now - 64
                r.search = {'steps': [{'pa': 0.0, 'stage': 'reference'}, {'pa': 0.1, 'stage': 'upper'},
                                      {'pa': 0.053, 'stage': 'area'}], 'prediction': 0.0487}
                r.last = {'run': 3, 'status': 'complete', 'pa': 0.0487, 'unc': 0.0003, 'grade': 'good'}
                t = p.pt + 0.1
                for k in range(6):
                    r._log_leg(t, t + 0.6, 12.0, 'fast'); r._log_leg(t + 0.6, t + 1.6, 1.8, 'slow')
                    p.speeds.append((t, t + 0.6, 12.0)); t += 1.6
                return 'ok'
            return 'ok'
        if method == 'printer.objects.list':
            objs = [] if os.environ.get('PROBE_ONLY') else ['pa_loadcell']
            return {'objects': objs + (['piro_tap'] if taps else [])}
        if method.startswith('printer.piro_tap.'):
            if not taps:
                raise Exception('piro_tap not configured')
            r = Req(params)
            taps['eps']['piro_tap/' + method.split('.')[-1]](r)
            return r.out
        ep = 'pa_loadcell/' + method.split('.')[-1]
        r = Req(params)
        eps[ep](r)
        return r.out

    async def handler(ws):
        async for raw in ws:
            m = json.loads(raw)
            try:
                res = {'result': call(m['method'], m.get('params', {}))}
            except Exception as e:
                res = {'error': {'code': -1, 'message': str(e)}}
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': m['id'], **res}))

    async def serve():
        asyncio.create_task(tick())
        async with websockets.serve(handler, host, ws_port):
            await asyncio.Future()

    UI = os.path.join(HERE, '..', 'ui')
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k): super().__init__(*a, directory=UI, **k)
        def log_message(s, *a): pass
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer((host, http_port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print('UI: http://%s:%d/   (mock websocket :%d)' % (host, http_port, ws_port))
    asyncio.run(serve())


if __name__ == '__main__':
    main()
