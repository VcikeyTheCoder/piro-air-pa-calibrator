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
dips, failing at PA_LIMIT), then values around the predicted answer."""
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
        s = Settings(**out)
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
            raise PAError("CYCLES must be at least 2 (the first is skipped)")
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

    @staticmethod
    def charge_speed(pa, area, fast_flow, slow_flow, smooth):
        """mm/s of filament the artificial PA charge move needs at `pa`."""
        vf, vs = fast_flow / area, slow_flow / area
        return (pa * (vf - vs) + (vf + vs) * smooth / 2.) / smooth
