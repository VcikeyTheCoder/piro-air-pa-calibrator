"""Where the PIRO plugins keep their files: one folder outside the config
folder, so the config folder only holds the .cfg files you edit.

  ~/printer_data/prio_edge/
    pa_auto/    PA calibrator runs, themes, sensor state
    eddy/       PIRO-TAP taps and state
    loadcell/   load cell homing state and probe tests
    backups/    copies of config files before they are changed (last 10)

Each Klipper module has its own copy of this file (they are installed
separately)."""
import os
import time

ROOT = '~/printer_data/prio_edge'
KEEP_BACKUPS = 10


def resolve(configured, sub, old=None):
    """Data folder: the configured one, else ROOT/sub. A config that still
    names the old default folder, or an old folder not moved yet, keeps
    working: the old folder is used until install.sh moves it."""
    new = os.path.expanduser(os.path.join(ROOT, sub))
    oldp = os.path.expanduser(old) if old else None
    if configured:
        c = os.path.expanduser(configured)
        if oldp is None or os.path.normpath(c) != os.path.normpath(oldp):
            return c
    if oldp and not os.path.isdir(new) and os.path.isdir(oldp):
        return oldp
    return new


def backup(path, text, keep=KEEP_BACKUPS):
    """Save `text` (the old content of `path`) as
    ROOT/backups/<name>.<date>.bak and keep the newest `keep` of that
    name."""
    bdir = os.path.expanduser(os.path.join(ROOT, 'backups'))
    os.makedirs(bdir, exist_ok=True)
    name = os.path.basename(path)
    dst = os.path.join(bdir, '%s.%s.bak' % (name, time.strftime(
        '%Y%m%d-%H%M%S')))
    tmp = dst + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)
    old = sorted(n for n in os.listdir(bdir)
                 if n.startswith(name + '.') and n.endswith('.bak'))
    for n in old[:-keep]:
        try:
            os.remove(os.path.join(bdir, n))
        except OSError:
            pass
    return dst
