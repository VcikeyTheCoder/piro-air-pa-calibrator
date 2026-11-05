"""SampleBus: the ONLY file that touches Klipper's sensor APIs.

One Channel per sensor. Each one subscribes to its Klipper object's client
feed, turns the rows into (print_time, value) pairs, keeps a ring buffer for
live viewers and fans samples out to subscribers (test runner, recorder).
Everything above this file sees the same thing from either sensor: time,
value, unit, rate and health. sensor: load_cell | eddy | both picks the
channels; with both, `primary` is the one tests are judged by and the other
is recorded alongside.

Load cell (verified against klippy/extras/load_cell.py, load_cell_probe.py):
  * LoadCell.add_client(cb): cb(msg) about every 0.5 s, msg['data'] =
    [[print_time, grams_or_None, counts, tare_counts], ...], plus cumulative
    'errors' / 'overflows'. cb must return True to stay registered.
  * [load_cell] -> object 'load_cell [name]' is the LoadCell;
    [load_cell_probe] -> object 'load_cell_probe' holds it as '_load_cell'.
  * grams is None until calibrated and tared (then raw counts are used).
BTT Eddy / LDC1612 (klippy/extras/ldc1612.py, probe_eddy_current.py):
  * add_client(cb): cb(msg) about every 0.1 s, msg['data'] =
    [(print_time, frequency_hz, z), ...] at 400 samples/s, plus 'errors' /
    'overflows'. Reached through [pa_eddy] (object 'pa_eddy') or an
    existing [probe_eddy_current NAME] (never both on one chip).
  * The value kept is sign * (Hz - zero): the change from a zero point
    (Tare, or the first sample), with the sign set so more nozzle pressure
    reads higher. Raw Hz (~3 MHz) would lose detail in the page's buffer.
Sample times are print_time on every MCU: the same clock as toolhead moves."""
import collections
import itertools
import json
import logging
import os
import time
from .errors import PAError

KINDS = ('load_cell', 'eddy')
LABEL = {'load_cell': 'Load cell', 'eddy': 'Eddy'}


