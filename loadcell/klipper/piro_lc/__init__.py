"""PIRO-LC: Z homing, paper offset and nozzle growth for Klipper's own
[load_cell_probe]. Klipper's probing (tare, MCU trigger, pullback fit) is
used as it is; nothing in Klipper is patched.

[piro_lc]
#data_dir: ~/printer_data/prio_edge/loadcell (default)

Options in the normal [load_cell_probe] section (all optional):
#homing_first_speed: 10        mm/s, touch 1 of G28 Z
#homing_second_speed: 2        mm/s, touch 2
#homing_first_samples: 2       taps for touch 1
#homing_second_samples: 2      taps for touch 2
#homing_samples_tolerance: 0.01  mm, taps of one touch must agree
#homing_samples_retries: 5
#paper_offset: 0               mm, written by PIRO_LC_PAPER_TEST
#paper_thickness: 0.08         mm, start value (the page can change it live)
#nozzle_growth: 0              mm per C, written by PIRO_LC_GROWTH_CALIBRATE
#max_paper_offset: 1.0         mm, a paper offset beyond this is refused
#max_growth_offset: 0.3        mm, a growth offset beyond this is refused

G28 Z must use the load cell: [stepper_z] endstop_pin:
probe:z_virtual_endstop and homing_retract_dist > 0 (for touch 2)."""
import logging
import time

from .errors import LCError
from .store import Store
from .homing import HomeSession
from .thermal import Growth
from .paper import Paper
from .collect import Collector
from . import paths

LC_SECTION = 'load_cell_probe'


class Options:
    def __init__(self, config):
        lcs = config.getsection(LC_SECTION)
        self.first_speed = lcs.getfloat('homing_first_speed', 10., above=0.)
        self.second_speed = lcs.getfloat('homing_second_speed', 2., above=0.)
        self.first_samples = lcs.getint('homing_first_samples', 2, minval=1,
                                        maxval=10)
        self.second_samples = lcs.getint('homing_second_samples', 2,
                                         minval=1, maxval=10)
        self.tolerance = lcs.getfloat('homing_samples_tolerance', 0.01,
                                      above=0.)
        self.retries = lcs.getint('homing_samples_retries', 5, minval=0,
                                  maxval=50)
        self.paper_offset = lcs.getfloat('paper_offset', 0., minval=-2.,
                                         maxval=2.)
        self.paper_thickness = lcs.getfloat('paper_thickness', 0.08,
                                            minval=0., maxval=1.)
        self.nozzle_growth = lcs.getfloat('nozzle_growth', 0., minval=-0.01,
                                          maxval=0.01)
        self.max_paper_offset = lcs.getfloat('max_paper_offset', 1.,
                                             above=0., maxval=2.)
        self.max_growth_offset = lcs.getfloat('max_growth_offset', 0.3,
                                              above=0., maxval=1.)
        self.data_dir = paths.resolve(config.get('data_dir', None),
                                      'loadcell')
        # read here so the checks at connect need no other module
        sz = config.getsection('stepper_z') \
            if config.has_section('stepper_z') else None
        self.z_endstop = sz.get('endstop_pin', '') if sz else ''
        self.z_retract = sz.getfloat('homing_retract_dist', 5.) if sz else 0.
        self.z_offset = lcs.getfloat('z_offset', 0.)


