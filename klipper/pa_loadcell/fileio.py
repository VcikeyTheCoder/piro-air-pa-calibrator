"""Crash-safe file writes and a helper that runs heavy work in a separate
process, so Klipper's main loop keeps running (heaters, steps, webhooks).

atomic_write: temp file -> flush + fsync -> rename -> fsync the folder. A power
cut leaves either the old file or the new one, never an empty one.

in_background: the same pattern Klipper uses for its own heavy maths
(mathutil.background_coordinate_descent, shaper_calibrate): fork a process,
let it compute, and pause the reactor (not block it) until the answer is
back. The child never touches the reactor, G-code, the serial port or
Klipper's log queue. Without fork (or in tests) the work runs inline."""
import json
import logging
import multiprocessing
import os
import time
import traceback
from .errors import PAError


def atomic_write(path, text, sync=True):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
        if sync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)
    if not sync:
        return
    try:
        fd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass                    # some filesystems cannot fsync a folder


def atomic_json(path, obj, sync=True):
    atomic_write(path, json.dumps(obj, separators=(',', ':')), sync)


INLINE = False                  # tests set this: no fork


def in_background(reactor, fn, args=(), timeout=120.):
    """fn(*args) in a forked process; the reactor keeps running while it
    works. -> fn's return value (must pickle). Raises RuntimeError with the
    child's error. Falls back to running inline when fork is not possible."""
    if INLINE or reactor is None or not hasattr(reactor, 'pause') \
            or not hasattr(os, 'fork'):
        return fn(*args)
    try:
        ctx = multiprocessing.get_context('fork')
        parent, child = ctx.Pipe(duplex=False)
    except (ValueError, OSError):
        return fn(*args)

    def work():
        try:
            import queuelogger              # Klipper's log queue: not in the child
            queuelogger.clear_bg_logging()
        except Exception:
            pass
        try:
            res = (False, fn(*args))
        except PAError as e:
            res = ('pa', str(e))             # a clean message, no traceback
        except BaseException:
            res = (True, traceback.format_exc())
        try:
            child.send(res)
        finally:
            child.close()
    proc = ctx.Process(target=work)
    proc.daemon = True
    try:
        proc.start()
    except OSError:
        return fn(*args)
    child.close()
    end = time.time() + timeout
    try:
        while not parent.poll():
            if not proc.is_alive() and not parent.poll():
                raise RuntimeError("Background analysis stopped unexpectedly")
            if time.time() > end:
                proc.terminate()
                raise RuntimeError("Background analysis took longer than %.0f s"
                                   % timeout)
            reactor.pause(reactor.monotonic() + 0.05)
        err, res = parent.recv()
    finally:
        parent.close()
        proc.join(1.)
    if err == 'pa':
        raise PAError(res)
    if err:
        logging.error("pa_loadcell background work failed:\n%s", res)
        raise RuntimeError(res.strip().splitlines()[-1])
    return res
