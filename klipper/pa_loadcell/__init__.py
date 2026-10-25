# Klipper extras package. Copy this folder to ~/klipper/klippy/extras/pa_loadcell/
# Config: see pa_loadcell.cfg. Runtime data: see README.md.
import logging
from dataclasses import asdict
from .errors import PAError
from .feed import SampleBus
from .recorder import ManualRecorder
from .settings import Settings
from .storage import RunStore
from .testrun import PATestRunner


class PALoadCell:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.defaults = Settings.from_config(config)
        self.bus = SampleBus(printer, config.get('load_cell', 'load_cell'),
                             config.getint('ring_size', 60000, minval=2000),
                             config.getfloat('live_idle', 15., above=1.))
        self.store = RunStore(config.get('data_dir',
                                         '~/printer_data/pa_loadcell'))
        self.runner = PATestRunner(printer, self.bus, self.store, {
            'min_temp': config.getfloat('min_temp', 200.),
            'required_z': config.getfloat('required_z', 20.),
            'z_tol': config.getfloat('z_tolerance', 0.5, above=0.),
            'timeout': config.getfloat('sample_timeout', 3., above=0.5)})
        self.rec = ManualRecorder(printer, self.bus, self.store,
                                  config.getfloat('max_record_seconds', 300.))
        gcode = printer.lookup_object('gcode')
        gcode.register_command('PA_LOADCELL_TEST', self.cmd_test,
                               desc="Load cell pressure advance sweep")
        gcode.register_command('PA_LOADCELL_DIAG', self.cmd_diag,
                               desc="Check load cell stream and timestamps")
        # Webhook endpoints do NOT wait on the G-code mutex, so cancel and
        # record work while a test is running. Moonraker exposes them as
        # printer.pa_loadcell.<name> (JSON-RPC) and /printer/pa_loadcell/<name>.
        wh = printer.lookup_object('webhooks')
        for name in ('live', 'info', 'defaults', 'runs', 'run', 'segment',
                     'tag', 'delete', 'record_start', 'record_stop', 'mark',
                     'cancel'):
            wh.register_endpoint('pa_loadcell/' + name,
                                 self._wrap(getattr(self, '_wh_' + name)))

    # -- G-code -------------------------------------------------------
    def cmd_test(self, gcmd):
        try:
            self.runner.run(gcmd, self.defaults.merged(gcmd))
        except PAError as e:
            raise gcmd.error(str(e))

    def cmd_diag(self, gcmd):
        try:
            self.bus.ensure()
            n0, t_end = self.bus.total, self.reactor.monotonic() + 3.0
            while self.reactor.monotonic() < t_end:
                self.reactor.pause(self.reactor.monotonic() + 0.1)
            n = self.bus.total - n0
            if n == 0:
                raise PAError("no samples in 3 s: load cell not streaming")
            mcu = self.printer.lookup_object('mcu')
            est = mcu.estimated_print_time(self.reactor.monotonic())
            gcmd.respond_info(
                "samples: %d in 3 s (%.0f/s), unit: %s\n"
                "newest sample is %.3f s behind the estimated print_time "
                "(expected 0.1 to 1.0 s: samples arrive in 0.5 s batches; "
                "negative or several seconds = timestamps are not usable)"
                % (n, self.bus.rate, self.bus.unit, est - self.bus.latest_t))
        except PAError as e:
            raise gcmd.error(str(e))

    # -- webhooks -----------------------------------------------------
    def _wrap(self, fn):
        def handler(req):
            try:
                req.send(fn(req) or {})
            except PAError as e:
                raise getattr(req, 'error', Exception)(str(e))
        return handler

    def _wh_live(self, req):
        out = self.bus.live(req.get_int('since', 0))
        r = self.runner
        out['test'] = {'state': r.state, 'progress': round(r.progress, 3),
                       'pa': r.current_pa, 'run': r.run_id,
                       'elapsed': round(self.reactor.monotonic() - r.started,
                                        1) if r.state != 'idle' else 0}
        out['rec'] = {'active': self.rec.active,
                      'elapsed': round(self.rec.elapsed(), 1),
                      'marks': len(self.rec.marks)}
        return out

    def _wh_info(self, req):
        out = self.bus.info() if self.bus.lc else {
            'source': '', 'available': self.bus.available()}
        out['available'] = self.bus.available()
        return out

    def _wh_defaults(self, req):
        out = asdict(self.defaults)
        out['limits'] = self.runner.limits
        return out

    def _wh_mark(self, req):
        self.rec.mark(req.get_str('label', ''))

    def _wh_runs(self, req):
        return {'runs': self.store.list_runs()}

    def _wh_run(self, req):
        return self.store.get_run(req.get_int('run'))

    def _wh_segment(self, req):
        return self.store.get_segment(req.get_int('run'), req.get_int('idx'),
                                      req.get_int('max_points', 3000))

    def _wh_tag(self, req):
        pick = req.get_str('pa_pick', '__keep__')
        notes = req.get_str('notes', '__keep__')
        name = req.get_str('name', '')
        self.store.set_tag(
            req.get_int('run'),
            None if pick == '__keep__' else (float(pick) if pick else ''),
            None if notes == '__keep__' else notes, name)

    def _wh_delete(self, req):
        if self.runner.state != 'idle' and \
                req.get_int('run') == self.runner.run_id:
            raise PAError("cannot delete the run that is being recorded")
        self.store.delete(req.get_int('run'))

    def _wh_record_start(self, req):
        if self.runner.state != 'idle':
            raise PAError("a PA test is running")
        self.rec.start(req.get_str('label', ''))

    def _wh_record_stop(self, req):
        return {'run': self.rec.stop()}

    def _wh_cancel(self, req):
        self.runner.cancel()

    def get_status(self, eventtime):
        return {'state': self.runner.state,
                'progress': round(self.runner.progress, 3),
                'recording': self.rec.active,
                'feed': self.bus.running,
                'rate': round(self.bus.rate, 1)}


def load_config(config):
    return PALoadCell(config)
