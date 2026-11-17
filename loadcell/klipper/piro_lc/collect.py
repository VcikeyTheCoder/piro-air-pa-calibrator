"""Probe data for the PIRO-EDGE page, collected only for tests the page
starts.

PIRO_LC_TEST KIND=<kind> [...]
  accuracy  PROBE_ACCURACY at the current XY   [SAMPLES=10]
  homing    G28 Z, then one probe tap, repeated [COUNT=5]
            (the tap after each homing is what is saved: its Z against the
            fresh homing is the homing repeatability. The homing taps
            themselves are not saved: Klipper measures touch 1 and touch 2
            in different Z frames)
  mesh      BED_MESH_CALIBRATE (full bed)       [REPEAT=1]
  ztilt     Z_TILT_ADJUST
  growth    PIRO_LC_GROWTH_CALIBRATE            [START END STEP SETTLE SAMPLES]

The page sends these. Recording is on only while PIRO_LC_TEST runs: the
same commands from a print or a macro are not recorded. Every load cell
touch of the test is saved with time, XY, Z, the temperatures and the
peak force, as one session file."""
import logging
import math

from .errors import LCError

KINDS = {
    'accuracy': "PROBE_ACCURACY SAMPLES=%(SAMPLES)d",
    'homing': None,                 # handled below (G28 Z, COUNT times)
    'mesh': "BED_MESH_CALIBRATE",
    'ztilt': "Z_TILT_ADJUST",
    'growth': None,                 # PIRO_LC_GROWTH_CALIBRATE with params
}


def summary(kind, touches):
    zs = [t['z'] for t in touches]
    if not zs:
        return {}
    n = len(zs)
    m = sum(zs) / n
    sd = math.sqrt(sum((z - m) ** 2 for z in zs) / n)
    out = {'n': n, 'mean': round(m, 5), 'range': round(max(zs) - min(zs), 5),
           'std': round(sd, 5)}
    temps = [t['nozzle'] for t in touches if t.get('nozzle') is not None]
    if temps:
        out['nozzle'] = round(sum(temps) / len(temps), 1)
    return out


class Collector:
    def __init__(self, lc):
        self.lc = lc
        self.session = None
        self.last_tap = None
        self.paused = False             # not recording (homing taps)

    @property
    def on(self):
        return self.session is not None

    # -- hooks (called for every touch while a session runs) --------------
    def on_tap(self, msg):
        """Klipper's tap broadcast: force curve of the last tap."""
        try:
            if self.session is not None:
                tap = msg.get('tap') or {}
                f = tap.get('force') or []
                self.last_tap = {'peak': round(max(f), 1) if f else None,
                                 'n': len(f)}
        except Exception:
            logging.exception("piro_lc: tap hook")
        return True                     # stay registered

    def on_touch(self, pos, label):
        if self.session is None or self.paused:
            self.last_tap = None
            return
        try:
            temps = self.lc.temps()
            rec = {'t': round(self.lc.now(), 3), 'x': round(pos.bed_x, 3),
                   'y': round(pos.bed_y, 3), 'z': round(pos.bed_z, 5),
                   'label': label}
            rec.update(temps)
            if self.last_tap:
                rec['force'] = self.last_tap.get('peak')
            self.last_tap = None
            self.session['touches'].append(rec)
        except Exception:
            logging.exception("piro_lc: record touch")

    # -- PIRO_LC_TEST -----------------------------------------------------
    def run(self, gcmd):
        lc = self.lc
        kind = gcmd.get('KIND').lower()
        if kind not in KINDS:
            raise LCError("KIND must be one of %s" % ", ".join(KINDS))
        if self.session is not None:
            raise LCError("a test is already running")
        params = {k: v for k, v in gcmd.get_command_parameters().items()
                  if k != 'KIND'}
        sess = {'id': lc.store.new_id(), 'kind': kind, 'start': lc.now(),
                'params': params, 'touches': [], 'ok': False,
                'temps_start': lc.temps(), 'home_temp': lc.home_temp,
                'paper_offset': lc.paper_offset,
                'nozzle_growth': lc.opts.nozzle_growth}
        gcode = lc.printer.lookup_object('gcode')
        self.session = sess
        try:
            if kind == 'homing':
                count = gcmd.get_int('COUNT', 5, minval=1, maxval=50)
                for i in range(count):
                    self.paused = True
                    try:
                        gcode.run_script_from_command("G28 Z")
                    finally:
                        self.paused = False
                    gcode.run_script_from_command("PROBE SAMPLES=1")
            elif kind == 'growth':
                args = " ".join("%s=%s" % (k, v) for k, v in params.items()
                                if k in ('START', 'END', 'STEP', 'SETTLE',
                                         'SAMPLES', 'LIFT'))
                gcode.run_script_from_command(
                    "PIRO_LC_GROWTH_CALIBRATE " + args)
                sess['growth'] = lc.store.get('growth')
            elif kind == 'mesh':
                rep = gcmd.get_int('REPEAT', 1, minval=1, maxval=10)
                for i in range(rep):
                    gcode.run_script_from_command(KINDS[kind])
            else:
                fmt = {'SAMPLES': gcmd.get_int('SAMPLES', 10, minval=2,
                                               maxval=100)}
                gcode.run_script_from_command(KINDS[kind] % fmt)
            sess['ok'] = True
        except Exception as e:
            sess['error'] = str(e)
            raise
        finally:
            self.session = None
            self.last_tap = None
            self.paused = False
            sess['end'] = lc.now()
            sess['n'] = len(sess['touches'])
            sess['summary'] = summary(kind, sess['touches'])
            lc.store.save_session(sess)
            gcmd.respond_info("piro_lc: %s test saved (%d touches)"
                              % (kind, sess['n']))