class Channel:
    def __init__(self, printer, kind, name, ring_size, needed, state=None):
        self.printer, self.kind, self.name = printer, kind, name
        self.reactor = printer.get_reactor()
        self.ring = collections.deque(maxlen=ring_size)
        self.total = 0                  # samples ever stored (sequence no.)
        self.subs = []
        self.needed = needed            # () -> True while someone listens
        self.running = False
        self.gen = 0                    # invalidates a replaced callback
        self.unit_gen = 0               # bumps when the unit / zero / sign changes
        self.last_batch = 0.0
        self.latest_t = 0.0
        self.rate = 0.0
        self.unit = 'Hz' if kind == 'eddy' else 'counts'
        self.lc = None                  # the Klipper object feeding us
        self.source = ''                # its printer object name
        self.tare_name = None           # LOAD_CELL_TARE LOAD_CELL=<this>
        self.errors = self.overflows = 0     # cumulative, from the driver
        # eddy: value = sign * (Hz - zero)
        self.state = state              # dict saved by the bus (sign)
        self.sign = int((state or {}).get('sign', 1)) or 1
        self.zero = None
        self.raw = collections.deque(maxlen=200)     # recent raw Hz

    # -- finding the Klipper object ----------------------------------------
    def _find(self):
        """(printer object name, object with add_client) or (None, None)."""
        P = self.printer
        if self.kind == 'load_cell':
            for name in (self.name, 'load_cell', 'load_cell_probe'):
                obj = P.lookup_object(name, None)
                if obj is None:
                    continue
                for holder in (obj, getattr(obj, '_load_cell', None),
                               getattr(obj, 'load_cell', None)):
                    if holder is not None and hasattr(holder, 'get_sensor') \
                            and callable(getattr(holder, 'add_client', None)):
                        return name, holder
            return None, None
        names = [self.name, 'pa_eddy']
        try:
            names += [n for n, _ in P.lookup_objects('probe_eddy_current')]
        except Exception:
            pass
        for name in names:
            obj = P.lookup_object(name, None) if name else None
            if obj is not None and callable(getattr(obj, 'add_client', None)):
                return name, obj
        return None, None

    def available(self):
        return self._find()[1] is not None

    def missing_text(self):
        if self.kind == 'eddy':
            return ("No Eddy sensor found. Add [pa_eddy] (see pa_eddy.cfg) or "
                    "set eddy: to your [probe_eddy_current NAME] section")
        return ("No load cell found. Add [load_cell] or [load_cell_probe] "
                "(looked for '%s', 'load_cell', 'load_cell_probe')" % self.name)

    def ensure(self):
        now = self.reactor.monotonic()
        stale = self.running and self.last_batch and \
            now - self.last_batch > 5.0
        if self.running and not stale:
            return
        name, lc = self._find()
        if lc is None:
            raise PAError(self.missing_text())
        self.lc, self.source = lc, name
        self.tare_name = getattr(lc, 'name', None) \
            if self.kind == 'load_cell' else None
        self.gen += 1
        gen = self.gen
        lc.add_client(lambda msg: self._on_batch(msg, gen))
        self.running = True
        self.last_batch = now

    # -- data path ----------------------------------------------------
    def _convert(self, rows):
        if self.kind == 'eddy':
            out = []
            for r in rows:
                try:
                    t, hz = float(r[0]), float(r[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if hz <= 0:
                    continue              # chip error sample
                self.raw.append(hz)
                if self.zero is None:
                    self.zero = hz
                out.append((t, self.sign * (hz - self.zero)))
            return out, 'Hz'
        # grams is None until the load cell is calibrated AND tared; then
        # raw counts are all we have. Decided per batch so a later tare
        # switches the stream to grams cleanly.
        unit = 'g' if rows and all(r[1] is not None for r in rows) \
            else 'counts'
        out = []
        for r in rows:
            try:
                out.append((float(r[0]), float(r[1] if unit == 'g' else r[2])))
            except (TypeError, ValueError, IndexError):
                continue
        return out, unit

    def _on_batch(self, msg, gen):
        if gen != self.gen:
            return False                # replaced by a newer callback
        try:
            self._take(msg)
        except Exception:
            # never let an exception out of a sensor callback: Klipper
            # would shut down
            logging.exception("pa_loadcell: %s batch failed", self.kind)
        if not self.needed(self):
            self.running = False
            return False                # falsy = unregister this client
        return True                     # truthy = keep receiving

    def _take(self, msg):
        if not isinstance(msg, dict):
            return
        for k in ('errors', 'overflows'):
            try:
                setattr(self, k, int(msg.get(k) or 0))
            except (TypeError, ValueError):
                pass
        out, unit = self._convert(msg.get('data') or [])
        if out and unit != self.unit:
            self._reset(unit)
        if not out:
            return
        self.ring.extend(out)
        self.total += len(out)
        self.latest_t = out[-1][0]
        self.last_batch = self.reactor.monotonic()
        span = out[-1][0] - out[0][0]
        if len(out) > 2 and span > 0:
            self.rate = (len(out) - 1) / span
        for cb in list(self.subs):
            try:
                cb(out)
            except Exception:
                logging.exception("pa_loadcell: sample subscriber failed")

    def _reset(self, unit=None):
        """Stored values change meaning (unit, zero or sign): live viewers
        drop their buffer."""
        self.ring.clear()
        self.total = 0
        if unit:
            self.unit = unit
        self.unit_gen += 1

    # -- eddy: zero and sign -------------------------------------------
    def tare(self):
        """Eddy: today's frequency becomes 0 (no Klipper command needed)."""
        if self.kind != 'eddy':
            raise PAError("Tare of the load cell is a Klipper command")
        if not self.raw:
            raise PAError("No Eddy samples yet")
        recent = list(self.raw)[-80:]
        self.zero = sum(recent) / len(recent)
        self._reset()

    def flip_sign(self):
        """More pressure must read higher: flip, and remember it."""
        self.sign = -self.sign
        if self.state is not None:
            self.state['sign'] = self.sign
        self._reset()

    def health(self):
        return {'errors': self.errors, 'overflows': self.overflows}

    def info(self):
        lc = self.lc
        if self.kind == 'eddy':
            cal, tared = True, self.zero is not None
        else:
            cal = bool(lc and lc.is_calibrated()) if lc else False
            tared = bool(lc and lc.is_tared()) if lc else False
        stale = self.running and \
            self.reactor.monotonic() - self.last_batch > 2.0
        return {'kind': self.kind, 'label': LABEL[self.kind],
                'source': self.source, 'tare_name': self.tare_name,
                'unit': self.unit, 'calibrated': cal, 'tared': tared,
                'rate': round(self.rate, 1), 'streaming': self.running,
                'stale': bool(stale), 'sign': self.sign,
                'errors': self.errors, 'overflows': self.overflows}


class SampleBus:
    """The primary channel (what tests are judged by) plus, with sensor:
    both, a second one recorded alongside. Attributes like rate, unit,
    total ... are the primary channel's."""
    def __init__(self, printer, lc_name, ring_size, live_idle,
                 sensor='load_cell', primary=None, eddy_name='pa_eddy',
                 state_dir=None):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.live_idle = live_idle
        self.last_live = -1e9
        self.epoch = int(time.time())   # changes on Klipper restart
        self.sensor = sensor if sensor in ('load_cell', 'eddy', 'both') \
            else 'load_cell'
        kinds = ['eddy'] if self.sensor == 'eddy' else ['load_cell'] \
            if self.sensor == 'load_cell' else \
            ([primary, 'load_cell' if primary == 'eddy' else 'eddy']
             if primary in KINDS else ['load_cell', 'eddy'])
        self.state_path = os.path.join(os.path.expanduser(state_dir),
                                       'sensor_state.json') \
            if state_dir else None
        self.state = self._load_state()
        names = {'load_cell': lc_name, 'eddy': eddy_name}
        self.channels = [Channel(printer, k, names[k], ring_size,
                                 self._needed, self.state.setdefault(k, {}))
                         for k in kinds]
        self.main = self.channels[0]
        self.other = self.channels[1] if len(self.channels) > 1 else None

    # -- saved sensor state (eddy sign) ---------------------------------
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save_state(self):
        if not self.state_path:
            return
        from .fileio import atomic_json
        try:
            atomic_json(self.state_path, self.state)
        except OSError:
            logging.exception("pa_loadcell: saving sensor state failed")

    def _needed(self, ch):
        return bool(ch.subs) or \
            self.reactor.monotonic() - self.last_live < self.live_idle

    # -- the primary channel, as before ------------------------------------
    def __getattr__(self, name):
        main = self.__dict__.get('main')
        if main is None or name.startswith('__'):
            raise AttributeError(name)
        return getattr(main, name)

    def channel(self, kind):
        for ch in self.channels:
            if ch.kind == kind:
                return ch
        return None

    def available(self):
        return self.main.available()

    def ensure(self):
        self.main.ensure()
        if self.other is not None:
            try:
                self.other.ensure()
            except PAError:
                pass                     # a missing second sensor: main only

    def subscribe(self, cb, kind=None):
        ch = self.channel(kind) if kind else self.main
        if ch is None:
            raise PAError("No %s channel" % kind)
        ch.subs.append(cb)
        ch.ensure()

    def unsubscribe(self, cb, kind=None):
        for ch in ([self.channel(kind)] if kind else self.channels):
            if ch is not None and cb in ch.subs:
                ch.subs.remove(cb)

    def tare(self, kind=None):
        (self.channel(kind) if kind else self.main).tare()

    def info(self):
        out = self.main.info()
        out['sensor'] = self.sensor
        out['primary'] = self.main.kind
        if self.other is not None:
            out['other'] = self.other.info()
        return out

    def live(self, since, limit=6000, since2=0):
        """Cursor-based read: the client resumes from the last sequence it
        saw, so websocket drops and hidden tabs never lose or duplicate data.
        'reset' tells the client to drop its buffer (restart / unit flip).
        With two sensors the second comes as 'other' with its own cursor."""
        self.last_live = self.reactor.monotonic()
        self.ensure()
        out = self.info()
        out.update(self._read(self.main, since, limit))
        out['epoch'] = self.epoch
        if self.other is not None and self.other.running:
            o = self._read(self.other, since2, limit)
            o.update(unit=self.other.unit, kind=self.other.kind,
                     label=LABEL[self.other.kind], unit_gen=self.other.unit_gen)
            out['other_live'] = o
        return out

    @staticmethod
    def _read(ch, since, limit):
        reset = since > ch.total
        if reset:
            since = 0
        first = ch.total - len(ch.ring)
        start = max(since, first)
        items = list(itertools.islice(ch.ring, start - first,
                                      start - first + limit))
        return {'first': first, 'seq': start + len(items), 'reset': reset,
                'gap': 0 < since < first, 'unit_gen': ch.unit_gen,
                'samples': [[round(t, 4), round(v, 3)] for t, v in items]}
