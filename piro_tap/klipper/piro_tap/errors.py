"""TapError: a touch, calibration or thermal step that cannot go on. The
probe turns it into Klipper's command_error ("piro_tap: ...")."""


class TapError(Exception):
    pass
