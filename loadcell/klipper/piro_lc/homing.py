"""Two-touch Z homing with the load cell, through Klipper's own homing.

G28 with probe:z_virtual_endstop: Klipper's homing code sets Z "homed",
asks the probe for touch 1 (HOME_ATTEMPT_NUM=1), retracts by
homing_retract_dist, asks for touch 2, then sets Z and sends its homing
events. This module only answers those two requests:

  touch 1: homing_first_samples taps at homing_first_speed
  touch 2: homing_second_samples taps at homing_second_speed

Every tap is Klipper's own load cell tap: tare, descend until the MCU
trigger, then the pullback fit (TappingMove.run_tap). The samples of one
touch must agree within homing_samples_tolerance (Klipper's
samples_tolerance logic, with retries); the median goes back to Klipper.

Klipper's first-touch workaround (load_cell_probe.py, "Homing workaround")
re-zeroes Z on the first tap of touch 1, so only that tap is sent with
HOME_ATTEMPT_NUM=1. The other taps of touch 1 run in that same Z frame.

The paper offset is added to the result Klipper uses to set Z, so after
homing Z=0 is the real nozzle-on-bed height. SET_GCODE_OFFSET can't
remove it. The nozzle temperature of every Z homing is saved."""
import logging

try:
    from .. import probe as kprobe
except ImportError:                     # tests import this file directly
    kprobe = None


def median_result(positions, how='median'):
    if kprobe is not None and hasattr(kprobe, 'calc_probe_z_average'):
        return kprobe.calc_probe_z_average(positions, how)
    zs = sorted(p.bed_z for p in positions)
    n = len(zs)
    z = zs[n // 2] if n % 2 else (zs[n // 2 - 1] + zs[n // 2]) / 2.
    return positions[0]._replace(bed_z=z)


class HomeSession:
    """Stands in for Klipper's probe session during G28 Z."""
    def __init__(self, lc, inner):
        self.lc = lc
        self.inner = inner              # Klipper's own probe session
        self.results = []
        self.zero_done = False          # Klipper's first-touch re-zero ran
        self.touches = []               # (touch, [z...]) for the log

    def _gcmd(self, attempt, speed, first_tap):
        gcode = self.lc.printer.lookup_object('gcode')
        # only the very first tap may trigger Klipper's re-zero workaround
        num = attempt if (attempt != '1' or first_tap) else '1.1'
        return gcode.create_gcode_command(
            "G28", "", {'HOME_ATTEMPT_NUM': num, 'SAMPLES': '1',
                        'PROBE_SPEED': '%.4f' % speed})

    def run_probe(self, gcmd):
        o = self.lc.opts
        attempt = str(gcmd.get('HOME_ATTEMPT_NUM', '1'))
        first = attempt == '1'
        speed = o.first_speed if first else o.second_speed
        count = o.first_samples if first else o.second_samples
        toolhead = self.lc.printer.lookup_object('toolhead')
        params = self.lc.probe.get_probe_params(gcmd)
        xy = toolhead.get_position()[:2]
        positions, retries = [], 0
        while len(positions) < count:
            sub = self._gcmd(attempt, speed, not self.zero_done)
            self.inner.run_probe(sub)
            pos = self.inner.pull_probed_results()[0]
            self.zero_done = True
            positions.append(pos)
            zs = [p.bed_z for p in positions]
            if max(zs) - min(zs) > o.tolerance:
                if retries >= o.retries:
                    raise self.lc.printer.command_error(
                        "piro_lc: Z homing touch %s: samples %s differ by "
                        "more than homing_samples_tolerance %.4f after %d "
                        "retries" % (attempt, ", ".join("%.4f" % z for z in zs),
                                     o.tolerance, retries))
                self.lc.respond("piro_lc: touch %s samples differ by %.4f, "
                                "retrying" % (attempt, max(zs) - min(zs)))
                retries += 1
                positions = []
            if len(positions) < count:
                z = toolhead.get_position()[2] + params['sample_retract_dist']
                toolhead.manual_move(xy + [z], params['lift_speed'])
        res = median_result(positions, 'median')
        self.touches.append((attempt, [p.bed_z for p in positions]))
        # the paper offset moves where Z=0 ends up (see module docstring)
        res = res._replace(bed_z=res.bed_z + self.lc.paper_offset)
        self.results.append(res)

    def pull_probed_results(self):
        res, self.results = self.results, []
        return res

    def end_probe_session(self):
        try:
            self.inner.end_probe_session()
        finally:
            if self.touches:
                self.lc.on_z_homed(self.touches)