class PiroLC:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = LC_SECTION
        if not config.has_section(LC_SECTION):
            raise config.error("[piro_lc] needs Klipper's [load_cell_probe]")
        self.opts = Options(config)
        self.store = Store(self.opts.data_dir)
        self.paper_offset = self.opts.paper_offset
        self.home_temp = self.store.get('home_temp')
        self.home_time = self.store.get('home_time')
        self.last_home = None
        self.probe = None
        self.notes = []
        self.growth = Growth(self)
        self.paper = Paper(self)
        self.collector = Collector(self)
        self.printer.register_event_handler('klippy:connect', self._connect)
        self.printer.register_event_handler('klippy:ready', self._ready)
        gcode = self.printer.lookup_object('gcode')
        for cmd, fn, desc in (
                ('PIRO_LC_PAPER_TEST', self.paper.start,
                 "Paper test: real nozzle-on-bed height after Z homing"),
                ('PIRO_LC_PAPER_THICKNESS', self.paper.set_thickness,
                 "Set the paper thickness (also during the paper test)"),
                ('PIRO_LC_GROWTH_CALIBRATE', self.growth.calibrate,
                 "Measure nozzle growth per degree C"),
                ('PIRO_LC_GROWTH_APPLY', self.growth.apply,
                 "Predict nozzle growth to TEMP and correct Z"),
                ('PIRO_LC_CALIBRATE', self.cmd_calibrate,
                 "Calibration: home Z, paper test, then (GROWTH=1) growth"),
                ('PIRO_LC_TEST', self.collector.run,
                 "Run a probe test and save its data (PIRO-EDGE page)"),
                ('PIRO_LC_STATUS', self.cmd_status,
                 "Show the load cell homing values")):
            gcode.register_command(cmd, self._wrap(fn), desc=desc)
        wh = self.printer.lookup_object('webhooks')
        for ep, fn in (('status', self._wh_status), ('growth', self._wh_growth),
                       ('thermal', self._wh_growth),
                       ('sessions', self._wh_sessions),
                       ('session', self._wh_session),
                       ('series', self._wh_series)):
            wh.register_endpoint('piro_lc/' + ep, self._endpoint(fn))

    # -- hooks into Klipper's probe ---------------------------------------
    def _connect(self):
        probe = self.printer.lookup_object('probe', None)
        if probe is None or not hasattr(probe, '_probe_session') \
                or not hasattr(probe, '_tapping_move'):
            raise self.printer.config_error(
                "[piro_lc] works with Klipper's [load_cell_probe] only")
        self.probe = probe
        # G28 Z: our two-touch session; anything else: Klipper's own
        orig_start = probe.start_probe_session

        def start_probe_session(gcmd):
            if gcmd.get_command() == 'G28' and \
                    gcmd.get('HOME_ATTEMPT_NUM', None) is not None:
                return HomeSession(self, orig_start(gcmd))
            return orig_start(gcmd)
        probe.start_probe_session = start_probe_session
        # every single tap (for the UI tests): wrap Klipper's tap session
        sah = probe._probe_session
        orig_cb = getattr(sah, 'start_session_cb', None)
        if orig_cb is not None:
            sah.start_session_cb = lambda gcmd: TouchRecorder(
                self, orig_cb(gcmd), gcmd)
        else:
            self.notes.append("piro_lc: this Klipper has no per-tap hook, "
                              "tests record averaged results only")
        clients = getattr(probe._tapping_move, '_clients', None)
        if clients is not None and hasattr(clients, 'add_client'):
            clients.add_client(self.collector.on_tap)
        # setup checks: warnings, never errors
        o = self.opts
        if 'z_virtual_endstop' not in o.z_endstop:
            self.notes.append(
                "piro_lc: [stepper_z] endstop_pin is not "
                "probe:z_virtual_endstop, so G28 Z does not use the load cell")
        elif o.z_retract <= 0.:
            self.notes.append(
                "piro_lc: [stepper_z] homing_retract_dist is 0, so G28 Z "
                "does only touch 1. Set it to 2 for the second touch")
        if abs(o.z_offset) > 1e-9:
            self.notes.append(
                "piro_lc: [load_cell_probe] z_offset is %.4f. Set it to 0: "
                "the paper test sets the offset" % o.z_offset)
        try:
            maxv = self.printer.lookup_object('toolhead').get_status(
                self.reactor.monotonic()).get('max_z_velocity', None)
        except Exception:
            maxv = None
        if maxv is None:
            try:
                maxv = self.printer.lookup_object('configfile').get_status(
                    None)['settings']['printer'].get('max_z_velocity')
            except Exception:
                maxv = None
        if maxv and max(o.first_speed, o.second_speed) > maxv:
            self.notes.append("piro_lc: homing speed above max_z_velocity "
                              "%.1f is capped by Klipper" % maxv)

    def _ready(self):
        for n in self.notes:
            logging.warning(n)
            self.respond("!! " + n)

    # -- called by HomeSession ----------------------------------------------
    def on_z_homed(self, touches):
        t = self.temps()
        self.home_temp = t.get('nozzle')
        self.home_time = self.now()
        self.last_home = {'time': self.home_time, 'nozzle': self.home_temp,
                          'touches': [[a, [round(z, 5) for z in zs]]
                                      for a, zs in touches]}
        self.growth.on_home()
        self.store.set('home_temp', self.home_temp)
        self.store.set('home_time', self.home_time)
        self.store.set('last_home', self.last_home)

    # -- helpers ------------------------------------------------------------
    def now(self):
        return time.time()

    def respond(self, msg):
        try:
            self.printer.lookup_object('gcode').respond_info(msg)
        except Exception:
            logging.info(msg)

    def z_homed(self):
        th = self.printer.lookup_object('toolhead')
        return 'z' in th.get_status(self.reactor.monotonic())['homed_axes']

    def shift_z(self, delta):
        """Same nozzle height reads `delta` lower: commands then put the
        nozzle `delta` higher. Like SET_KINEMATIC_POSITION, keeps homing."""
        th = self.printer.lookup_object('toolhead')
        th.wait_moves()
        pos = th.get_position()
        pos[2] -= delta
        th.set_position(pos)

    def lift(self, dist):
        th = self.printer.lookup_object('toolhead')
        params = self.probe.get_probe_params()
        pos = th.get_position()
        pos[2] += dist
        th.manual_move(pos, params['lift_speed'])

    def touch(self, gcmd, n):
        """n load cell touches at the current XY with Klipper's PROBE
        session (tare, trigger, pullback). -> list of Z."""
        gcode = self.printer.lookup_object('gcode')
        sub = gcode.create_gcode_command(
            "PROBE", "PROBE", {'SAMPLES': '1',
                               'PROBE_SPEED': '%.4f' % self.opts.second_speed})
        sess = self.probe.start_probe_session(sub)
        zs = []
        try:
            for i in range(n):
                if i:
                    self.lift(1.)
                sess.run_probe(sub)
                zs.append(sess.pull_probed_results()[0].bed_z)
        finally:
            sess.end_probe_session()
        return zs

    def temps(self):
        out = {}
        now = self.reactor.monotonic()
        try:
            h = self.printer.lookup_object('toolhead').get_extruder() \
                .get_heater().get_status(now)
            out['nozzle'] = round(h['temperature'], 2)
            out['nozzle_target'] = h.get('target')
        except Exception:
            pass
        for key, names in (('bed', ['heater_bed']),
                           ('chamber', None)):
            objs = names or [n for n, o in self.printer.lookup_objects()
                             if 'chamber' in n and hasattr(o, 'get_status')]
            for n in objs:
                o = self.printer.lookup_object(n, None)
                if o is None:
                    continue
                try:
                    st = o.get_status(now)
                    if 'temperature' in st:
                        out[key] = round(st['temperature'], 2)
                        break
                except Exception:
                    continue
        return out

    def save_config(self, gcmd, key, value):
        self.printer.lookup_object('configfile').set(self.name, key, value)
        self.respond("%s: %s: %s\nThe SAVE_CONFIG command will update the "
                     "printer config file with the above and restart the "
                     "printer." % (self.name, key, value))

    # -- commands -------------------------------------------------------------
    def _wrap(self, fn):
        def handler(gcmd):
            try:
                fn(gcmd)
            except LCError as e:
                raise gcmd.error("piro_lc: %s" % (e,))
        return handler

    def cmd_calibrate(self, gcmd):
        gcode = self.printer.lookup_object('gcode')
        th = self.printer.lookup_object('toolhead')
        homed = th.get_status(self.reactor.monotonic())['homed_axes']
        gcode.run_script_from_command("G28 Z" if 'x' in homed and 'y' in homed
                                      else "G28")
        then = None
        if gcmd.get_int('GROWTH', 0, minval=0, maxval=1):
            args = " ".join("%s=%s" % (k, v) for k, v in
                            gcmd.get_command_parameters().items()
                            if k in ('START', 'END', 'STEP', 'SETTLE',
                                     'SAMPLES', 'LIFT'))

            def then():
                gcode.run_script_from_command(
                    "PIRO_LC_GROWTH_CALIBRATE " + args)
        self.paper.start(gcmd, then)

    def cmd_status(self, gcmd):
        s = self.get_status(None)
        gcmd.respond_info(
            "piro_lc: paper offset %+.4f mm (paper %.3f mm), nozzle growth "
            "%.3f um/C, last Z homing at %s C, growth offset now %+.4f mm"
            % (s['paper_offset'], s['paper_thickness'],
               s['nozzle_growth'] * 1000.,
               '-' if s['home_temp'] is None else '%.0f' % s['home_temp'],
               s['growth_offset']))

    def get_status(self, eventtime):
        return {
            'paper_offset': round(self.paper_offset, 5),
            'paper_thickness': self.paper.thickness(),
            'paper_active': self.paper.active,
            'nozzle_growth': self.opts.nozzle_growth,
            'home_temp': self.home_temp, 'home_time': self.home_time,
            'growth_offset': round(self.growth.applied, 6),
            'collecting': self.collector.on,
            'homing': {'first_speed': self.opts.first_speed,
                       'second_speed': self.opts.second_speed,
                       'first_samples': self.opts.first_samples,
                       'second_samples': self.opts.second_samples,
                       'tolerance': self.opts.tolerance},
        }

    # -- web endpoints (Moonraker: printer.piro_lc.<name>) --------------------
    def _endpoint(self, fn):
        def handler(req):
            err = getattr(req, 'error', Exception)
            try:
                out = fn(req)
            except err:
                raise
            except Exception as e:
                logging.exception("piro_lc: endpoint failed")
                raise err("piro_lc: %s" % (e,))
            req.send(out or {})
        return handler

    def _wh_status(self, req):
        s = self.get_status(None)
        s['last_home'] = self.last_home or self.store.get('last_home')
        s['paper'] = self.store.get('paper')
        s['notes'] = self.notes
        return s

    def _wh_growth(self, req):
        return self.growth.status()

    def _wh_sessions(self, req):
        kind = req.get_str('kind', None)
        lim = max(1, min(500, req.get_int('limit', 100)))
        return {'sessions': self.store.list(kind, lim)}

    def _wh_session(self, req):
        try:
            return self.store.load(req.get_int('id'))
        except KeyError as e:
            raise req.error(str(e))

    def _wh_series(self, req):
        kind = req.get_str('kind', None)
        lim = max(1, min(200, req.get_int('limit', 50)))
        return {'sessions': self.store.load_many(kind, lim)}


class TouchRecorder:
    """Klipper's single-tap session with every result handed to the
    collector (only records while a PIRO_LC_TEST runs)."""
    def __init__(self, lc, inner, gcmd):
        self.lc = lc
        self.inner = inner
        self.label = gcmd.get_command() if gcmd is not None else ''

    def run_probe(self, gcmd):
        self.inner.run_probe(gcmd)

    def pull_probed_results(self):
        res = self.inner.pull_probed_results()
        if self.lc.collector.on:
            for p in res:
                self.lc.collector.on_touch(p, self.label)
        return res

    def end_probe_session(self):
        self.inner.end_probe_session()


def load_config(config):
    return PiroLC(config)
