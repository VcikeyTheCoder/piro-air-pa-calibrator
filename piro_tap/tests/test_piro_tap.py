"""PIRO-TAP tests: filters, tap analysis, the Eddy hub (start after ready,
rate switching, stuck driver, counts) and whole probe taps against a
simulated printer: a nozzle that presses an Eddy-sensed mount, an MCU
trigger that runs the same filter and trigger rules as Klipper's
trigger_analog.c, and stepper history for the pullback.

The probe tests use Klipper's real probe.py / manual_probe.py:
  KLIPPER_DIR=~/klipper python3 piro_tap/tests/test_piro_tap.py
Without a Klipper checkout those tests are skipped."""
import importlib.util
import math
import os
import random
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', 'klipper'))     # piro_tap/
SHARED = os.path.abspath(os.path.join(HERE, '..', '..', 'shared'))
PA = os.path.abspath(os.path.join(HERE, '..', '..', 'klipper'))   # pa_loadcell
sys.path[:0] = [REPO, SHARED, PA]
from piro_tap import filters, tapfit, thermal    # noqa: E402
from piro_eddy import LdcHub                     # noqa: E402

KL = os.path.expanduser(os.environ.get('KLIPPER_DIR', '~/klipper'))
FREQ_CONV = 0.0894                    # Hz per raw count (12 MHz, div 2)


# -- simulated world -----------------------------------------------------------
class CmdError(Exception):
    pass


class Reactor:
    def __init__(s, world):
        s.w = world
        s.cbs = []

    def monotonic(s):
        return s.w.now

    def pause(s, t):
        s.w.advance(max(t, s.w.now + 0.001))
        return t

    def register_callback(s, cb, *a):
        s.cbs.append(cb)

    def run_callbacks(s):
        cbs, s.cbs = s.cbs, []
        for cb in cbs:
            cb(s.w.now)

    def register_timer(s, *a):
        return object()

    def unregister_timer(s, *a):
        pass

    NEVER = 9e99


class BatchBulk:
    def __init__(s, sensor):
        s.sensor = sensor
        s.is_started = False
        s.client_cbs = []
        s.batch_timer = None

    def add_client(s, cb):
        s.client_cbs.append(cb)
        if not s.is_started:
            s.is_started = True
            s.sensor.start_cb()
            s.batch_timer = object()

    def _stop(s):
        del s.client_cbs[:]
        s.batch_timer = None
        s.is_started = False
        s.sensor.stops += 1


class Sensor:
    """Klipper's LDC1612 seen from the outside."""
    def __init__(s, world, fail_start=0):
        s.w = world
        s.data_rate = 400
        s.batch_bulk = BatchBulk(s)
        s.fail_start = fail_start
        s.starts = s.stops = 0
        s.started_rates = []

    def get_mcu(s):
        return s.w.mcu

    def get_samples_per_second(s):
        return s.data_rate

    def convert_frequency_to_raw(s, f):
        return int(f / FREQ_CONV + 0.5)

    def convert_raw_to_frequency(s, r):
        return r * FREQ_CONV

    def add_client(s, cb):
        if s.fail_start:
            s.fail_start -= 1
            # what Klipper did before ready: AttributeError inside start,
            # BatchBulkHelper left "started" without a timer
            s.batch_bulk.is_started = True
            s.batch_bulk.client_cbs.append(cb)
            raise AttributeError("'NoneType' object has no attribute 'send'")
        s.batch_bulk.add_client(cb)

    def start_cb(s):
        s.starts += 1
        s.started_rates.append(s.data_rate)

    def emit(s, rows):
        bb = s.batch_bulk
        if not bb.is_started or bb.batch_timer is None:
            return
        msg = {'data': rows, 'errors': 0, 'overflows': 0}
        for cb in list(bb.client_cbs):
            if not cb(msg):
                bb.client_cbs.remove(cb)
                if not bb.client_cbs:
                    bb._stop()


class Mcu:
    def __init__(s, world):
        s.w = world
        s.cfg_cbs = []

    def is_fileoutput(s):
        return False

    def try_lookup_command(s, fmt):
        return object()

    def register_config_callback(s, cb):
        s.cfg_cbs.append(cb)

    def get_name(s):
        return 'EBB'

    def estimated_print_time(s, t):
        return t


