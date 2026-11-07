"""piro_eddy: the BTT Eddy (LDC1612) code shared by PIRO-TAP ([piro_tap]) and
the PA calibrator's [pa_eddy]. Not a config section of its own.

LdcHub: the one reader of an LDC1612 for this project.

Klipper's LDC1612 driver streams while at least one client is registered.
Everything here (PA tests, the live graph, probing) goes through one hub
client, which gives three things the bare driver does not:

* It never starts the chip before Klipper is ready. A request that comes in
  earlier (the web page polls while Klipper starts) is remembered, and the
  stream starts from a reactor callback once klippy:ready has run (the same
  fix Kalico made in commit 5a16e1f). Starting earlier crashed inside the
  driver (its I2C commands do not exist yet) and left it stuck: "started",
  with no timer, never streaming again.
* Sample rate per job. Klipper fixes the chip at 400 samples/s. The rate can
  only change while the stream is stopped, so the hub stops it, sets the new
  rate and starts it again (the method OznLab's PA Sensei uses). Jobs ask for
  a mode, the highest one wins: probe > pa > idle.
* Error and overflow counts that only go up, whether the driver reports them
  as running totals (Klipper) or per batch (some forks).

The hub never lets an exception out of a sensor callback (Klipper would shut
down)."""
import logging

MODES = ('idle', 'pa', 'probe')         # lowest to highest priority
RATE_MIN, RATE_MAX = 50, 1000


def check_rate(value, what='sample rate'):
    try:
        r = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number" % what)
    if not RATE_MIN <= r <= RATE_MAX:
        raise ValueError("%s must be %d to %d samples/s"
                         % (what, RATE_MIN, RATE_MAX))
    return r


