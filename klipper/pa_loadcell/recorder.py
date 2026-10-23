"""Manual recording: independent of the PA test, start/stop any time."""
from .errors import PAError


class ManualRecorder:
    def __init__(self, printer, bus, store, max_seconds):
        self.printer, self.bus, self.store = printer, bus, store
        self.max_seconds = max_seconds
        self.active = False
        self.buf = []
        self.label = ''
        self.auto_stopped = False
        self.marks = []

    def _pa_and_temp(self):
        th = self.printer.lookup_object('toolhead')
        now = self.printer.get_reactor().monotonic()
        st = th.get_extruder().get_status(now)
        return st.get('pressure_advance', 0.), st.get('temperature', 0.)

    def start(self, label=''):
        if self.active:
            raise PAError("already recording")
        self.buf, self.label, self.auto_stopped = [], label, False
        self.marks = []
        self.active = True
        try:
            self.bus.subscribe(self._on)
        except PAError:
            self.active = False
            raise

    def _on(self, samples):
        self.buf.extend(samples)
        if self.buf[-1][0] - self.buf[0][0] > self.max_seconds:
            self.auto_stopped = True
            self.stop()

    def mark(self, text=''):
        if not self.active or not self.buf:
            raise PAError("start recording first")
        self.marks.append((self.buf[-1][0], text))

    def elapsed(self):
        return self.buf[-1][0] - self.buf[0][0] if self.buf else 0.0

    def stop(self):
        if not self.active:
            raise PAError("not recording")
        self.active = False
        self.bus.unsubscribe(self._on)
        if not self.buf:
            raise PAError("no samples were received from the load cell")
        t0 = self.buf[0][0]
        pa, temp = self._pa_and_temp()
        marks = [{'name': 'mark', 'label': txt or 'mark #%d' % (i + 1),
                  't0': round(tm - t0, 4), 't1': round(tm - t0, 4)}
                 for i, (tm, txt) in enumerate(self.marks)]
        seg = {'pa': pa, 'unit': self.bus.unit, 'markers': marks,
               't': [round(t - t0, 4) for t, _ in self.buf],
               'f': [round(v, 3) for _, v in self.buf]}
        rid = self.store.new_run('manual', {
            'filament': self.label, 'nozzle_temp': round(temp, 1),
            'settings': {'pa_current': pa}})
        self.store.add_segment(rid, seg)
        self.store.finalize(rid, 'complete',
                            'auto-stopped at max length'
                            if self.auto_stopped else '')
        self.buf, self.marks = [], []
        return rid