class World:
    """Nozzle over a bed; the Eddy sees mount flex as a frequency change."""
    def __init__(s, z_bed=0.0, k=3000., noise=4., sign=-1, ooze=0.,
                 relax=0., seed=3, drift=0., growth=0.):
        s.now = 100.0
        s.z = 5.0
        s.xy = [100., 100.]
        s.z_bed, s.k, s.noise, s.sign = z_bed, k, noise, sign
        s.ooze, s.relax, s.drift = ooze, relax, drift
        s.rnd = random.Random(seed)
        s.mcu = Mcu(s)
        s.sensor = Sensor(s)
        s.hist = [(s.now, s.z)]
        s.next_sample = s.now
        s.pressed_for = 0.
        s.mcu_check = None          # set during a probing move
        s.frame = 0.                # toolhead Z minus nozzle Z
        s.growth = growth           # nozzle growth, mm per degree over 25 C
        s.heater = None

    def tip(s):
        """How far the hot nozzle reaches below its cold length."""
        return s.growth * ((s.heater.temp if s.heater else 25.) - 25.)

    def force(s):
        zb = s.z_bed + s.tip()          # a longer nozzle touches earlier
        d = zb - s.z
        f = s.k * max(0., d)
        if s.ooze:
            dd = zb + s.ooze - s.z
            if dd > 0:
                f += 600. * min(dd, s.ooze)
        if f > 0 and s.relax:
            f *= max(0.3, 1. - s.relax * s.pressed_for)
        return f

    def hz(s):
        return 3.0e6 + s.sign * s.force() + s.drift * (s.now - 100.) + \
            s.rnd.gauss(0., s.noise)

    def _sample_until(s, t, dz_per_s=0.):
        rows = []
        dt = 1. / s.sensor.data_rate
        while s.next_sample <= t:
            tt = s.next_sample
            s.z += dz_per_s * (tt - s.now)
            s.now = tt
            if s.force() > 0:
                s.pressed_for += dt
            else:
                s.pressed_for = 0.
            s.hist.append((tt, s.z))
            hz = s.hz()
            rows.append((tt, hz, 999.9))
            s.next_sample += dt
            if s.mcu_check is not None and s.mcu_check(tt, hz):
                s.stopped = True        # the MCU stopped the steppers here
                break
        if rows:
            s.sensor.emit(rows)
        return rows

    def advance(s, t):
        s._sample_until(t)
        s.now = max(s.now, t)

    def move_z(s, z1, speed):
        z0 = s.z
        dur = abs(z1 - z0) / speed
        t_end = s.now + dur
        v = (z1 - z0) / dur if dur else 0.
        s.stopped = False
        s._sample_until(t_end, v)
        if not s.stopped:
            s.z = z1
            s.now = t_end
        s.hist.append((s.now, s.z))

    def z_at(s, t):
        h = s.hist
        lo, hi = 0, len(h) - 1
        if t <= h[0][0]:
            return h[0][1]
        if t >= h[-1][0]:
            return h[-1][1]
        while hi - lo > 1:
            m = (lo + hi) // 2
            if h[m][0] <= t:
                lo = m
            else:
                hi = m
        (t0, z0), (t1, z1) = h[lo], h[hi]
        return z0 if t1 == t0 else z0 + (z1 - z0) * (t - t0) / (t1 - t0)


# -- fake Klipper objects ----------------------------------------------------
class FakeTrigger:
    """trigger_analog.MCU_trigger_analog + the MCU rules of trigger_analog.c"""
    def __init__(s, sensor):
        s.sensor = sensor
        s.sos = None
        s.ttype, s.tval = None, 0
        s.raw_min, s.raw_max = 0, 1 << 31
        s.last_trigger = 0.

        class D:
            def get_command_queue(d):
                return None

            def add_stepper(d, st):
                pass
        s.dispatch = D()

    def get_dispatch(s):
        return s.dispatch

    def setup_sos_filter(s, sos):
        s.sos = sos

    def get_sos_filter(s):
        return s.sos

    def set_trigger(s, ttype, value):
        s.ttype, s.tval = ttype, value

    def set_raw_range(s, lo, hi):
        s.raw_min, s.raw_max = lo, hi

    def get_last_trigger_time(s):
        return s.last_trigger

    def run(s, world, z_min, speed):
        """A probing move: returns trigger time or raises like Klipper."""
        rows = [list(r) for r in s.sos.design.get_filter_sections()]
        state = [[0., 0.] for _ in rows]
        hit = {}

        def check(t, hz):
            raw = s.sensor.convert_frequency_to_raw(hz)
            if raw < s.raw_min or raw > s.raw_max:
                hit['err'] = 'RAW_RANGE'
                return True
            x = (raw + s.sos.offset) * s.sos.scale
            for i, (b0, b1, b2, _, a1, a2) in enumerate(rows):
                z1, z2 = state[i]
                y = b0 * x + z1
                state[i] = [b1 * x - a1 * y + z2, b2 * x - a2 * y]
                x = y
            if (s.ttype == 'abs_ge' and abs(x) >= s.tval) or \
                    (s.ttype == 'gt' and x > s.tval):
                hit['t'] = t
                return True
            return False
        world.mcu_check = check
        try:
            world.move_z(z_min, speed)
        finally:
            world.mcu_check = None
        if 'err' in hit:
            raise CmdError("Trigger analog error: %s" % hit['err'])
        if 't' not in hit:
            raise CmdError("No trigger on probe after full movement")
        s.last_trigger = hit['t']
        return hit['t']


class FakeSos:
    def __init__(s, mcu, cq, n):
        s.design, s.offset, s.scale = None, 0, 1.

    def set_filter_design(s, d):
        s.design = d

    def set_start_state(s, v):
        pass

    def set_offset_scale(s, offset=0, scale=1., auto_offset=False):
        s.offset, s.scale = offset, scale


