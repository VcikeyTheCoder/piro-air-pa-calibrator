# [pa_eddy]: a BTT Eddy (LDC1612 over I2C) used as a nozzle pressure sensor
# for pa_loadcell, without making it a Z probe.
#
# Klipper's ldc1612.py has no config section of its own (only
# [probe_eddy_current] starts it, as a probe). This module starts Klipper's
# own LDC1612 driver from a [pa_eddy] section and hands its samples (time,
# frequency in Hz) to pa_loadcell. Klipper's LDC_CALIBRATE_DRIVE_CURRENT
# CHIP=pa_eddy works on it as usual.
#
# One chip, one driver: if the same Eddy is already a [probe_eddy_current]
# probe, do not add [pa_eddy]; set  eddy: probe_eddy_current NAME  in
# [pa_loadcell] instead (Klipper would otherwise let two drivers program the
# same chip).
#
# Example (BTT Eddy USB):
#   [pa_eddy]
#   i2c_mcu: eddy
#   i2c_bus: i2c0f
#   #reg_drive_current: 15     # LDC_CALIBRATE_DRIVE_CURRENT CHIP=pa_eddy
from . import ldc1612

LDC1612_ADDR = 0x2a


def _chip(config):
    """(mcu, bus or software pins, address) of an I2C section."""
    sw = config.get('i2c_software_scl_pin', None)
    return (config.get('i2c_mcu', 'mcu'),
            sw or config.get('i2c_bus', None),
            config.getint('i2c_address', LDC1612_ADDR, minval=0, maxval=127))


class PAEddy:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        mine = _chip(config)
        for other in config.get_prefix_sections('probe_eddy_current'):
            if _chip(other) == mine:
                raise config.error(
                    "[%s] uses the same Eddy chip as [%s]. Remove [%s] and "
                    "set 'eddy: %s' in [pa_loadcell]"
                    % (self.name, other.get_name(), self.name,
                       other.get_name()))
        self.sensor = ldc1612.LDC1612(config)

    def add_client(self, cb):
        """cb(msg): msg['data'] = [(print_time, frequency_hz, z), ...]"""
        self.sensor.add_client(cb)

    def get_status(self, eventtime):
        return {}


def load_config(config):
    return PAEddy(config)


def load_config_prefix(config):
    return PAEddy(config)
