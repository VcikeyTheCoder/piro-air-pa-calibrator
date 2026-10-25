"""Per-run settings. Defaults come from [pa_loadcell] in the config file;
every field can be overridden by the same-named (UPPERCASE) command parameter."""
from dataclasses import dataclass, fields
from .errors import PAError


@dataclass
class Settings:
    pa_start: float = 0.0
    pa_end: float = 0.08
    pa_step: float = 0.002
    sweeps: int = 5               # slow+fast sweeps per PA value
    extrude_len: float = 10.0     # mm of filament per slow / fast phase
    slow_speed: float = 2.0       # mm/s filament
    fast_speed: float = 20.0      # mm/s filament
    accel: float = 3000.0         # mm/s^2 (toolhead + extruder-only accel)
    settle: float = 0.5           # s dwell after every phase
    park_x: float = 0.0           # nozzle rests here before/after the test
    park_y: float = 0.0
    test_x: float = 0.0           # nozzle extrudes (in air) here
    test_y: float = 0.0
    filament: str = ""            # free-text name stored in run metadata

    @classmethod
    def from_config(cls, config):
        g = config.getfloat
        return cls(
            pa_start=g('pa_start', 0., minval=0.),
            pa_end=g('pa_end', 0.08, minval=0.),
            pa_step=g('pa_step', 0.002, above=0.),
            sweeps=config.getint('sweeps', 5, minval=1),
            extrude_len=g('extrude_len', 10., above=0.),
            slow_speed=g('slow_speed', 2., above=0.),
            fast_speed=g('fast_speed', 20., above=0.),
            accel=g('accel', 3000., above=0.),
            settle=g('settle', 0.5, minval=0.),
            park_x=g('park_x'), park_y=g('park_y'),
            test_x=g('test_x'), test_y=g('test_y'),
            filament=config.get('filament', ''))

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
        s.validate()
        return s

    def validate(self, max_values=500):
        if self.pa_end < self.pa_start:
            raise PAError("PA_END must be >= PA_START")
        if len(self.pa_values()) > max_values:
            raise PAError("too many PA values (max %d): raise PA_STEP"
                          % max_values)
        if self.sweeps < 1:
            raise PAError("SWEEPS must be >= 1")
        if self.slow_speed > self.fast_speed:
            raise PAError("SLOW_SPEED must be <= FAST_SPEED")
        if self.extrude_len <= 0:
            raise PAError("EXTRUDE_LEN must be > 0")

    def pa_values(self):
        n = int(round((self.pa_end - self.pa_start) / self.pa_step)) + 1
        vals = [round(self.pa_start + i * self.pa_step, 6) for i in range(n)]
        return [v for v in vals if v <= self.pa_end + 1e-9]

    def fast_duration(self):
        return self.extrude_len / self.fast_speed
