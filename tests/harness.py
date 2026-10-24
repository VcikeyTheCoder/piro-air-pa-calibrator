"""UI development harness: runs the REAL pa_loadcell backend on a fake printer
and serves the page with a mock Moonraker websocket, so the UI can be tried
(and screenshotted) without a printer.

  pip install websockets
  python3 tests/harness.py   ->  open http://127.0.0.1:8080/?ws=127.0.0.1:8765
Fake physics: force = K * lag(speed) with PA acting as a lead term, so the
best PA is 0.045; lower PA responds slowly, higher PA overshoots."""
import asyncio, json, math, os, random, re, shlex, sys, tempfile, threading
import http.server, socketserver
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'klipper'))
sys.path.insert(0, HERE)
import test_backend as tb
import pa_loadcell
from pa_loadcell.errors import PAError

TAU0, K, NOISE = 0.045, 4.0, 1.2
RATE = 320.0
SPEED = int(os.environ.get('SPEED', '1'))


class Gcode(tb.Gcode):
    def __init__(s, p): super().__init__(p); s.cmds = {}
    def register_command(s, n, cb, desc=None): s.cmds[n] = cb
    def run_script_from_command(s, script):
        for line in script.split('\n'):
            m = re.match(r'G1 E([\d.]+) F([\d.]+)', line)
            if m:
                L, F = float(m.group(1)), float(m.group(2)) / 60
                s.p.speeds.append((s.p.pt, s.p.pt + L / F, F))
            m = re.match(r'SET_PRESSURE_ADVANCE ADVANCE=([\d.]+)', line)
            if m:
                s.p.pa_tl.append((s.p.pt, float(m.group(1))))
        super().run_script_from_command(script)


class LoadCell(tb.LoadCell):
    name = 'load_cell_probe'
    def __init__(s, p):
        super().__init__(p); s.z = 0.0; s.t = 1000.0; s.tared = True
        s.rng = random.Random(3)
    def is_tared(s): return s.tared
    def pa_at(s, t):
        pa = 0.0
        for tt, v in s.p.pa_tl:
            if tt <= t: pa = v
        return pa
    def u_at(s, t):
        for a, b, v in s.p.speeds:
            if a <= t < b: return v
        return 0.0
    def pump(s):
        p = s.p
        if not p.client: return
        n1 = int((p.pt + 0.3 - 1000) * RATE)
        n0 = int((p.sent - 1000) * RATE)
        data = []
        for i in range(n0, n1):
            t = 1000 + i / RATE
            u = s.u_at(t); pa = s.pa_at(t)
            dz = (u - s.z) / TAU0
            s.z += dz / RATE
            force = K * (s.z + pa * dz) + s.rng.gauss(0, NOISE)
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
    plug = pa_loadcell.load_config(Cfg(p, data))
    return p, plug


def seed_history(p, plug):
    """Two finished tests (different filaments) so History/Compare have data."""
    g = p.objs['gcode']
    for fil in ('PLA grey', 'PETG black'):
        g.cmds['PA_LOADCELL_TEST'](Gcmd({'PA_START': '0', 'PA_END': '0.08',
            'PA_STEP': '0.01', 'SWEEPS': '3', 'FILAMENT': fil}))
    rid = plug.store.list_runs()[0]['id']
    plug.store.set_tag(rid, pa_pick=0.04, notes='felt right on the 0.5 corner')
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


def main(seed=True, host='127.0.0.1', ws_port=8765, http_port=8080):
    import websockets
    p, plug = build()
    if seed:
        seed_history(p, plug)
    eps = p.objs['webhooks'].eps
    lc = p.objs['load_cell_probe']
    state = {'t': 0.0}

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
            return 'ok'
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
