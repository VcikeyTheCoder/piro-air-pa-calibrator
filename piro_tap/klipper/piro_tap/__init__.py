# [piro_tap]: PIRO-TAP, a nozzle-contact Z probe from a BTT Eddy that senses
# nozzle pressure (the coil watches the hotend mount flex). The same Eddy can
# feed the PA calibrator (pa_loadcell, eddy: piro_tap).
#
# A Klipper probe: PROBE, PROBE_ACCURACY, BED_MESH_CALIBRATE, Z_TILT_ADJUST,
# QUAD_GANTRY_LEVEL, SCREWS_TILT_CALCULATE and G28 via probe:z_virtual_endstop
# (Klipper's own homing code). Everything is in Hz: the change of the Eddy
# frequency from the tare taken before each touch.
#
# Files:
#   __init__.py   settings, Klipper probe interface, status, web endpoints
#   touch.py      one touch (Kalico's tapping move), probe points with
#                 Kalico's retries, Z homing touches
#   trigger.py    the contact trigger on the Eddy's MCU
#   kalico_tap_analysis.py, kalico_tap_quality_classifier.py,
#   kalico_retry.py, kalico_calibration.py
#                 copied 1:1 from Kalico (GPLv3): the tap analysis (Z), the
#                 tap quality, bad_probe_strategy, TAP_PERFORMANCE
#   kalico_compat.py  what those files need from Kalico, from Klipper
#   calibrate.py  PIRO_TAP_CALIBRATE (THRESHOLD, DRIFT, TAP_PERFORMANCE)
#   thermal.py    nozzle thermal expansion: calibrate and apply
#   commands.py   the other G-code commands
#   filters.py, collector.py, tapstore.py, state.py, errors.py
#   ../piro_eddy.py (shared with pa_eddy): the Eddy sample hub
#
# Needs Klipper v0.13.0-496 or newer on the Pi and on the board the Eddy is
# wired to (LDC1612 trigger support). Wiki: "PIRO-TAP probe".
import logging
import os

try:                            # inside Klipper: klippy/extras/piro_tap
    from .. import ldc1612, probe, manual_probe, trigger_analog
    from ..piro_eddy import LdcHub, check_rate, check_one_owner
except (ImportError, ValueError):   # tests import the parts on their own
    ldc1612 = probe = manual_probe = trigger_analog = None
try:                            # Kalico's tap analysis needs numpy
    from .kalico_tap_analysis import TapAnalysisHelper
    from .kalico_tap_quality_classifier import TapQualityClassifier
    from .kalico_retry import RetryPolicyFactory
    NUMPY_ERROR = None
except ImportError as e:
    NUMPY_ERROR = str(e)
from .collector import Collector
from .commands import Commands
from .errors import TapError
from .state import State
from .tapstore import TapStore
from .thermal import Thermal
from .touch import Toucher
from .trigger import Trigger, MAX_SECTIONS, sections_needed

SIGNS = {'auto': 0, 'up': 1, 'down': -1}
DATA_DIR = '~/printer_data/piro_tap'


class Options:
    """[piro_tap] settings (some can change at run time: calibrations)."""
    def __init__(self, config):
        g, gi = config.getfloat, config.getint
        self.trigger_hz = g('trigger_hz', None, above=0.)
        self.force_limit_hz = g('force_limit_hz', 2000., above=0.)
        if self.trigger_hz and self.trigger_hz >= self.force_limit_hz:
            raise config.error("trigger_hz must be below force_limit_hz")
        self.sign = SIGNS[config.getchoice('pressure_sign',
                                           {k: k for k in SIGNS}, 'auto')]
        self.tare_time = g('tare_time', 0.1, minval=0.02, maxval=1.)
        self.max_noise = g('max_noise_hz', 0., minval=0.)
        self.lowpass_hz = g('lowpass_hz', 25., above=0.)
        self.lowpass_order = gi('lowpass_order', 2, minval=1, maxval=4)
        self.drift_hz = g('drift_filter_hz', 0., minval=0., maxval=5.)
        # Kalico's pullback move (load_cell_probe.py)
        self.pullback = g('pullback_distance', 0.2, minval=0.01, maxval=2.)
        # default: probe_rate x 0.001 mm/s (Kalico: samples per second x
        # 0.001)
        self.pullback_speed = g('pullback_speed', None, minval=0.1,
                                maxval=1.)
        self.home_tolerance = g('home_tolerance', 0.01, above=0., maxval=0.5)
        self.max_temp = g('max_probe_temp', 0., minval=0.)
        self.z_offset = g('z_offset', 0.)
        self.thermal_coeff = g('thermal_coeff', 0., minval=-0.01,
                               maxval=0.01)
        self.data_dir = config.get('data_dir', DATA_DIR)
        if sections_needed(self.lowpass_order, self.drift_hz) > MAX_SECTIONS:
            raise config.error("[%s] filter too large" % config.get_name())