class Stepper:
    def __init__(s, w):
        s.w = w

    def get_name(s):
        return 'stepper_z'

    def get_past_mcu_position(s, t):
        return s.w.z_at(t)

    def mcu_to_commanded_position(s, p):
        # like Klipper: step history in today's frame (set_position moves it)
        return p + s.w.frame


class Kin:
    def __init__(s, w):
        s.w = w

    def get_steppers(s):
        return [Stepper(s.w)]

    def calc_position(s, spos):
        return [s.w.xy[0], s.w.xy[1], spos['stepper_z']]


class Heater:
    """Nozzle heater: reaches a target at once when waited for, cools
    towards 25 C (time constant 60 s) when off."""
    max_temp = 300.

    def __init__(s, w):
        s.w = w
        s.target = 0.
        s.t_set, s.t_from = w.now, 150.
        s.fixed = 150.

    @property
    def temp(s):
        if s.target > 0 or s.fixed is not None:
            return s.fixed if s.fixed is not None else s.target
        return 25. + (s.t_from - 25.) * math.exp(-(s.w.now - s.t_set) / 60.)

    def set(s, target):
        cur = s.temp
        s.target = target
        if target > 0:
            s.fixed = target
        else:
            s.fixed = None
            s.t_set, s.t_from = s.w.now, cur

    def get_status(s, t):
        return {'temperature': s.temp, 'target': s.target}


class Heaters:
    def __init__(s, w):
        s.w = w

    def set_temperature(s, heater, temp, wait=False):
        heater.set(temp)
        if wait:
            s.w.advance(s.w.now + 2.)


class Extruder:
    def __init__(s, w):
        s.heater = Heater(w)
        w.heater = s.heater

    def get_heater(s):
        return s.heater

    def get_status(s, t):
        return s.heater.get_status(t)


Z_MAX = 290.                       # [stepper_z] position_max, like yours


class Toolhead:
    def __init__(s, w):
        s.w = w
        s.ext = Extruder(w)
        s.offset = 0.

    def get_last_move_time(s):
        return s.w.now

    def get_position(s):
        return [s.w.xy[0], s.w.xy[1], s.w.z + s.offset, 0.]

    def set_position(s, pos):
        s.offset = s.w.frame = pos[2] - s.w.z

    def manual_move(s, pos, speed):
        pos = list(pos) + [None] * (3 - len(pos))
        if pos[0] is not None:
            s.w.xy[:2] = [pos[0], pos[1]]
        if pos[2] is not None:
            # Klipper checks the end of every move against the Z range
            if not -2. <= pos[2] <= Z_MAX:
                raise CmdError("Move out of range: %.3f %.3f %.3f [0.000]"
                               % (s.w.xy[0], s.w.xy[1], pos[2]))
            s.w.move_z(pos[2] - s.offset, speed)

    def dwell(s, d):
        s.w.advance(s.w.now + d)

    def wait_moves(s):
        pass

    def get_kinematics(s):
        return Kin(s.w)

    def get_extruder(s):
        return s.ext

    def get_status(s, t):
        return {'homed_axes': 'xyz'}


class Homing:
    def __init__(s, w, th):
        s.w, s.th = w, th

    def probing_move(s, trig, pos, speed, check_movement=True):
        trig.run(s.w, pos[2] - s.th.offset, speed)
        return s.th.get_position()

    def check_probe_first_home(s, gcmd):
        return gcmd.get_command() == 'G28' and \
            gcmd.get('HOME_ATTEMPT_NUM', None) == '1'


class Gcmd:
    def __init__(s, params=None, cmd='PROBE'):
        s.params = {k.upper(): str(v) for k, v in (params or {}).items()}
        s.cmd = cmd
        s.out = []

    def get_command(s):
        return s.cmd

    def get_command_parameters(s):
        return dict(s.params)

    def get(s, n, default=None):
        return s.params.get(n, default)

    def _num(s, conv, n, default, minval=None, maxval=None, above=None,
             below=None):
        v = s.params.get(n)
        if v is None:
            return default
        v = conv(v)
        if (minval is not None and v < minval) or \
                (maxval is not None and v > maxval) or \
                (above is not None and v <= above):
            raise CmdError("bad %s" % n)
        return v

    def get_int(s, n, default=None, **k):
        return s._num(int, n, default, **k)

    def get_float(s, n, default=None, **k):
        return s._num(float, n, default, **k)

    def respond_info(s, msg, log=True):
        s.out.append(msg)

    def error(s, msg):
        return CmdError(msg)


class GcodeObj:
    def __init__(s):
        s.cmds = {}
        s.scripts = []
        s.z_offset = 0.

    def run_script_from_command(s, script):
        s.scripts.append(script)
        if script.startswith('SET_GCODE_OFFSET Z_ADJUST='):
            s.z_offset += float(script.split('=')[1].split()[0])

    def register_command(s, name, fn, desc=None):
        s.cmds[name] = fn

    def register_mux_command(s, *a, **k):
        pass

    def create_gcode_command(s, cmd, line, params):
        return Gcmd(params, cmd)

    def respond_info(s, msg, log=True):
        pass

    Coord = staticmethod(lambda t: t)


