"""SampleBus: the ONLY file that touches Klipper's load cell API.

It subscribes to the load cell's bulk sensor once, keeps a fixed-size ring
buffer (for live viewers) and fans samples out to subscribers (test runner,
manual recorder). The sensor is only streamed while someone needs it, so an
idle printer carries no extra load.

Verify against your Klipper version with PA_LOADCELL_DIAG before trusting
timestamps. The three assumptions below are marked ASSUMPTION."""
import collections
import itertools
import time
from .errors import PAError

# ASSUMPTION 1: a bulk-sensor client callback returns True when it wants to
# be removed (this is how Klipper's dump clients behave).
CALLBACK_DONE, CALLBACK_KEEP = True, False


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
        self.last_batch = 0.0
        self.latest_t = 0.0
        self.rate = 0.0
        self.unit = 'counts'
        self._c2g = None
        self.lc = None

    # -- sensor access ------------------------------------------------
    def _candidates(self):
        """Objects that may hold the load cell. [load_cell_probe] setups
        often have no plain [load_cell], so try the configured name, then
        'load_cell', then 'load_cell_probe' (and its .load_cell member)."""
        out = []
        for name in (self.lc_name, 'load_cell', 'load_cell_probe'):
            obj = self.printer.lookup_object(name, None)
            if obj is not None and obj not in out:
                out.append(obj)
                inner = getattr(obj, 'load_cell', None)
                if inner is not None and inner not in out:
                    out.append(inner)
        return out

    def _sensor(self):
        cands = self._candidates()
        if not cands:
            raise PAError("load cell '%s' not found: check [load_cell] / "
                          "[load_cell_probe] and the load_cell option"
                          % self.lc_name)
        for lc in cands:
            # ASSUMPTION 2: the load cell exposes its bulk sensor.
            get = getattr(lc, 'get_sensor', None)
            sensor = get() if callable(get) else getattr(lc, 'sensor', None)
            if sensor is not None and hasattr(sensor, 'add_client'):
                self.lc = lc
                # ASSUMPTION 3: counts_to_grams() exists; else raw counts.
                self._c2g = getattr(lc, 'counts_to_grams', None)
                self.unit = 'g' if self._c2g else 'counts'
                return sensor
        raise PAError("this Klipper's load_cell API is not supported (no "
                      "sensor.add_client on %s); adapt feed.py"
                      % ", ".join(type(c).__name__ for c in cands))

    def available(self):
        return bool(self._candidates())

    def ensure(self):
        now = self.reactor.monotonic()
        stale = self.running and self.last_batch and \
            now - self.last_batch > 5.0
        if self.running and not stale:
            return
        self._sensor().add_client(self._on_batch)
        self.running = True
        self.last_batch = now

    def _needed(self):
        return bool(self.subs) or \
            self.reactor.monotonic() - self.last_live < self.live_idle

    # -- data path ----------------------------------------------------
    def _convert(self, s):
        v = s[1]
        if self._c2g:
            try:
                v = self._c2g(v)
            except Exception:
                pass
        return (float(s[0]), float(v))

    def _on_batch(self, msg):
        data = msg.get('data') or []
        if data:
            out = [self._convert(s) for s in data]
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
            return CALLBACK_DONE
        return CALLBACK_KEEP

    def subscribe(self, cb):
        self.subs.append(cb)
        self.ensure()

    def unsubscribe(self, cb):
        if cb in self.subs:
            self.subs.remove(cb)

    def live(self, since, limit=6000):
        """Cursor-based read: client resumes from the last sequence it saw,
        so websocket drops and hidden tabs never lose or duplicate data."""
        self.last_live = self.reactor.monotonic()
        self.ensure()
        first = self.total - len(self.ring)
        start = max(since, first)
        items = list(itertools.islice(self.ring, start - first,
                                      start - first + limit))
        return {'epoch': self.epoch, 'first': first,
                'seq': start + len(items),
                'gap': 0 < since < first,
                'rate': round(self.rate, 1), 'unit': self.unit,
                'samples': [[round(t, 4), round(v, 3)] for t, v in items]}
