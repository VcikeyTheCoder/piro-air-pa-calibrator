"""Collector: the Eddy samples (print_time, Hz) of the current probe
session, the last 30 s, read by the touches and the calibrations."""
import bisect


class Collector:
    def __init__(self, hub, seconds=30.):
        self.hub = hub
        self.t = []
        self.hz = []
        self.active = False
        self.key = None
        self.seconds = seconds
        self.latest = 0.

    def start(self):
        if self.active:
            return
        self.t, self.hz = [], []
        self.latest = 0.
        self.active = True
        self.key = self.hub.add_client(self._on)

    def stop(self):
        self.active = False
        if self.key is not None:
            self.hub.remove_client(self.key)
            self.key = None

    def _on(self, msg):
        if not self.active:
            return False
        for r in msg.get('data') or []:
            try:
                t, hz = float(r[0]), float(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            if hz > 0 and t > self.latest:     # 0 Hz = a chip error sample
                self.t.append(t)
                self.hz.append(hz)
                self.latest = t
        if self.t and self.t[-1] - self.t[0] > self.seconds + 5.:
            k = bisect.bisect_left(self.t, self.t[-1] - self.seconds)
            del self.t[:k], self.hz[:k]
        return True

    def window(self, t0, t1):
        """[(t, Hz), ...] with t0 <= t <= t1."""
        a = bisect.bisect_left(self.t, t0)
        b = bisect.bisect_right(self.t, t1)
        return list(zip(self.t[a:b], self.hz[a:b]))