class ConfigFile:
    def __init__(s):
        s.saved = {}

    def set(s, sec, key, val):
        s.saved[(sec, key)] = val


class Template:
    def __init__(s, text):
        s.text = text

    def __bool__(s):
        return bool(s.text)

    def create_template_context(s):
        return {}

    def run_gcode_from_command(s, context=None):
        s.ran = getattr(s, 'ran', 0) + 1


class Printer:
    command_error = CmdError
    config_error = CmdError

    def __init__(s, world):
        s.w = world
        s.reactor = Reactor(world)
        s.th = Toolhead(world)
        s.handlers = {}
        s.objs = {'gcode': GcodeObj(), 'toolhead': s.th,
                  'homing': Homing(world, s.th), 'configfile': ConfigFile(),
                  'heaters': Heaters(world),
                  'webhooks': types.SimpleNamespace(
                      eps={}, register_endpoint=lambda p, cb: s.objs[
                          'webhooks'].eps.__setitem__(p, cb)),
                  'pins': types.SimpleNamespace(register_chip=lambda *a: None),
                  'gcode_macro': types.SimpleNamespace(
                      load_template=lambda c, n, d='': Template(
                          c.get(n, d)))}

    def get_reactor(s):
        return s.reactor

    def lookup_object(s, n, default=None):
        return s.objs.get(n, default)

    def lookup_objects(s, module=None):
        return [(k, v) for k, v in s.objs.items()
                if module is None or k.startswith(module)]

    def add_object(s, n, o):
        s.objs[n] = o

    def load_object(s, config, n):
        return s.objs[n]

    def register_event_handler(s, ev, cb):
        s.handlers.setdefault(ev, []).append(cb)

    def send_event(s, ev, *a):
        for cb in s.handlers.get(ev, []):
            cb(*a)

    def ready(s):
        s.send_event('klippy:ready')
        s.reactor.run_callbacks()


class Config:
    def __init__(s, printer, opts, name='piro_tap', sections=None):
        s.p, s.o, s.name = printer, dict(opts), name
        s.sections = sections or {}

    def error(s, msg):
        return CmdError(msg)

    def get_printer(s):
        return s.p

    def get_name(s):
        return s.name

    def _get(s, n, default, conv):
        if n in s.o:
            return conv(s.o[n])
        return default

    def get(s, n, default=None, note_valid=True):
        return s._get(n, default, str)

    def getint(s, n, default=None, minval=None, maxval=None,
               note_valid=True):
        return s._get(n, default, int)

    def getfloat(s, n, default=None, minval=None, maxval=None, above=None,
                 below=None, note_valid=True):
        if n not in s.o and default is None and n == 'z_offset':
            return 0.
        return s._get(n, default, float)

    def getboolean(s, n, default=None, note_valid=True):
        return s._get(n, default, lambda v: v in ('1', 'True', 'true'))

    def getchoice(s, n, choices, default=None, note_valid=True):
        v = s.o.get(n, default)
        return v if isinstance(choices, list) else choices[v]

    def getlists(s, n, default=None, **k):
        return default

    def get_prefix_sections(s, prefix):
        return [Config(s.p, o, n) for n, o in s.sections.items()
                if n.startswith(prefix)]

    def has_section(s, n):
        return n == 'stepper_z'

    def getsection(s, n):
        return Config(s.p, {'position_min': -2., 'position_max': Z_MAX})


# -- loading piro_tap as Klipper would (klippy/extras/piro_tap) ----------------
def load_package():
    if not os.path.exists(os.path.join(KL, 'klippy', 'extras', 'probe.py')):
        return None
    if 'kx.piro_tap' in sys.modules:
        return sys.modules['kx.piro_tap']
    sys.path.insert(0, os.path.join(KL, 'klippy'))
    kx = types.ModuleType('kx')
    kx.__path__ = [REPO, SHARED]        # kx.piro_tap, kx.piro_eddy
    sys.modules['kx'] = kx

    def real(name):
        spec = importlib.util.spec_from_file_location(
            'kx.' + name, os.path.join(KL, 'klippy', 'extras', name + '.py'))
        m = importlib.util.module_from_spec(spec)
        sys.modules['kx.' + name] = m
        spec.loader.exec_module(m)
        setattr(kx, name, m)
        return m
    real('manual_probe')
    real('probe')
    ldc = types.ModuleType('kx.ldc1612')
    ldc.LDC1612 = None                   # set per test (needs the world)
    ta = types.ModuleType('kx.trigger_analog')
    ta.MCU_trigger_analog = FakeTrigger
    ta.MCU_SosFilter = FakeSos
    for n, m in (('ldc1612', ldc), ('trigger_analog', ta)):
        sys.modules['kx.' + n] = m
        setattr(kx, n, m)
    import importlib as il
    return il.import_module('kx.piro_tap')


