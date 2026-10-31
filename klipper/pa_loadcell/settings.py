"""Per-run settings. Defaults come from [pa_loadcell] in the config file;
every field can be overridden by the same-named (UPPERCASE) command parameter.

PIRO-EDGE test: a prime (PRIME_LENGTH mm at PRIME_FLOW mm^3/s), then an
adaptive search over PA values. Every PA value is a slow lead-in and CYCLES
fast/slow pulses, all extrude-only. Klipper's own PA is held at 0 and the
PA charge K*(fast-slow) is put in by hand over the smoothing window around
every speed-up and taken back around every slow-down ("artificial PA",
the way OznLab sensor does it, github.com/ozancs/oznlab-sensor), so no XY
motion and no max_extrude_cross_section change is needed.

The search (see testrun.py): PA 0, then PA_MAX (+UPPER_STEP until the force
dips, failing at PA_LIMIT), then values around the predicted answer.

PA_METHOD=klipper instead lets Klipper's own pressure advance and smoothing
act: the nozzle moves back and forth on a short line along MOVE_AXIS with a
constant amount of filament per mm, fast and slow come from the toolhead
speed, and the PA value is set with SET_PRESSURE_ADVANCE. No conversion is
applied to its result.

Auto PA (PA_AUTO in pa_auto.cfg, before a print): APPLY=1 sets the result as
Klipper's PA and never stops the print (a poor result, a failure or a cancel
applies FAIL_PA); REUSE_HOURS reuses a matching recent auto result instead of
testing; RESTRICT=0 runs where the nozzle is (no Z check, no moves)."""
from dataclasses import dataclass, fields
from .errors import PAError

# options from earlier versions: still read so Klipper does not reject an
# older pa_loadcell.cfg, but they no longer do anything
OBSOLETE = ('sweeps', 'extrude_len', 'slow_speed', 'fast_speed', 'settle',
            'warmup', 'method', 'pa_mode', 'pa_start', 'pa_end', 'pa_step',
            'wobble', 'wobble_axis', 'accel', 'model')
SEARCH_TYPICAL = 7          # PA values a search usually needs
SEARCH_MAX_TRAINS = 14      # hard stop for one search


