# Klipper extras package. Copy this folder to ~/klipper/klippy/extras/pa_loadcell/
# Config: see pa_loadcell.cfg. Runtime data: see README.md.
import json
import logging
import time
import uuid
from dataclasses import asdict
from .errors import PAError
from .feed import SampleBus
from .recorder import ManualRecorder
from .settings import Settings
from .storage import RunStore
from .themes import ThemeStore
from . import cfgfile
from .testrun import PATestRunner, extruder_info


class PALoadCell:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.defaults = Settings.from_config(config)
        self.bus = SampleBus(printer, config.get('load_cell', 'load_cell'),
                             config.getint('ring_size', 60000, minval=2000),
                             config.getfloat('live_idle', 15., above=1.))
        data_dir = config.get('data_dir', '~/printer_data/pa_loadcell')
        self.store = RunStore(data_dir)
        self.themes = ThemeStore(data_dir)
        self.cfg_override = config.get('config_path', None)
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
        self.uploads = {}       # token -> {'parts': [...], 'size', 'at'}
        wh = printer.lookup_object('webhooks')
        for name in ('live', 'info', 'defaults', 'runs', 'run', 'segment',
                     'tag', 'delete', 'record_start', 'record_stop', 'mark',
                     'cancel', 'export', 'import_begin', 'import_chunk',
                     'import_end', 'reanalyze', 'config_save', 'theme_list',
                     'theme_save', 'theme_delete'):
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
                raise PAError("No samples in 3 s. Load cell not streaming")
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
                       'phase': r.phase, 'pa_t': r.pa_t,
                       'leg_seq': r.leg_seq, 'note': r.note,
                       'search': r.search if r.state != 'idle' else None,
                       'legs': r.legs_since(req.get_int('leg_seq', 0)),
                       'elapsed': round(self.reactor.monotonic() - r.started,
                                        1) if r.state != 'idle' else 0}
        out['printer'] = self._printer_status()
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
        th = self.printer.lookup_object('toolhead', None)
        out['extruder'] = extruder_info(th.get_extruder()) if th else None
        if th:
            st = th.get_status(self.reactor.monotonic())
            out['toolhead'] = {k: st.get(k) for k in ('max_velocity', 'max_accel')}
        return out

    def _wh_mark(self, req):
        self.rec.mark(req.get_str('label', ''))

    def _wh_runs(self, req):
        return {'runs': self.store.list_runs()}

    def _wh_run(self, req):
        return self.store.get_run(req.get_int('run'))

    def _wh_segment(self, req):
        def opt(name):
            v = req.get(name, None)
            if v is None:
                return None
            if not isinstance(v, (int, float)):
                raise req.error("%s must be a number" % name)
            return float(v)
        return self.store.get_segment(req.get_int('run'), req.get_int('idx'),
                                      max(0, req.get_int('max_points', 3000)),
                                      opt('t0'), opt('t1'))

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
            raise PAError("Cannot delete a run while it is recording")
        self.store.delete(req.get_int('run'))

    def _wh_record_start(self, req):
        if self.runner.state != 'idle':
            raise PAError("PA test running")
        self.rec.start(req.get_str('label', ''))

    def _wh_record_stop(self, req):
        return {'run': self.rec.stop()}

    def _wh_cancel(self, req):
        self.runner.cancel()

    # -- export / import / re-analyse ------------------------------------
    def _wh_export(self, req):
        return self.store.export_run(req.get_int('run'))

    MAX_UPLOAD = 64 * 1024 * 1024

    def _wh_import_begin(self, req):
        now = time.time()
        for k in [k for k, u in self.uploads.items() if now - u['at'] > 600]:
            del self.uploads[k]                 # forget stale uploads
        token = uuid.uuid4().hex
        self.uploads[token] = {'parts': [], 'size': 0, 'at': now}
        return {'token': token}

    def _wh_import_chunk(self, req):
        u = self.uploads.get(req.get_str('token'))
        if u is None:
            raise PAError("Upload expired")
        part = req.get_str('data')
        if req.get_int('seq') != len(u['parts']):
            raise PAError("Upload out of order")
        u['size'] += len(part)
        if u['size'] > self.MAX_UPLOAD:
            self.uploads.pop(req.get_str('token'), None)
            raise PAError("File too large")
        u['parts'].append(part)
        u['at'] = time.time()
        return {'received': len(u['parts'])}

    def _wh_import_end(self, req):
        u = self.uploads.pop(req.get_str('token'), None)
        if u is None:
            raise PAError("Upload expired")
        try:
            data = json.loads(''.join(u['parts']))
        except ValueError:
            raise PAError("Not a JSON file")
        return {'run': self.store.import_run(data)}

    def _wh_reanalyze(self, req):
        rid = req.get_int('run')
        if self.runner.state != 'idle' and rid == self.runner.run_id:
            raise PAError("Run still recording")
        skip = req.get('skip_first', None)
        return self.store.reanalyze(rid, None if skip is None else bool(skip))

    def _printer_status(self):
        """Nozzle temperature (current / set) and Z for the status chips."""
        try:
            th = self.printer.lookup_object('toolhead', None)
            if th is None:
                return None
            now = self.reactor.monotonic()
            st = th.get_extruder().get_status(now)
            return {'temp': round(st.get('temperature', 0.), 1),
                    'target': round(st.get('target', 0.), 1),
                    'z': round(th.get_position()[2], 2),
                    'homed': th.get_status(now).get('homed_axes', '')}
        except Exception:
            return None

    # -- settings saved to pa_loadcell.cfg --------------------------------
    def _wh_config_save(self, req):
        vals = req.get('values', None)
        if not isinstance(vals, dict) or not vals:
            raise PAError("No changes")
        if self.runner.state != 'idle':
            raise PAError("Test running")
        merged = asdict(self.defaults)
        for k, v in vals.items():
            if k not in cfgfile.EDITABLE:
                raise PAError("'%s' is read only" % k)
            cur = merged[k]
            try:
                merged[k] = (int(v) if isinstance(cur, int)
                             and not isinstance(cur, bool) else
                             float(v) if isinstance(cur, float) else str(v))
            except (TypeError, ValueError):
                raise PAError("'%s' must be a number" % k)
        new = Settings(**merged)
        new.validate()
        path = cfgfile.save(cfgfile.config_path(self.printer,
                                                self.cfg_override),
                            {cfgfile.EDITABLE[k]: getattr(new, k)
                             for k in vals})
        self.defaults = new           # used right away, no restart needed
        return {'saved': path, 'defaults': asdict(new)}

    # -- custom themes ------------------------------------------------------
    def _wh_theme_list(self, req):
        return {'themes': self.themes.list()}

    def _wh_theme_save(self, req):
        return {'theme': self.themes.save(req.get('theme', None))}

    def _wh_theme_delete(self, req):
        self.themes.delete(req.get_str('id'))

    def get_status(self, eventtime):
        return {'state': self.runner.state,
                'progress': round(self.runner.progress, 3),
                'recording': self.rec.active,
                'feed': self.bus.running,
                'rate': round(self.bus.rate, 1)}


def load_config(config):
    return PALoadCell(config)
