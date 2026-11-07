# [pa_eddy]: a BTT Eddy (LDC1612 over I2C) used as a nozzle pressure sensor
# for pa_loadcell, without making it a Z probe. (To probe with it as well,
# use [piro_tap] instead: it does both.)
#
# Klipper's ldc1612.py has no config section of its own (only
# [probe_eddy_current] starts it, as a probe). This module starts Klipper's
# own LDC1612 driver from a [pa_eddy] section and hands its samples (time,
# frequency in Hz) to pa_loadcell through the shared Eddy hub (piro_eddy.py):
# the stream starts
# only after Klipper is ready, and the sample rate follows the job (idle /
# PA test). Klipper's LDC_CALIBRATE_DRIVE_CURRENT CHIP=pa_eddy works as usual.
#
# One chip, one section: never [pa_eddy], [piro_tap] and
# [probe_eddy_current] on the same Eddy.
#
# Example (BTT Eddy on an EBB36 I2C header):
#   [pa_eddy]
#   i2c_mcu: EBB
#   i2c_bus: i2c3_PB3_PB4
#   #reg_drive_current: 15     # LDC_CALIBRATE_DRIVE_CURRENT CHIP=pa_eddy
#   #idle_rate: 100            # samples/s while only the live graph reads
#   #pa_rate: 400              # samples/s during a PA test
from . import ldc1612
from .piro_eddy import LdcHub, check_rate, check_one_owner


class PAEddy:
    def __init__(self, config):
        self.printer = self._printer = config.get_printer()
        self.name = config.get_name()
        check_one_owner(config)
        self.sensor = ldc1612.LDC1612(config)
        rates = {}
        for m, d in (('idle', 100), ('pa', 400)):
            try:
                rates[m] = check_rate(config.getint(m + '_rate', d))
            except ValueError as e:
                raise config.error("[%s] %s" % (self.name, e))
        rates['probe'] = rates['pa']
        self.hub = LdcHub(self.printer, self.sensor, self.name, rates)

    def add_client(self, cb):
        """cb(msg): msg['data'] = [(print_time, frequency_hz, z), ...]"""
        self.hub.add_client(cb)

    def ensure(self):
        return self.hub.ensure()

    def set_mode(self, owner, mode):
        return self.hub.set_mode(owner, mode)

    def get_status(self, eventtime):
        return self.hub.status()


def load_config(config):
    return PAEddy(config)


def load_config_prefix(config):
    return PAEddy(config)
