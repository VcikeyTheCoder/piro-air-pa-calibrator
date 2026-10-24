"""Per-run settings. Defaults come from [pa_loadcell] in the config file;
every field can be overridden by the same-named (UPPERCASE) command parameter.

Pulse defaults follow KAPAT (github.com/vzagranichnyy/KAPAT): a slow/fast
volumetric square wave, each leg paired with a tiny XY wobble so Klipper
applies pressure advance to it (Klipper skips PA on extrude-only moves)."""
import math
from dataclasses import dataclass, fields
from .errors import PAError

AXES = ('X', 'Y')
OBSOLETE = ('sweeps', 'extrude_len', 'slow_speed', 'fast_speed', 'settle')


@dataclass
class Settings:
    pa_start: float = 0.0
    pa_end: float = 0.08
    pa_step: float = 0.005
    cycles: int = 3               # fast+slow pulses per PA value
    fast_flow: float = 19.24      # mm^3/s during the fast leg
    fast_time: float = 0.25       # s per fast leg
    slow_flow: float = 1.92       # mm^3/s during the slow leg
    slow_time: float = 1.0        # s per slow leg
    warmup: float = 4.0           # first PA value: lead-in slow leg x this
    wobble: float = 0.05          # mm XY nudge per leg (0 = off, PA inactive)
    wobble_axis: str = 'Y'
    accel: float = 1000.0         # mm/s^2, toolhead (XY) only, during test
    max_filament: float = 400.0   # mm, refuse a test that would use more
    park_x: float = 0.0           # nozzle rests here before/after the test
    park_y: float = 0.0
    test_x: float = 0.0           # nozzle extrudes (in air) here
    test_y: float = 0.0
    filament: str = ""            # free-text name stored in run metadata

    @classmethod
    def from_config(cls, config):
        g = config.getfloat
        # options from the first version: read so Klipper does not reject
        # an older pa_loadcell.cfg, but they no longer do anything
        for old in OBSOLETE:
            config.get(old, None)
        return cls(
            pa_start=g('pa_start', 0., minval=0.),
            pa_end=g('pa_end', 0.08, minval=0.),
            pa_step=g('pa_step', 0.005, above=0.),
            cycles=config.getint('cycles', 3, minval=1),
            fast_flow=g('fast_flow', 19.24, above=0.),
            fast_time=g('fast_time', 0.25, above=0.),
            slow_flow=g('slow_flow', 1.92, above=0.),
            slow_time=g('slow_time', 1.0, above=0.),
            warmup=g('warmup', 4.0, minval=1.),
            wobble=g('wobble', 0.05, minval=0.),
            wobble_axis=config.get('wobble_axis', 'Y').upper(),
            accel=g('accel', 1000., above=0.),
            max_filament=g('max_filament', 400., above=0.),
            park_x=g('park_x'), park_y=g('park_y'),
            test_x=g('test_x'), test_y=g('test_y'),
            filament=config.get('filament', '').strip().strip('"\''))

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
        out['wobble_axis'] = str(out['wobble_axis']).upper()
        s = Settings(**out)
        s.validate()
        return s

    def validate(self, max_values=500):
        if self.pa_end < self.pa_start:
            raise PAError("PA_END must be >= PA_START")
        if self.pa_step <= 0:
            raise PAError("PA_STEP must be > 0")
        if len(self.pa_values()) > max_values:
            raise PAError("too many PA values (max %d): raise PA_STEP"
                          % max_values)
        if self.cycles < 1:
            raise PAError("CYCLES must be >= 1")
        for name in ('fast_flow', 'fast_time', 'slow_flow', 'slow_time'):
            if getattr(self, name) <= 0:
                raise PAError("%s must be > 0" % name.upper())
        if self.slow_flow >= self.fast_flow:
            raise PAError("SLOW_FLOW must be below FAST_FLOW")
        if self.warmup < 1:
            raise PAError("WARMUP must be >= 1")
        if self.wobble < 0:
            raise PAError("WOBBLE must be >= 0")
        if self.wobble_axis not in AXES:
            raise PAError("WOBBLE_AXIS must be X or Y")

    def pa_values(self):
        n = int(round((self.pa_end - self.pa_start) / self.pa_step)) + 1
        vals = [round(self.pa_start + i * self.pa_step, 6) for i in range(n)]
        return [v for v in vals if v <= self.pa_end + 1e-9]

    # -- derived amounts (area = filament cross-section in mm^2) --------
    def leg_mm(self, area):
        """(fast_mm, slow_mm): filament length of one fast / slow leg."""
        return (self.fast_flow / area * self.fast_time,
                self.slow_flow / area * self.slow_time)

    def warmup_legs(self):
        """The first PA value starts with this many slow legs (lead-in),
        split so no single move is much longer than a normal slow leg."""
        return max(1, int(math.ceil(self.warmup - 1e-9)))

    def filament_mm(self, area):
        fast, slow = self.leg_mm(area)
        per_pa = slow + self.cycles * (fast + slow)      # lead-in + pulses
        return per_pa * len(self.pa_values()) + (self.warmup - 1.0) * slow

    def duration_s(self):
        per_pa = self.slow_time + self.cycles * (self.fast_time
                                                 + self.slow_time)
        return per_pa * len(self.pa_values()) + \
            (self.warmup - 1.0) * self.slow_time

    def cross_section_needed(self, area):
        """mm^2 that [extruder] max_extrude_cross_section must allow: each
        leg extrudes its filament over only WOBBLE mm of XY travel."""
        if self.wobble <= 0:
            return 0.0
        fast, slow = self.leg_mm(area)
        lead = slow * self.warmup / self.warmup_legs()
        return max(fast, slow, lead) / self.wobble * area