def make(opts=None, **world_kw):
    pkg = load_package()
    w = World(**world_kw)
    p = Printer(w)
    sys.modules['kx.ldc1612'].LDC1612 = lambda config: w.sensor
    o = {'trigger_hz': 40, 'speed': 3, 'data_dir': tempfile.mkdtemp(),
         'i2c_mcu': 'EBB', 'i2c_bus': 'i2c3_PB3_PB4'}
    o.update(opts or {})
    tap = pkg.PiroTap(Config(p, o))
    p.ready()
    w.z = 1.0
    return pkg, w, p, tap


def probe_once(p, tap, params=None, cmd='PROBE'):
    probe_mod = sys.modules['kx.probe']
    g = Gcmd(params, cmd)
    return probe_mod.run_single_probe(tap, g), g


def skip_without_klipper(fn):
    def wrapped():
        if load_package() is None:
            print('SKIP %s (no Klipper checkout: set KLIPPER_DIR)'
                  % fn.__name__)
            return
        fn()
    wrapped.__name__ = fn.__name__
    return wrapped


# -- tests: maths --------------------------------------------------------------
def test_filters_match_scipy_design():
    try:
        import numpy as np
        import scipy.signal as sg
    except ImportError:
        print('SKIP scipy check')
        return
    for btype, order, fc, fs in (('lowpass', 4, 25, 400),
                                 ('lowpass', 2, 25, 200),
                                 ('highpass', 1, 0.5, 100)):
        mine = np.array(filters.butter(order, fc, fs, btype))
        ref = sg.butter(order, fc, btype=btype, fs=fs, output='sos')
        _, h1 = sg.sosfreqz(mine, worN=256, fs=fs)
        _, h2 = sg.sosfreqz(ref, worN=256, fs=fs)
        assert np.max(np.abs(h1 - h2)) < 1e-9


def test_filter_rejects_bad_frequency():
    try:
        filters.butter(2, 60, 100)
        assert False
    except ValueError as e:
        assert 'half the sample rate' in str(e)


def test_hinge_fit_finds_bend():
    xs = [i * 0.001 for i in range(200)]
    ys = [(-3000. * (0.08 - x) if x < 0.08 else 0.) for x in xs]
    f = tapfit.hinge_fit(xs, ys)
    assert abs(f['b'] - 0.08) < 2e-5, f


# -- tests: the hub ------------------------------------------------------------
class HubPrinter:
    command_error = CmdError

    def __init__(s):
        s.w = World()
        s.reactor = Reactor(s.w)
        s.h = {}

    def get_reactor(s):
        return s.reactor

    def register_event_handler(s, e, cb):
        s.h.setdefault(e, []).append(cb)

    def ready(s):
        for cb in s.h.get('klippy:ready', []):
            cb()
        s.reactor.run_callbacks()


def test_hub_waits_for_ready_then_starts():
    p = HubPrinter()
    hub = LdcHub(p, p.w.sensor, 'piro_tap')
    got = []
    hub.add_client(lambda m: got.append(m) or True)
    assert p.w.sensor.starts == 0           # Klipper not ready: nothing runs
    p.ready()
    assert p.w.sensor.starts == 1 and hub.running
    assert p.w.sensor.started_rates == [100]      # idle rate
    p.w.advance(p.w.now + 0.1)
    assert got and got[-1]['rate'] == 100


def test_hub_unsticks_a_failed_start():
    p = HubPrinter()
    p.w.sensor.fail_start = 1
    hub = LdcHub(p, p.w.sensor, 'piro_tap')
    hub.add_client(lambda m: True)
    p.ready()                               # late start fails, no exception
    assert not hub.running and 'could not start' in hub.error
    bb = p.w.sensor.batch_bulk
    assert not bb.is_started                # put back to stopped
    hub.ensure()                            # next try works
    assert hub.running and bb.is_started


def test_hub_rate_follows_the_job():
    p = HubPrinter()
    hub = LdcHub(p, p.w.sensor, 'piro_tap',
                 {'idle': 100, 'pa': 400, 'probe': 200})
    hub.add_client(lambda m: True)
    p.ready()
    hub.set_mode('pa_loadcell', 'pa')
    assert p.w.sensor.data_rate == 400 and hub.rate == 400
    hub.set_mode('piro_tap', 'probe')       # probe wins over pa
    assert p.w.sensor.data_rate == 200
    hub.set_mode('piro_tap', None)
    assert p.w.sensor.data_rate == 400
    hub.set_mode('pa_loadcell', None)
    assert p.w.sensor.data_rate == 100
    assert p.w.sensor.started_rates == [100, 400, 200, 400, 100]
    # someone else reads the chip: the rate is left alone, with a note
    p.w.sensor.batch_bulk.client_cbs.append(lambda m: True)
    hub.set_mode('x', 'pa')
    assert p.w.sensor.data_rate == 100 and 'another program' in hub.note


