"""Small saved state that does not belong in printer.cfg: the tap quality
limits found by PIRO_TAP_CALIBRATE MODE=QUALITY."""
import json
import logging
import os

from .tapfit import DEFAULT_LIMITS


class State:
    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = d
        except (OSError, ValueError):
            pass

    def limits(self):
        out = dict(DEFAULT_LIMITS)
        for k, v in (self.data.get('limits') or {}).items():
            if k in out and isinstance(v, (int, float)) and 0 < v < 10:
                out[k] = float(v)
        return out

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            logging.exception("piro_tap: saving %s failed", self.path)
