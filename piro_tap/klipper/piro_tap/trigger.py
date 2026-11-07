"""The contact trigger on the Eddy's MCU (Klipper's trigger_analog).

Per touch: the SOS filter gets the tare as its offset and a scale that turns
raw LDC1612 counts into 1/64 Hz of pressure; the trigger fires when the
filtered pressure passes trigger_hz; the raw range is tare +- force_limit_hz.
The MCU only checks all of this during a probing move, so the force limit
never plays a part in PA tests."""
from . import filters
from .errors import TapError

FRAC_HZ = 64.               # MCU filter units per Hz
MAX_SECTIONS = 4            # SOS sections the MCU filter has room for


def sections_needed(lowpass_order, drift_hz):
    return (lowpass_order + 1) // 2 + (1 if drift_hz else 0)


class Trigger:
    def __init__(self, sensor, trig, sos, opts):
        self.sensor, self.trig, self.sos, self.o = sensor, trig, sos, opts

    def design(self, rate):
        d = filters.Design(rate)
        try:
            d.add_lowpass(self.o.lowpass_hz, self.o.lowpass_order)
            if self.o.drift_hz:
                d.add_highpass(self.o.drift_hz, 1)
        except ValueError as e:
            raise TapError("filter: %s (Eddy at %d samples/s)" % (e, rate))
        return d

    def arm(self, rate, tare_hz, trigger_hz, limit_hz, sign):
        """Program filter, trigger and force limit for the next move."""
        self.sos.set_filter_design(self.design(rate))
        self.sos.set_start_state(0.)
        tare_raw = self.sensor.convert_frequency_to_raw(tare_hz)
        per_raw = self.sensor.convert_raw_to_frequency(1)
        self.sos.set_offset_scale(int(-tare_raw), (sign or 1) * per_raw * FRAC_HZ)
        # auto sign: fire on a change either way
        self.trig.set_trigger('gt' if sign else 'abs_ge',
                              int(trigger_hz * FRAC_HZ))
        lim = self.sensor.convert_frequency_to_raw(limit_hz)
        self.trig.set_raw_range(max(1, tare_raw - lim), tare_raw + lim)

    def filtered(self, rate, hz):
        """The pressure the MCU trigger would see for these samples (Hz,
        relative to their mean)."""
        if not hz:
            return []
        m = sum(hz) / len(hz)
        return filters.sosfilt(self.design(rate).get_filter_sections(),
                               [x - m for x in hz])