def test_hub_counts_only_go_up():
    p = HubPrinter()
    hub = LdcHub(p, p.w.sensor, 'piro_tap')
    seen = []
    hub.add_client(lambda m: seen.append((m['errors'], m['overflows']))
                   or True)
    p.ready()
    for e, o in ((2, 0), (5, 1), (5, 1), (1, 0), (3, 0)):  # totals, then
        hub._batch({'data': [], 'errors': e, 'overflows': o}, hub.gen)
    # 2, 5, 5 are a running total; 1 is smaller = a new batch count
    assert [s[0] for s in seen] == [2, 5, 5, 6, 8], seen


# -- tests: whole taps ----------------------------------------------------------
@skip_without_klipper
def test_probe_finds_the_bed():
    pkg, w, p, tap = make()
    pos, g = probe_once(p, tap)
    assert abs(pos.bed_z - 0.0) < 0.004, pos
    assert tap.last['ok'] and tap.last['quality'] >= 60, tap.last
    assert not tap.in_session and w.sensor.data_rate == 100   # back to idle
    assert 400 in w.sensor.started_rates                     # probe_rate
    # auto sign found "down"; the status says so
    assert 'pressure_sign: down' in tap.note
    # tare before every touch, fixed pullback at 4 samples per micron
    ev = tap.store.get(tap.last['id'])['detail']['events']
    assert abs(ev['pull1'] - ev['pull0'] - 0.2 / 0.1) < 0.02, ev   # 0.1 mm/s


@skip_without_klipper
def test_probe_with_known_sign_and_bed_offset():
    pkg, w, p, tap = make({'pressure_sign': 'up'}, sign=1, z_bed=0.25)
    w.z = 1.2
    pos, g = probe_once(p, tap)
    assert abs(pos.bed_z - 0.25) < 0.004, pos


@skip_without_klipper
def test_probe_accuracy_is_tight():
    pkg, w, p, tap = make()
    zs = []
    for i in range(6):
        w.z = 0.8
        pos, g = probe_once(p, tap)
        zs.append(pos.bed_z)
    assert max(zs) - min(zs) < 0.005, zs


@skip_without_klipper
def test_ooze_is_retried_then_fails():
    # Kalico's bad_probe_strategy: retry, then fail
    pkg, w, p, tap = make({'min_quality': 80, 'bad_probe_retries': 1},
                          ooze=0.04, relax=4.)
    try:
        probe_once(p, tap)
        assert False
    except CmdError as e:
        assert 'Probing failed after 2 bad probes' in str(e), e
    assert len(tap.store.list(10)) == 2
    assert all(t['quality'] < 80 for t in tap.store.list(10))
    # ignore: the touch is used anyway
    pkg, w, p, tap = make({'min_quality': 80, 'bad_probe_strategy': 'ignore'},
                          ooze=0.04, relax=4.)
    pos, g = probe_once(p, tap)
    assert len(tap.store.list(10)) == 1 and abs(pos.bed_z) < 0.05


@skip_without_klipper
def test_circle_moves_retries_around_the_point():
    pkg, w, p, tap = make({'min_quality': 80, 'bad_probe_retries': 3,
                           'bad_probe_strategy': 'circle'},
                          ooze=0.04, relax=4.)
    try:
        probe_once(p, tap)
    except CmdError:
        pass
    xy = [(t['x'], t['y']) for t in reversed(tap.store.list(10))]
    assert len(xy) == 4 and len(set(xy)) == 4, xy     # 4 different spots
    assert all(abs(x - 100.) <= 4.1 and abs(y - 100.) <= 4.1 for x, y in xy)


@skip_without_klipper
def test_pullback_too_short_is_refused():
    # a soft bed (front edge): contact breaks late in the 0.2 mm pullback
    pkg, w, p, tap = make({'bad_probe_retries': 0, 'trigger_hz': 250},
                          k=700.)
    try:
        probe_once(p, tap)
        assert False
    except CmdError as e:
        assert 'Probing failed' in str(e), e
    codes = tap.store.list(1)[0]['codes']
    assert codes and codes[0] in ('TAP_PULLBACK_TOO_SHORT',
                                  'BREAK_CONTACT_TOO_LATE'), codes
    # a longer pullback fixes it
    pkg, w, p, tap = make({'trigger_hz': 250, 'pullback_distance': 0.6},
                          k=700.)
    pos, g = probe_once(p, tap)
    assert abs(pos.bed_z) < 0.01, pos


@skip_without_klipper
def test_first_homing_touch_starts_above_position_max():
    """klippy_35: the pullback of the first homing touch went to Z 416.5
    ("Move out of range"). Z is shifted into range for the touches."""
    pkg, w, p, tap = make(z_bed=0.3)
    w.z = 19.0
    home_z(p, tap)
    assert abs(p.th.get_position()[2] - (w.z - 0.3)) < 0.003
    taps = tap.store.list(10)
    assert len(taps) == 4 and all(t['ok'] for t in taps), taps