@dataclass
class Settings:
    pa_max: float = 0.10          # first high PA tried (upper-limit check)
    upper_step: float = 0.02      # added while PA_MAX shows no dip
    pa_limit: float = 0.20        # no dip up to here: the test fails
    cycles: int = 6               # fast+slow pulses per PA value
    fast_flow: float = 12.0       # mm^3/s during the fast leg
    fast_time: float = 0.6        # s per fast leg
    slow_flow: float = 1.8        # mm^3/s during the slow leg
    slow_time: float = 1.0        # s per slow leg
    prime_length: float = 20.0    # mm of filament pushed before the first PA
    prime_flow: float = 12.0      # mm^3/s during the prime
    prime_rest: float = 1.0       # s pause after the prime
    rest_time: float = 0.3        # s pause before every PA value
    max_filament: float = 400.0   # mm, refuse a test that could use more
    skip_first: int = 1           # 1 = ignore the first pulse of every PA
    park_x: float = 0.0           # nozzle rests here before/after the test
    park_y: float = 0.0
    test_x: float = 0.0           # nozzle extrudes (in air) here
    test_y: float = 0.0
    filament: str = ""            # free-text name stored in run metadata
    pa_method: str = "push"       # push = test push, klipper = Klipper's PA
    move_axis: str = "Y"          # klipper method: line axis
    test_accel: float = 5000.0    # klipper method: toolhead accel, mm/s^2
    line_length: float = 0.0      # klipper method: line length, 0 = from max cross-section
    line_dir: int = 0             # klipper method: 0 = centred on the test position,
                                  # 1 / -1 = starts there, runs towards + / -
    smooth_time: float = 0.0      # 0 = the extruder's pressure_advance_smooth_time
    apply: int = 0                # 1 = set the result as Klipper's PA (auto PA)
    fail_pa: float = 0.02         # applied when an auto run gives no usable result
    reuse_hours: float = 0.0      # auto: reuse a matching result this recent
    restrict: int = 1             # 0 = run where the nozzle is, no Z check

    @classmethod
    def from_config(cls, config):
        g = config.getfloat
        for old in OBSOLETE:
            config.get(old, None)
        return cls(
            pa_max=g('pa_max', 0.10, above=0.),
            upper_step=g('upper_step', 0.02, above=0.),
            pa_limit=g('pa_limit', 0.20, above=0.),
            cycles=config.getint('cycles', 6, minval=2),
            fast_flow=g('fast_flow', 12., above=0.),
            fast_time=g('fast_time', 0.6, above=0.),
            slow_flow=g('slow_flow', 1.8, above=0.),
            slow_time=g('slow_time', 1.0, above=0.),
            prime_length=g('prime_length', 20., minval=0.),
            prime_flow=g('prime_flow', 12., above=0.),
            prime_rest=g('prime_rest', 1., minval=0.),
            rest_time=g('rest_time', 0.3, minval=0.),
            max_filament=g('max_filament', 400., above=0.),
            skip_first=config.getint('skip_first_pulse', 1, minval=0,
                                     maxval=1),
            park_x=g('park_x'), park_y=g('park_y'),
            test_x=g('test_x'), test_y=g('test_y'),
            filament=config.get('filament', '').strip().strip('"\''),
            pa_method=config.getchoice('pa_method', {'push': 'push',
                                                     'klipper': 'klipper'},
                                       'push'),
            move_axis=config.getchoice('move_axis', {'X': 'X', 'Y': 'Y',
                                                     'x': 'X', 'y': 'Y'}, 'Y'),
            test_accel=g('test_accel', 5000., above=0.),
            line_length=g('line_length', 0., minval=0.),
            line_dir=config.getint('line_dir', 0, minval=-1, maxval=1),
            smooth_time=g('smooth_time', 0., minval=0., maxval=0.1),
            fail_pa=g('fail_pa', 0.02, minval=0., maxval=2.),
            reuse_hours=g('reuse_hours', 0., minval=0.))

    def merged(self, gcmd):
        """Defaults overridden by command parameters."""
        out = {}
        for f in fields(self):
            key, cur = f.name.upper(), getattr(self, f.name)
            if isinstance(cur, float):
                out[f.name] = gcmd.get_float(key, cur)
            elif isinstance(cur, int):
                out[f.name] = gcmd.get_int(key, cur)
            else:
                out[f.name] = gcmd.get(key, cur)
        s = Settings(**out)
        s.pa_method = str(s.pa_method).strip().lower()
        s.move_axis = str(s.move_axis).strip().upper()
        s.validate()
        return s

    def validate(self):
        if not 0 < self.pa_limit <= 2.0:
            raise PAError("PA_LIMIT must be between 0 and 2")
        if not 0 < self.pa_max <= self.pa_limit:
            raise PAError("PA_MAX must be above 0 and at most PA_LIMIT (%.3f)"
                          % self.pa_limit)
        if not 0 < self.upper_step <= 0.5:
            raise PAError("UPPER_STEP must be between 0 and 0.5")
        if self.cycles < 2:
            raise PAError("CYCLES must be at least 2")
        for name in ('fast_flow', 'fast_time', 'slow_flow', 'slow_time'):
            if getattr(self, name) <= 0:
                raise PAError("%s must be > 0" % name.upper())
        if self.slow_flow >= self.fast_flow:
            raise PAError("SLOW_FLOW must be below FAST_FLOW")
        if min(self.fast_time, self.slow_time) < 0.15:
            raise PAError("FAST_TIME and SLOW_TIME must be at least 0.15 s")
        if self.prime_length < 0:
            raise PAError("PRIME_LENGTH must be >= 0")
        if self.prime_flow <= 0:
            raise PAError("PRIME_FLOW must be > 0")
        if self.prime_rest < 0 or self.rest_time < 0:
            raise PAError("PRIME_REST and REST_TIME must be >= 0")
        if self.skip_first not in (0, 1):
            raise PAError("SKIP_FIRST must be 0 or 1")
        if self.pa_method not in ('push', 'klipper'):
            raise PAError("PA_METHOD must be push or klipper")
        if self.move_axis not in ('X', 'Y'):
            raise PAError("MOVE_AXIS must be X or Y")
        if self.test_accel <= 0:
            raise PAError("TEST_ACCEL must be > 0")
        if self.line_length < 0:
            raise PAError("LINE_LENGTH must be >= 0 (0 = automatic)")
        if self.line_dir not in (-1, 0, 1):
            raise PAError("LINE_DIR must be -1, 0 or 1")
        if self.smooth_time and not 0.01 <= self.smooth_time <= 0.1:
            raise PAError("SMOOTH_TIME must be 0 (printer value) or 0.01 to 0.1")
        if self.apply not in (0, 1) or self.restrict not in (0, 1):
            raise PAError("APPLY and RESTRICT must be 0 or 1")
        if not 0 <= self.fail_pa <= 2.0:
            raise PAError("FAIL_PA must be between 0 and 2")
        if self.reuse_hours < 0:
            raise PAError("REUSE_HOURS must be >= 0")

    # -- derived amounts (area = filament cross-section in mm^2) --------
    def leg_mm(self, area):
        """(fast_mm, slow_mm): filament length of one fast / slow leg."""
        return (self.fast_flow / area * self.fast_time,
                self.slow_flow / area * self.slow_time)

    def prime_time(self, area):
        return self.prime_length * area / self.prime_flow

    def per_pa_mm(self, area):
        fast, slow = self.leg_mm(area)
        return slow + self.cycles * (fast + slow)      # lead-in + pulses

    def filament_mm(self, area, trains=SEARCH_MAX_TRAINS):
        return self.per_pa_mm(area) * trains + self.prime_length

    def per_pa_s(self):
        return self.rest_time + self.slow_time + \
            self.cycles * (self.fast_time + self.slow_time)

    def duration_s(self, area, trains=SEARCH_TYPICAL):
        return self.per_pa_s() * trains + self.prime_time(area) + \
            self.prime_rest

    # -- klipper method: the back-and-forth line ------------------------
    def line(self, area, max_ratio=None, smooth=0.04):
        """Geometry of the klipper method. max_ratio = Klipper's
        max_extrude_ratio (max_extrude_cross_section / filament area). The
        line is 85 % of that cross-section, capped at the 0.4 mm nozzle
        default (0.64 mm2) so a raised limit does not shrink the motion to
        nothing. LINE_LENGTH > 0 sets the length instead: the filament per mm
        follows from it (one fast + one slow leg = one length). -> dict"""
        cap = 0.64 / area                    # Klipper default, 0.4 mm nozzle
        r = 0.85 * min(max_ratio or cap, cap)   # filament mm per toolhead mm
        if self.line_length > 0:
            r = (self.fast_flow * self.fast_time + self.slow_flow *
                 self.slow_time) / (area * self.line_length)
        vf, vs = self.fast_flow / area / r, self.slow_flow / area / r
        h = (self.smooth_time or smooth) / 2.
        # turn around inside the slow leg: after the dip window (ends
        # h + 0.305 s after the slow-down), before the late slow level
        rev = 0.4 if self.slow_time >= 0.8 else 0.5 * self.slow_time
        rev = max(rev, min(0.5 * self.slow_time, h + 0.33))
        d_f, d_a, d_b = vf * self.fast_time, vs * rev, vs * (self.slow_time - rev)
        return {'ratio': r, 'v_fast': vf, 'v_slow': vs, 'rev': rev,
                'd_fast': d_f, 'd_after': d_a, 'd_before': d_b,
                'length': d_f + d_a + d_b,
                'cross_section': r * area}

    @staticmethod
    def charge_speed(pa, area, fast_flow, slow_flow, smooth):
        """mm/s of filament the artificial PA charge move needs at `pa`."""
        vf, vs = fast_flow / area, slow_flow / area
        return (pa * (vf - vs) + (vf + vs) * smooth / 2.) / smooth
