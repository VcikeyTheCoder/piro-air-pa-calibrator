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
from . import cfgfile, fileio
from .testrun import PATestRunner, extruder_info


class PALoadCell:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.defaults = Settings.from_config(config)
        data_dir = config.get('data_dir', '~/printer_data/pa_loadcell')
        # which sensor(s): load_cell, eddy or both (primary judges the test,
        # the other is recorded alongside and compared)
        sensor = config.getchoice('sensor', {k: k for k in (
            'load_cell', 'eddy', 'both')}, 'load_cell')
        primary = config.getchoice('primary', {'load_cell': 'load_cell',
                                               'eddy': 'eddy'}, 'load_cell')
        self.bus = SampleBus(printer, config.get('load_cell', 'load_cell'),
                             config.getint('ring_size', 60000, minval=2000),
                             config.getfloat('live_idle', 15., above=1.),
                             sensor=sensor, primary=primary,
                             eddy_name=config.get('eddy', 'pa_eddy'),
                             state_dir=data_dir)
        self.store = RunStore(data_dir)
        # heavy analysis in a separate process: Klipper's loop keeps running
        self.store.bg = lambda fn, args: fileio.in_background(
            self.reactor, fn, args)
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
        printer.register_event_handler('klippy:ready', self._ready)
        printer.register_event_handler('klippy:shutdown', self._shutdown)
        printer.register_event_handler('klippy:disconnect', self._shutdown)
        wh = printer.lookup_object('webhooks')
        for name in ('live', 'info', 'defaults', 'runs', 'run', 'segment',
                     'tag', 'delete', 'record_start', 'record_stop', 'mark',
                     'cancel', 'export', 'import_begin', 'import_chunk',
                     'import_end', 'reanalyze', 'config_save', 'theme_list',
                     'theme_save', 'theme_delete', 'tare', 'status'):
            wh.register_endpoint('pa_loadcell/' + name,
                                 self._wrap(getattr(self, '_wh_' + name)))

    # -- Klipper events -------------------------------------------------
    def _ready(self):
        """Runs left 'running' by a crash, power cut or restart."""
        try:
            n = self.store.mark_interrupted(
                self.runner.run_id if self.runner.state != 'idle' else None)
            if n:
                logging.info("pa_loadcell: %d interrupted run(s) marked", n)
        except Exception:
            logging.exception("pa_loadcell: checking old runs failed")

    def _shutdown(self):
        """Emergency stop / MCU error: no G-code works any more. The test
        stops at its next step and saves what it measured; Klipper's
        restart puts PA and all limits back from the config."""
        self.runner.on_shutdown()
        if self.rec.active:
            self.rec.abort()

    def busy(self):
        """'' or why heavy or conflicting work must wait."""
        if self.runner.state != 'idle':
            return "PA test running"
        if self.rec.active:
            return "Recording running"
        return ''

    # -- G-code -------------------------------------------------------
    def cmd_test(self, gcmd):
        try:
            auto = int(gcmd.get('APPLY', '0') or 0) == 1
        except ValueError:
            auto = False
        try:
            s = self.defaults.merged(gcmd)
        except Exception as e:
            if not auto:
                raise gcmd.error(str(e)) if isinstance(e, PAError) else e
            try:
                fail = float(gcmd.get('FAIL_PA', self.defaults.fail_pa))
            except ValueError:
                fail = self.defaults.fail_pa
            self.runner.fallback(gcmd, fail, str(e))
            return
        if self.rec.active:
            if s.apply:
                self.runner.fallback(gcmd, s.fail_pa, "a recording is running")
                return
            raise gcmd.error("Stop the recording first")
        if s.apply:
            self.runner.auto(gcmd, s)
            return
        try:
            self.runner.run(gcmd, s)
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
        """Every endpoint: a problem becomes an error answer for the page.
        Klipper shuts the printer down on any other exception from a
        webhook, so nothing is allowed to escape."""
        def handler(req):
            err = getattr(req, 'error', Exception)
            try:
                out = fn(req)
            except err:
                raise                       # bad request parameter
            except PAError as e:
                raise err(str(e))
            except Exception as e:
                logging.exception("pa_loadcell: endpoint %s failed",
                                  getattr(fn, '__name__', '?'))
                raise err("pa_loadcell: %s" % (e,))
            req.send(out or {})
        return handler

    def _wh_live(self, req):
        out = self.bus.live(req.get_int('since', 0),
                            min(6000, max(100, req.get_int('limit', 6000))),
                            req.get_int('since2', 0))
        r = self.runner
        out['test'] = {'state': r.state, 'progress': round(r.progress, 3),
                       'pa': r.current_pa, 'run': r.run_id,
                       'phase': r.phase, 'pa_t': r.pa_t,
                       'leg_seq': r.leg_seq, 'note': r.note,
                       'search': r.search if r.state != 'idle' else None,
                       'legs': r.legs_since(req.get_int('leg_seq', 0)),
                       'elapsed': round(self.reactor.monotonic() - r.started,
                                        1) if r.state != 'idle' else 0,
                       'values': r.values if r.state != 'idle' else 0,
                       'max_values': r.max_values if r.state != 'idle' else 0,
                       'last': r.last}
        out['printer'] = self._printer_status()
        out['rec'] = {'active': self.rec.active,
                      'elapsed': round(self.rec.elapsed(), 1),
                      'marks': len(self.rec.marks)}
        return out

    def _wh_info(self, req):
        out = self.bus.info() if self.bus.lc else {
            'source': '', 'sensor': self.bus.sensor,
            'primary': self.bus.main.kind}
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
        # an older run is re-analysed when opened, but not during a test
        return self.store.get_run(req.get_int('run'), analyse=not self.busy())

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
        pick = str(req.get('pa_pick', '__keep__')).strip()
        notes = req.get_str('notes', '__keep__')
        name = req.get_str('name', '')
        if pick not in ('__keep__', ''):
            try:
                pick = float(pick.replace(',', '.'))
            except ValueError:
                raise PAError("PA must be a number, e.g. 0.045")
            if not 0 <= pick <= 2:
                raise PAError("PA must be between 0 and 2")
        self.store.set_tag(
            req.get_int('run'), None if pick == '__keep__' else pick,
            None if notes == '__keep__' else notes, name)

    def _wh_delete(self, req):
        if self.runner.state != 'idle' and \
                req.get_int('run') == self.runner.run_id:
            raise PAError("Cannot delete a run while it is recording")
        self.store.delete(req.get_int('run'))

    def _wh_record_start(self, req):
        if self.busy():
            raise PAError(self.busy())
        self.rec.start(req.get_str('label', ''))

    def _wh_record_stop(self, req):
        return {'run': self.rec.stop()}

    def _wh_cancel(self, req):
        self.runner.cancel()

    def _wh_tare(self, req):
        """Eddy: zero the reading here. Load cell: the page sends Klipper's
        LOAD_CELL_TARE instead."""
        if self.runner.state != 'idle':
            raise PAError("PA test running")
        kind = req.get_str('sensor', '') or None
        if kind not in (None, 'load_cell', 'eddy'):
            raise PAError("sensor must be load_cell or eddy")
        self.bus.tare(kind)
        return self.bus.info()

    # -- export / import / re-analyse ------------------------------------
    def _wh_export(self, req):
        return self.store.export_run(req.get_int('run'))

    MAX_UPLOAD = 32 * 1024 * 1024   # one file
    MAX_UPLOADS = 2                 # at the same time

    def _wh_import_begin(self, req):
        now = time.time()
        for k in [k for k, u in self.uploads.items() if now - u['at'] > 600]:
            del self.uploads[k]                 # forget stale uploads
        if self.busy():
            raise PAError("%s: import after it" % self.busy())
        if len(self.uploads) >= self.MAX_UPLOADS:
            raise PAError("Another import is still running")
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
        if self.busy():
            raise PAError("%s: import after it" % self.busy())
        try:
            data = json.loads(''.join(u['parts']))
        except (ValueError, RecursionError):
            raise PAError("Not a JSON file")
        u['parts'] = []
        return {'run': self.store.import_run(data)}

    def _wh_reanalyze(self, req):
        rid = req.get_int('run')
        if self.busy():
            raise PAError("%s: re-analyse after it" % self.busy())
        skip = req.get('skip_first', None)
        mode = req.get('mode', None) or req.get('pick', None)
        if mode == 'fit':                    # older name
            mode = 'area'
        if mode not in (None, 'fall', 'area', 'edge'):
            raise PAError("mode must be fall, area or edge")
        return self.store.reanalyze(rid, None if skip is None else bool(skip),
                                    mode)

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

    def _status(self):
        """Small, cheap snapshot of the test for Mainsail and the mini
        window (Klipper status object 'pa_loadcell', pushed by Moonraker)."""
        r = self.runner
        busy = r.state != 'idle'
        return {'state': r.state,
                'progress': round(r.progress, 3),
                'phase': r.phase if busy else '',
                'pa': r.current_pa if busy else None,
                'values': r.values if busy else 0,
                'max_values': r.max_values if busy else 0,
                'note': r.note if busy else '',
                'run': r.run_id,
                'elapsed': round(self.reactor.monotonic() - r.started, 1)
                if busy else 0,
                'prediction': (r.search or {}).get('prediction') if busy
                else None,
                'last': r.last,
                'recording': self.rec.active,
                'feed': self.bus.running,
                'sensor': self.bus.sensor,
                'primary': self.bus.main.kind,
                'unit': self.bus.unit,
                'rate': round(self.bus.rate, 1)}

    def _wh_status(self, req):
        out = self._status()
        r = self.runner
        out['search'] = r.search if r.state != 'idle' else None
        out['legs'] = r.legs_since(req.get_int('leg_seq', 0))
        out['leg_seq'] = r.leg_seq
        out['pa_t'] = r.pa_t
        return out

    def get_status(self, eventtime):
        return self._status()


def load_config(config):
    return PALoadCell(config)