@skip_without_klipper
def test_force_limit_stops_the_move():
    pkg, w, p, tap = make({'trigger_hz': 120, 'force_limit_hz': 125,
                           'lowpass_order': 4, 'lowpass_hz': 10})
    try:
        probe_once(p, tap)
        assert False
    except CmdError as e:
        assert 'force_limit_hz' in str(e), e
    p.send_event('gcode:command_error')     # what Klipper's G-code does
    assert not tap.in_session and w.sensor.data_rate == 100


@skip_without_klipper
def test_no_contact_is_explained():
    pkg, w, p, tap = make(z_bed=-5.)
    try:
        probe_once(p, tap)
        assert False
    except CmdError as e:
        assert 'no contact found' in str(e), e


@skip_without_klipper
def test_noisy_eddy_refuses_to_tap():
    pkg, w, p, tap = make(noise=80.)
    try:
        probe_once(p, tap)
        assert False
    except CmdError as e:
        assert 'too noisy' in str(e), e


def home_z(p, tap, fast=5., slow=2.):
    """What Klipper's homing.py does for G28 Z with probe:z_virtual_endstop
    (_do_home_z_via_probe): session, forced start Z, attempt 1 at
    homing_speed, retract, attempt 2 at second_homing_speed."""
    th = p.th
    g1 = Gcmd({'HOME_ATTEMPT_NUM': '1', 'SAMPLES': '1',
               'PROBE_SPEED': fast}, 'G28')
    sess = tap.start_probe_session(g1)
    # Z unknown: Klipper forces position_endstop + 1.5 x travel (above
    # position_max)
    th.set_position([100, 100, 0. + 1.5 * (Z_MAX - 0.), 0])
    for attempt, speed in (('1', fast), ('2', slow)):
        g = Gcmd({'HOME_ATTEMPT_NUM': attempt, 'SAMPLES': '1',
                  'PROBE_SPEED': speed}, 'G28')
        sess.run_probe(g)
        pos = sess.pull_probed_results()[0]
        cur = th.get_position()
        cur[2] -= pos.bed_z
        th.set_position(cur)
        if attempt == '1':                  # homing_retract_dist: 5
            cur = th.get_position()
            cur[2] += 5.
            th.manual_move(cur, 10.)
    sess.end_probe_session()


@skip_without_klipper
def test_homing_four_touches_fast_then_slow():
    pkg, w, p, tap = make(z_bed=0.3)
    w.z = 3.0
    home_z(p, tap)
    # Z now says where the nozzle is above the bed
    assert abs(p.th.get_position()[2] - (w.z - 0.3)) < 0.003
    taps = tap.store.list(10)
    assert [t['speed'] for t in taps] == [5., 5., 2., 2.], taps
    assert [t['label'] for t in taps] == ['home 1'] * 2 + ['home 2'] * 2
    assert tap.thermal.ref_temp() == 150.
    assert not tap.in_session


@skip_without_klipper
def test_homing_retries_until_two_slow_touches_agree():
    pkg, w, p, tap = make({'home_tolerance': 0.0005}, z_bed=0.3, noise=6.)
    w.z = 3.0
    try:
        home_z(p, tap)
    except CmdError as e:
        assert 'agreed within' in str(e), e      # strict tolerance: may fail
    n2 = [t for t in tap.store.list(30) if t['label'] == 'home 2']
    assert len(n2) >= 2


@skip_without_klipper
def test_mesh_style_session_and_store():
    pkg, w, p, tap = make()
    sess = tap.start_probe_session(Gcmd())
    for x in (10., 50., 90.):
        w.xy[0] = x
        w.z = 0.6
        sess.run_probe(Gcmd())
    res = sess.pull_probed_results()
    sess.end_probe_session()
    assert len(res) == 3 and all(abs(r.bed_z) < 0.004 for r in res)
    eps = p.objs['webhooks'].eps
    out = {}

    class Req:
        error = CmdError

        def __init__(r, args):
            r.a = args

        def get_int(r, n, default=None):
            return int(r.a.get(n, default))

        def send(r, o):
            out['o'] = o
    eps['piro_tap/taps'](Req({}))
    taps = out['o']['taps']
    assert len(taps) == 3 and taps[-1]['x'] == 90.0
    eps['piro_tap/tap'](Req({'id': taps[-1]['id']}))
    d = out['o']['detail']
    assert len(d['t']) == len(d['hz']) > 50 and 'decompression' in d['lines']


def cmd(p, name, params=None):
    g = Gcmd(params, name)
    p.objs['gcode'].cmds[name](g)
    return g


