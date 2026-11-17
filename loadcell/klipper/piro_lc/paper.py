"""Paper test: the real nozzle-on-bed height after a load cell homing.

PIRO_LC_PAPER_TEST [THICKNESS=0.08] [START_Z=1]
  Z must be homed with the load cell (G28 Z). Moves to START_Z above the
  homed zero and starts Klipper's manual probe: TESTZ Z=-0.05 ... until the
  nozzle just grips the paper, then ACCEPT (or ABORT). Mainsail and
  Fluidd show their Z adjust dialog for it.

PIRO_LC_PAPER_THICKNESS VALUE=0.08
  Change the paper thickness at any time, also while the test runs. The
  value used is the one set when ACCEPT is pressed.

Result: paper offset = (paper-touch Z - probed Z) - paper thickness.
It is kept on its own (paper_offset in [load_cell_probe], staged for
SAVE_CONFIG) and added at every Z homing, apart from the nozzle growth
offset. Z is corrected right away, no new homing needed."""
from .errors import LCError

try:
    from .. import manual_probe
except ImportError:
    manual_probe = None


class Paper:
    def __init__(self, lc):
        self.lc = lc
        self.active = False
        self.then = None            # called after ACCEPT (calibration sequence)
        self.last = None            # last result for the UI

    def thickness(self):
        return self.lc.store.get('paper_thickness',
                                 self.lc.opts.paper_thickness)

    def set_thickness(self, gcmd):
        v = gcmd.get_float('VALUE', minval=0., maxval=1.)
        self.lc.store.set('paper_thickness', v)
        gcmd.respond_info("piro_lc: paper thickness %.3f mm" % v)

    def start(self, gcmd, then=None):
        lc = self.lc
        if manual_probe is None:
            raise LCError("Klipper's manual_probe module is missing")
        if gcmd.get('THICKNESS', None) is not None:
            lc.store.set('paper_thickness',
                         gcmd.get_float('THICKNESS', minval=0., maxval=1.))
        if not lc.z_homed() or lc.home_temp is None:
            raise LCError("home Z with the load cell first (G28 Z)")
        start_z = gcmd.get_float('START_Z', 1., minval=0.2, maxval=5.)
        toolhead = lc.printer.lookup_object('toolhead')
        pos = toolhead.get_position()
        pos[2] = start_z
        toolhead.manual_move(pos, 5.)
        toolhead.wait_moves()
        self.then = then
        self.active = True
        gcmd.respond_info(
            "piro_lc: paper test. Put the paper (%.3f mm) under the nozzle. "
            "Lower with TESTZ Z=-0.05 (smaller steps near the paper) until "
            "the paper just drags, then ACCEPT." % self.thickness())
        manual_probe.ManualProbeHelper(lc.printer, gcmd, self._finalize)

    def _finalize(self, kin_pos):
        lc = self.lc
        self.active = False
        then, self.then = self.then, None
        if kin_pos is None:
            lc.respond("piro_lc: paper test aborted, paper offset unchanged")
            return
        t = self.thickness()
        delta = kin_pos[2] - t                  # Z at paper - paper = bed
        new = lc.paper_offset + delta
        if abs(new) > lc.opts.max_paper_offset:
            lc.respond("!! piro_lc: paper offset would be %+.3f mm, more than "
                       "max_paper_offset %.3f. Not used: check the paper "
                       "and the load cell homing" % (new, lc.opts.max_paper_offset))
            return
        lc.shift_z(delta)                       # Z correct right away
        lc.paper_offset = new
        self.last = {'z_paper': round(kin_pos[2], 5), 'thickness': t,
                     'offset': round(new, 5), 'time': lc.now()}
        lc.store.set('paper', self.last)
        lc.respond("piro_lc: paper touch at Z %.4f, paper %.3f mm: paper "
                   "offset %+.4f mm (was %+.4f)"
                   % (kin_pos[2], t, new, new - delta))
        lc.save_config(None, 'paper_offset', '%.5f' % new)
        if then is not None:
            then()
