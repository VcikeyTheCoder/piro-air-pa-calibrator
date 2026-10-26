"""SampleBus: the ONLY file that touches Klipper's load cell API.

It subscribes to the LoadCell object's client feed (the same feed that
LOAD_CELL tools use), keeps a fixed-size ring buffer for live viewers and
fans samples out to subscribers (test runner, manual recorder).

Verified against Klipper's klippy/extras/load_cell.py and load_cell_probe.py:
  * LoadCell.add_client(cb): cb(msg) is called about every 0.5 s with
    msg['data'] = [[print_time, grams_or_None, counts, tare_counts], ...].
    cb must return a TRUE value to stay registered; a falsy value removes it.
  * [load_cell]       -> printer object 'load_cell [name]' IS the LoadCell.
  * [load_cell_probe] -> printer object 'load_cell_probe' is the probe and
                         holds the LoadCell as its private attribute
                         '_load_cell'.
  * Sample times are print_time, the same clock as toolhead move times.
  * Rows are [time, grams_or_None, counts, tare_counts]; grams is None until
    the load cell is calibrated and tared (then we fall back to counts)."""
import collections
import itertools
import time
from .errors import PAError


class SampleBus:
    def __init__(self, printer, lc_name, ring_size, live_idle):
        self.printer, self.lc_name = printer, lc_name
        self.reactor = printer.get_reactor()
        self.ring = collections.deque(maxlen=ring_size)
        self.total = 0                  # samples ever stored (sequence no.)
        self.epoch = int(time.time())   # changes on Klipper restart
        self.subs = []
        self.live_idle = live_idle
        self.last_live = -1e9
        self.running = False
        self.gen = 0                    # invalidates a replaced callback
        self.unit_gen = 0               # bumps when grams <-> counts flips
        self.last_batch = 0.0
        self.latest_t = 0.0
        self.rate = 0.0
        self.unit = 'counts'
        self.lc = None
        self.source = ''                # printer object the data comes from
        self.tare_name = None           # value for LOAD_CELL_TARE LOAD_CELL=

    # -- load cell lookup ---------------------------------------------
    def _find(self):
        """Return (printer_object_name, LoadCell) or (None, None).
        Works for [load_cell] and [load_cell_probe]; whichever exists."""
        for name in (self.lc_name, 'load_cell', 'load_cell_probe'):
            obj = self.printer.lookup_object(name, None)
            if obj is None:
                continue
            for holder in (obj, getattr(obj, '_load_cell', None),
                           getattr(obj, 'load_cell', None)):
                if holder is not None and hasattr(holder, 'get_sensor') \
                        and callable(getattr(holder, 'add_client', None)):
                    return name, holder
        return None, None

    def available(self):
        return self._find()[1] is not None

    def ensure(self):
        now = self.reactor.monotonic()
        stale = self.running and self.last_batch and \
            now - self.last_batch > 5.0
        if self.running and not stale:
            return
        name, lc = self._find()
        if lc is None:
            raise PAError("No load cell found. Add [load_cell] or "
                          "[load_cell_probe] (looked for '%s', 'load_cell', "
                          "'load_cell_probe')" % self.lc_name)
        self.lc, self.source = lc, name
        self.tare_name = getattr(lc, 'name', None)
        self.gen += 1
        gen = self.gen
        lc.add_client(lambda msg: self._on_batch(msg, gen))
        self.running = True
        self.last_batch = now

    def _needed(self):
        return bool(self.subs) or \
            self.reactor.monotonic() - self.last_live < self.live_idle

    # -- data path ----------------------------------------------------
    def _on_batch(self, msg, gen):
        if gen != self.gen:
            return False                # replaced by a newer callback
        rows = msg.get('data') or []
        # grams is None until the load cell is calibrated AND tared; then
        # raw counts are all we have. Decide per batch so a later tare
        # switches the stream to grams cleanly.
        unit = 'g' if rows and all(r[1] is not None for r in rows) \
            else 'counts'
        if rows and unit != self.unit:
            self.ring.clear()
            self.total = 0
            self.unit = unit
            self.unit_gen += 1
        out = [(float(r[0]), float(r[1] if unit == 'g' else r[2]))
               for r in rows]
        if out:
            self.ring.extend(out)
            self.total += len(out)
            self.latest_t = out[-1][0]
            self.last_batch = self.reactor.monotonic()
            span = out[-1][0] - out[0][0]
            if len(out) > 2 and span > 0:
                self.rate = (len(out) - 1) / span
            for cb in list(self.subs):
                cb(out)
        if not self._needed():
            self.running = False
            return False                # falsy = unregister this client
        return True                     # truthy = keep receiving

    def subscribe(self, cb):
        self.subs.append(cb)
        self.ensure()

    def unsubscribe(self, cb):
        if cb in self.subs:
            self.subs.remove(cb)

    def info(self):
        lc = self.lc
        cal = bool(lc and lc.is_calibrated()) if lc else False
        tared = bool(lc and lc.is_tared()) if lc else False
        stale = self.running and \
            self.reactor.monotonic() - self.last_batch > 2.0
        return {'source': self.source, 'tare_name': self.tare_name,
                'unit': self.unit, 'calibrated': cal, 'tared': tared,
                'rate': round(self.rate, 1), 'streaming': self.running,
                'stale': bool(stale)}

    def live(self, since, limit=6000):
        """Cursor-based read: client resumes from the last sequence it saw,
        so websocket drops and hidden tabs never lose or duplicate data.
        'reset' tells the client to drop its buffer (restart / unit flip)."""
        self.last_live = self.reactor.monotonic()
        self.ensure()
        reset = since > self.total
        if reset:
            since = 0
        first = self.total - len(self.ring)
        start = max(since, first)
        items = list(itertools.islice(self.ring, start - first,
                                      start - first + limit))
        out = self.info()
        out.update({'epoch': self.epoch, 'first': first,
                    'seq': start + len(items), 'reset': reset,
                    'gap': 0 < since < first, 'unit_gen': self.unit_gen,
                    'samples': [[round(t, 4), round(v, 3)]
                                for t, v in items]})
        return out