@skip_without_klipper
def test_calibrate_threshold_is_three_times_peak_to_peak():
    pkg, w, p, tap = make({'trigger_hz': 1})
    w.z = 1.0
    g = cmd(p, 'PIRO_TAP_CALIBRATE', {'MODE': 'THRESHOLD'})
    saved = p.objs['configfile'].saved
    trig = float(saved[('piro_tap', 'trigger_hz')])
    line = [l for l in g.out if 'peak to peak' in l][0]
    p2p = float(line.split('trigger_hz = 3 x ')[1].split(' =')[0])
    assert abs(trig - round(3 * p2p, 1)) < 0.11, (trig, p2p)
    assert 5 <= trig < 60, (trig, g.out)
    assert saved[('piro_tap', 'pressure_sign')] == 'down'
    w.z = 1.0
    cmd(p, 'PIRO_TAP_CALIBRATE', {'MODE': 'QUALITY', 'SAMPLES': 6})
    assert tap.state.limits()['dwell'] > 0
    try:
        cmd(p, 'PIRO_TAP_CALIBRATE', {'MODE': 'PULLBACK'})
        assert False
    except CmdError as e:
        assert 'THRESHOLD, DRIFT or QUALITY' in str(e)


@skip_without_klipper
def test_thermal_calibration_finds_the_growth():
    # nozzle grows 0.25 um per degree
    pkg, w, p, tap = make(growth=0.00025)
    w.z = 3.0
    home_z(p, tap)
    g = cmd(p, 'PIRO_TAP_THERMAL_CALIBRATE',
            {'START': 0, 'END': 200, 'STEP': 50, 'SETTLE': 20})
    cal = tap.state.data['thermal']
    assert [pt[3] for pt in cal['points']] == [0, 50, 100, 150, 200]
    assert abs(cal['coeff'] - 0.00025) < 0.00003, (cal, g.out)
    saved = p.objs['configfile'].saved[('piro_tap', 'thermal_coeff')]
    assert abs(float(saved) - 0.00025) < 0.00003
    assert w.heater.target == 0.       # heater back to where it was
    # the calibration touches do not change the homing reference
    assert tap.thermal.ref_temp() == 150.
    assert p.objs['gcode'].z_offset == 0.


@skip_without_klipper
def test_thermal_apply_once_before_print():
    pkg, w, p, tap = make({'thermal_coeff': 0.00025})
    try:
        cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 220})
        assert False
    except CmdError as e:
        assert 'home Z first' in str(e)
    w.z = 3.0
    home_z(p, tap)                          # homed at 150 C
    gc = p.objs['gcode']
    cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 220})
    assert abs(gc.z_offset - 0.00025 * 70) < 1e-9
    cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 220})     # next print: same
    assert abs(gc.z_offset - 0.00025 * 70) < 1e-9       # not added twice
    w.heater.set(200.)
    w.z = 3.0
    home_z(p, tap)                          # re-homed at 200 C
    cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 220})
    assert abs(gc.z_offset - 0.00025 * 20) < 1e-9
    cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'RESET': 1})
    assert abs(gc.z_offset) < 1e-12


@skip_without_klipper
def test_thermal_apply_corrects_a_real_offset():
    """Home cold, print hot: with the offset the nozzle tip is at the
    commanded height again."""
    pkg, w, p, tap = make({'thermal_coeff': 0.0003}, growth=0.0003)
    w.heater.set(0.)
    w.advance(w.now + 3000.)                # cold: about 25 C
    w.z = 3.0
    home_z(p, tap)
    cold = tap.thermal.ref_temp()
    w.heater.set(230.)
    cmd(p, 'PIRO_TAP_THERMAL_APPLY', {'TEMP': 230})
    off = p.objs['gcode'].z_offset
    # tip below its cold length now: growth x (230 - cold)
    assert abs(off - 0.0003 * (230 - cold)) < 1e-9
    assert abs(off - (w.tip() - 0.0003 * (cold - 25.))) < 1e-6


@skip_without_klipper
def test_nozzle_clean_check():
    pkg, w, p, tap = make()
    g = Gcmd()
    p.objs['gcode'].cmds['NOZZLE_CLEAN_CHECK'](g)
    assert 'Nozzle clean' in g.out[-1], g.out
    pkg, w, p, tap = make(ooze=0.05, relax=5.)
    try:
        cmd(p, 'NOZZLE_CLEAN_CHECK', {'MAX_TAPS': 4})
        assert False
    except CmdError as e:
        assert 'not clean' in str(e)


@skip_without_klipper
def test_pa_loadcell_reads_piro_tap_at_pa_rate():
    pkg, w, p, tap = make()
    from pa_loadcell.feed import SampleBus
    bus = SampleBus(p, 'load_cell', 6000, 15., sensor='eddy',
                    eddy_name='piro_tap')
    try:
        bus.live(0)
        assert False
    except Exception:
        pass
    p.objs['piro_tap'] = tap
    out = bus.live(0)
    assert out.get('starting')               # bus not told ready yet
    bus.set_ready()
    bus.live(0)
    w.advance(w.now + 0.5)
    out = bus.live(0)
    assert out['samples'] and bus.main.source == 'piro_tap'
    bus.set_mode('pa')
    assert w.sensor.data_rate == 400
    bus.set_mode(None)
    assert w.sensor.data_rate == 100


if __name__ == '__main__':
    import time
    for n, fn in list(globals().items()):
        if n.startswith('test_'):
            t0 = time.time()
            fn()
            print('ok %-45s %.1fs' % (n, time.time() - t0))
