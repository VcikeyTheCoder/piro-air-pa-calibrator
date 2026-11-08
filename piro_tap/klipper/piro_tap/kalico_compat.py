"""What the Kalico files expect from Kalico's klippy package, taken from
mainline Klipper instead. Type names are only used in annotations."""
try:
    from ..load_cell import ApiClientHelper          # mainline Klipper
except ImportError:                                  # tests outside Klipper
    class ApiClientHelper(object):
        def __init__(self, printer):
            self.printer = printer
            self.client_cbs = []

        def send(self, msg):
            for cb in list(self.client_cbs):
                if not cb(msg):
                    self.client_cbs.remove(cb)

        def add_client(self, cb):
            self.client_cbs.append(cb)

        def add_mux_endpoint(self, path, key, value, header):
            pass

Printer = PrinterConfig = ConfigWrapper = GCodeCommand = object
DumpTrapQ = PrinterMotionReport = object
NEVER_TIME = 9999999999999999.9


def extract_trapq(dtrapq, start_time):
    """Kalico's DumpTrapQ.extract_trapq(start) = mainline's
    extract_trapq(start, end) with no end."""
    try:
        return dtrapq.extract_trapq(start_time, NEVER_TIME)
    except TypeError:
        return dtrapq.extract_trapq(start_time)
