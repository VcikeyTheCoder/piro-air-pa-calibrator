"""Manual recording: independent of the PA test, start/stop any time."""
import logging
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
            raise PAError("Already recording")
        self.buf, self.label, self.auto_stopped = [], label, False
        self.marks = []
        self.active = True
        try:
            self.bus.subscribe(self._on)
        except PAError:
            self.active = False
            raise

    def _on(self, samples):
        """Called from the sensor's data callback: only collect here. Saving
        (file writes, analysis) happens later from the reactor, so a disk
        problem can never take the sensor callback, and Klipper, down."""
        if not self.active:
            return
        self.buf.extend(samples)
        if self.buf[-1][0] - self.buf[0][0] > self.max_seconds \
                and not self.auto_stopped:
            self.auto_stopped = True
            reactor = self.printer.get_reactor()
            if hasattr(reactor, 'register_callback'):
                reactor.register_callback(lambda e: self._auto_stop())
            else:
                self._auto_stop()

    def _auto_stop(self):
        try:
            if self.active:
                self.stop()
        except Exception:
            logging.exception("pa_loadcell: saving the recording failed")
            self.active = False

    def abort(self):
        """Klipper shut down: stop collecting, keep nothing half-written."""
        self.active = False
        try:
            self.bus.unsubscribe(self._on)
        except Exception:
            pass
        self.buf, self.marks = [], []

    def mark(self, text=''):
        if not self.active or not self.buf:
            raise PAError("Not recording")
        self.marks.append((self.buf[-1][0], text))

    def elapsed(self):
        return self.buf[-1][0] - self.buf[0][0] if self.buf else 0.0

    def stop(self):
        if not self.active:
            raise PAError("Not recording")
        self.active = False
        self.bus.unsubscribe(self._on)
        if not self.buf:
            raise PAError("No load cell samples received")
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