class PiroTap:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.name = config.get_name()
        self.config = config
        check_one_owner(config)
        self.opts = Options(config)
        # -- the Eddy and its sample rates ------------------------------------
        self.sensor = ldc1612.LDC1612(config)
        self.mcu = self.sensor.get_mcu()
        rates = {}
        for mode, d in (('idle', 100), ('pa', 400), ('probe', 400)):
            try:
                rates[mode] = check_rate(config.getint(mode + '_rate', d))
            except ValueError as e:
                raise config.error("[%s] %s" % (self.name, e))
        self.hub = LdcHub(printer, self.sensor, self.name, rates)
        self.collector = Collector(self.hub)
        # -- Kalico's tap analysis, tap quality and bad-probe retries --------
        self.classifier = TapQualityClassifier(config)
        self.tap_helper = TapAnalysisHelper(printer, self.name,
                                            self.classifier)
        self.retry_factory = RetryPolicyFactory(config)
        # -- contact trigger on the MCU -----------------------------------
        self.mcu.register_config_callback(self._check_firmware)
        self.trig = trigger_analog.MCU_trigger_analog(self.sensor)
        cq = self.trig.get_dispatch().get_command_queue()
        self.sos = trigger_analog.MCU_SosFilter(self.mcu, cq, MAX_SECTIONS)
        self.trig.setup_sos_filter(self.sos)
        self.trigger = Trigger(self.sensor, self.trig, self.sos, self.opts)
        probe.LookupZSteppers(config, self.trig.get_dispatch().add_stepper)
        self.z_min = probe.lookup_minimum_z(config)
        # -- Klipper probe interface -----------------------------------------
        self.param_helper = probe.ProbeParameterHelper(config)
        try:
            self.cmd_helper = probe.ProbeCommandHelper(
                config, self, can_set_z_offset=True)
        except TypeError:
            self.cmd_helper = probe.ProbeCommandHelper(config, self)
        try:
            self.averaging = probe.SampleAveragingHelper(
                config, self.param_helper, self._new_session,
                nozzle_probe=True)
        except TypeError:               # Klipper before v0.13.0-800
            self.averaging = probe.SampleAveragingHelper(
                config, self.param_helper, self._new_session)
        probe.HomingViaProbeHelper(config, self.opts.z_offset)
        printer.add_object('probe', self)
        # -- parts ---------------------------------------------------------------
        data = os.path.expanduser(self.opts.data_dir)
        self.store = TapStore(data)
        self.state = State(os.path.join(data, 'piro_tap_state.json'))
        self.toucher = Toucher(self)
        self.thermal = Thermal(self, self.opts.thermal_coeff)
        self.in_session = False
        self.last = None                # last touch summary
        self.count = 0                  # touches in this session
        self.note = ''
        printer.register_event_handler('gcode:command_error', self.end_session)
        Commands(self)
        wh = printer.lookup_object('webhooks')
        for ep in ('taps', 'tap', 'thermal'):
            wh.register_endpoint('piro_tap/' + ep,
                                 self._endpoint(getattr(self, '_wh_' + ep)))

    def _check_firmware(self):
        if self.mcu.is_fileoutput():
            return
        if self.mcu.try_lookup_command(
                "ldc1612_attach_trigger_analog oid=%c trigger_analog_oid=%c"
                ) is None:
            raise self.printer.config_error(
                "[%s]: the firmware on MCU '%s' has no LDC1612 trigger "
                "support. Update Klipper and flash that board (v0.13.0-496 "
                "or newer, LDC1612 support on in 'make menuconfig')"
                % (self.name, self.mcu.get_name()))

    # -- the Eddy for pa_loadcell ----------------------------------------------
    def add_client(self, cb):
        """Eddy samples for pa_loadcell: cb(msg), msg['data'] =
        [(print_time, Hz, z), ...]"""
        self.hub.add_client(cb)

    def set_mode(self, owner, mode):
        return self.hub.set_mode(owner, mode)

    # -- Klipper probe interface -----------------------------------------------
    def get_probe_params(self, gcmd=None):
        return self.param_helper.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return 0., 0., self.opts.z_offset

    def start_probe_session(self, gcmd):
        return self.averaging.start_probe_session(gcmd)

    def _new_session(self, gcmd):
        return Session(self)

    def get_status(self, eventtime):
        st = self.cmd_helper.get_status(eventtime)
        st.update(self.hub.status())
        st.update({'trigger_hz': self.opts.trigger_hz,
                   'force_limit_hz': self.opts.force_limit_hz,
                   'in_session': self.in_session, 'session_taps': self.count,
                   'last_tap': self.last, 'note': self.note,
                   'thermal_coeff': self.thermal.coeff,
                   'home_temp': self.thermal.home_temp,
                   'thermal_applied': self.thermal.applied,
                   'is_last_tap_valid': bool(self.last and self.last['ok'])})
        return st

    # -- sessions: probe rate on, samples collected ------------------------------
    def begin_session(self):
        if not self.in_session:
            self.hub.set_mode('piro_tap', 'probe')
            self.collector.start()
            self.in_session = True
            self.count = 0
            self.toucher.warned_temp = False
        self.hub.ensure()

    def end_session(self):
        if not self.in_session:
            return
        self.in_session = False
        self.collector.stop()
        try:
            self.hub.set_mode('piro_tap', None)
        except Exception:
            logging.exception("piro_tap: rate back to idle failed")

    # -- helpers --------------------------------------------------------------
    # Kalico: LoadCellProbeConfigHelper pullback settings, PULLBACK_DISTANCE=
    # and PULLBACK_SPEED= override them for one command
    def get_pullback_distance(self, gcmd=None):
        if gcmd is None:
            return self.opts.pullback
        return gcmd.get_float('PULLBACK_DISTANCE', self.opts.pullback,
                              minval=0.01, maxval=2.)

    def get_pullback_speed(self, gcmd=None):
        speed = self.opts.pullback_speed
        if speed is None:
            speed = self.hub.rates['probe'] * 0.001
        if gcmd is None:
            return speed
        return gcmd.get_float('PULLBACK_SPEED', speed, minval=0.1, maxval=1.)

    def is_homed(self):
        th = self.printer.lookup_object('toolhead')
        return 'z' in th.get_status(self.reactor.monotonic()).get(
            'homed_axes', '')

    def check_homed(self):
        if not self.is_homed():
            raise TapError("home the printer first")

    def save_config(self, gcmd, key, value):
        self.printer.lookup_object('configfile').set(self.name, key, value)
        gcmd.respond_info("%s: %s: %s\nThe SAVE_CONFIG command will update the "
                          "printer config file with the above and restart "
                          "the printer." % (self.name, key, value))

    # -- web endpoints (Moonraker: printer.piro_tap.<name>) -------------------
    def _endpoint(self, fn):
        def handler(req):
            err = getattr(req, 'error', Exception)
            try:
                out = fn(req)
            except err:
                raise
            except Exception as e:
                logging.exception("piro_tap: endpoint failed")
                raise err("piro_tap: %s" % (e,))
            req.send(out or {})
        return handler

    def _wh_taps(self, req):
        lim = max(1, min(200, req.get_int('limit', 100)))
        return {'taps': self.store.list(lim), 'last': self.last,
                'status': self.hub.status(), 'in_session': self.in_session,
                'session_taps': self.count,
                'trigger_hz': self.opts.trigger_hz, 'note': self.note}

    def _wh_tap(self, req):
        try:
            return self.store.get(req.get_int('id'))
        except KeyError as e:
            raise req.error(str(e))

    def _wh_thermal(self, req):
        return self.thermal.status()


class Session:
    """Klipper's probe session: start / run_probe / pull / end."""
    def __init__(self, pt):
        self.pt = pt
        self.results = []
        try:
            pt.begin_session()
        except Exception:
            pt.end_session()
            raise

    def run_probe(self, gcmd):
        try:
            epos, res = self.pt.toucher.run(gcmd)
        except TapError as e:
            raise self.pt.printer.command_error("piro_tap: %s" % (e,))
        self.results.append(manual_probe.create_probe_result(
            epos, self.pt.get_offsets()))

    def pull_probed_results(self):
        res, self.results = self.results, []
        return res

    def end_probe_session(self):
        self.results = []
        self.pt.end_session()


def load_config(config):
    if probe is None:
        raise config.error("[piro_tap] must be installed in klippy/extras "
                           "(run install.sh)")
    if NUMPY_ERROR:
        raise config.error("[piro_tap] needs numpy in Klipper's Python (%s). "
                           "Run: ~/klippy-env/bin/pip install numpy"
                           % NUMPY_ERROR)
    return PiroTap(config)