class LdcHub:
    def __init__(self, printer, sensor, name, rates=None):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.sensor = sensor
        self.name = name
        r = dict(idle=100, pa=400, probe=200)
        r.update(rates or {})
        self.rates = {m: check_rate(r[m], '%s rate' % m) for m in MODES}
        self.subs = {}                  # key -> cb(msg)
        self.modes = {}                 # owner -> mode
        self.ready = False
        self.dead = False
        self.running = False
        self.gen = 0
        self.client = None              # our callback inside the driver
        self.rate = self._driver_rate()
        self.can_switch = hasattr(sensor, 'data_rate')
        self.error = ''                 # why the last start failed
        self.note = ''                  # why the rate is not the asked one
        self.errors = self.overflows = 0
        self._raw = {'errors': 0, 'overflows': 0}
        self.last_batch = 0.
        self.batches = 0
        printer.register_event_handler('klippy:ready', self._ready)
        printer.register_event_handler('klippy:shutdown', self._shutdown)

    # -- Klipper events ------------------------------------------------------
    def _ready(self):
        self.ready = True
        # start from the reactor, after every ready handler has run
        self.reactor.register_callback(self._late_start)

    def _late_start(self, eventtime=None):
        if not self.subs or self.running:
            return
        try:
            self._start()
        except Exception as e:
            # the subscriber sees the error in status(); never raise here
            logging.warning("piro_tap: %s did not start: %s", self.name, e)

    def _shutdown(self):
        self.dead = True
        self.running = False

    # -- clients ------------------------------------------------------------
    def add_client(self, cb):
        """Klipper-style client: cb(msg) is called with each batch and stays
        registered while it returns something truthy. Before Klipper is ready
        it only waits; the stream starts once ready."""
        key = object()

        def call(msg):
            if not cb(msg):
                self.subs.pop(key, None)
        self.subs[key] = call
        if self.ready:
            self.ensure()
        return key

    def remove_client(self, key):
        self.subs.pop(key, None)

    def ensure(self):
        """Start (or restart a stalled) stream. Raises command_error with a
        clear reason if the chip cannot start."""
        if self.dead:
            raise self.printer.command_error("Klipper is shut down")
        if not self.ready:
            return False
        if self.running and not self._stalled():
            return True
        self._start()
        return True

    def _stalled(self):
        bb = getattr(self.sensor, 'batch_bulk', None)
        if bb is not None and not getattr(bb, 'is_started', True):
            return True                 # the driver stopped on its own
        return self.last_batch and \
            self.reactor.monotonic() - self.last_batch > 5.0

    def _unstick(self):
        """A start that failed with anything but command_error leaves
        Klipper's BatchBulkHelper 'started' with no timer: it would never
        stream again. Put it back to stopped."""
        bb = getattr(self.sensor, 'batch_bulk', None)
        if bb is None:
            return
        if getattr(bb, 'is_started', False) and \
                getattr(bb, 'batch_timer', 1) is None:
            logging.warning("piro_tap: %s stream was stuck, resetting",
                            self.name)
            bb.is_started = False
            try:
                del bb.client_cbs[:]
            except (AttributeError, TypeError):
                pass

    def _start(self):
        self._unstick()
        self._set_rate(self.wanted_rate())
        self.gen += 1
        gen = self.gen
        self.client = lambda msg: self._batch(msg, gen)
        self._raw = {'errors': 0, 'overflows': 0}   # the driver starts at 0
        try:
            self.sensor.add_client(self.client)
        except Exception as e:
            self.running = False
            self._unstick()
            self.error = "%s could not start: %s" % (self.name, e)
            raise self.printer.command_error(self.error)
        self.running = True
        self.error = ''
        self.last_batch = self.reactor.monotonic()

    # -- data ---------------------------------------------------------------
    def _count(self, msg):
        for k in ('errors', 'overflows'):
            try:
                v = int(msg.get(k) or 0)
            except (TypeError, ValueError):
                continue
            prev = self._raw[k]
            # a running total only grows; a smaller number is a new batch
            # count (or the driver restarted)
            setattr(self, k, getattr(self, k) + (v - prev if v >= prev else v))
            self._raw[k] = v

    def _batch(self, msg, gen):
        if gen != self.gen:
            return False                # replaced by a newer start
        try:
            self.last_batch = self.reactor.monotonic()
            self.batches += 1
            if isinstance(msg, dict):
                self._count(msg)
                out = dict(msg, errors=self.errors, overflows=self.overflows,
                           rate=self.rate)
                for call in list(self.subs.values()):
                    try:
                        call(out)
                    except Exception:
                        logging.exception("piro_tap: Eddy subscriber failed")
        except Exception:
            logging.exception("piro_tap: Eddy batch failed")
        if not self.subs:
            self.running = False
            return False                # nobody listens: the stream stops
        return True

    # -- sample rate ----------------------------------------------------------
    def _driver_rate(self):
        try:
            return float(self.sensor.get_samples_per_second())
        except Exception:
            return 400.

    def wanted_rate(self):
        best = 'idle'
        for m in self.modes.values():
            if MODES.index(m) > MODES.index(best):
                best = m
        return self.rates[best]

    def mode(self):
        best = 'idle'
        for m in self.modes.values():
            if MODES.index(m) > MODES.index(best):
                best = m
        return best

    def set_mode(self, owner, mode):
        """owner asks for mode ('pa', 'probe') or None when it is done.
        Returns the rate now in use. Call from G-code context only (a rate
        change waits for the chip)."""
        if mode is None:
            self.modes.pop(owner, None)
        elif mode in MODES:
            self.modes[owner] = mode
        else:
            raise ValueError("unknown mode %r" % (mode,))
        self.apply_rate()
        return self.rate

    def set_rates(self, **rates):
        for m, v in rates.items():
            if m not in MODES:
                raise ValueError("unknown mode %r" % (m,))
            self.rates[m] = check_rate(v, '%s rate' % m)
        self.apply_rate()

    def _set_rate(self, rate):
        if not self.can_switch:
            self.note = "this Klipper's LDC1612 driver has a fixed rate"
            self.rate = self._driver_rate()
            return
        s = self.sensor
        s.data_rate = rate
        try:
            # its clock tracking smooths over ~0.2 s worth of samples
            from . import bulk_sensor   # klippy/extras/bulk_sensor.py
            s.ffreader.clock_sync = bulk_sensor.ClockSyncRegression(
                s.mcu, rate * 0.100 * 2)
        except Exception:
            pass                        # older driver: keeps its own
        self.rate = float(rate)

    def apply_rate(self):
        want = self.wanted_rate()
        if not self.can_switch:
            self.rate = self._driver_rate()
            return False
        if float(want) == self.rate:
            self.note = ''
            return True
        if not self.running:
            self._set_rate(want)
            self.note = ''
            return True
        bb = getattr(self.sensor, 'batch_bulk', None)
        cbs = list(getattr(bb, 'client_cbs', None) or [])
        if bb is None or not hasattr(bb, '_stop') or \
                any(c is not self.client for c in cbs):
            # someone else (e.g. a Moonraker dump) reads the chip too
            self.note = ("rate stays %d: another program is reading %s"
                         % (self.rate, self.name))
            return False
        self.gen += 1                   # the old callback goes quiet
        try:
            bb._stop()
        except Exception:
            logging.exception("piro_tap: stopping %s failed", self.name)
        self.running = False
        self._set_rate(want)
        self.note = ''
        if self.subs:
            self._start()
        return True

    # -- status --------------------------------------------------------------
    def status(self):
        return {'name': self.name, 'ready': self.ready,
                'streaming': self.running, 'rate': self.rate,
                'mode': self.mode(), 'rates': dict(self.rates),
                'error': self.error, 'note': self.note,
                'errors': self.errors, 'overflows': self.overflows}


# -- one Eddy chip, one Klipper section ---------------------------------
LDC1612_ADDR = 0x2a
OWNERS = ('pa_eddy', 'piro_tap', 'probe_eddy_current')


def chip_id(config):
    """(mcu, bus or software pins, address) of an I2C section."""
    sw = config.get('i2c_software_scl_pin', None, note_valid=False)
    return (config.get('i2c_mcu', 'mcu', note_valid=False),
            sw or config.get('i2c_bus', None, note_valid=False),
            config.getint('i2c_address', LDC1612_ADDR, minval=0, maxval=127,
                          note_valid=False))


def check_one_owner(config):
    me = config.get_name()
    mine = chip_id(config)
    for prefix in OWNERS:
        for other in config.get_prefix_sections(prefix):
            name = other.get_name()
            if name == me or name.split()[0] not in OWNERS:
                continue
            if chip_id(other) == mine:
                raise config.error(
                    "[%s] and [%s] use the same Eddy chip. Keep one of them "
                    "([piro_tap] does probing and PA; [pa_eddy] only PA)"
                    % (me, name))
