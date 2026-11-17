"""Files: state (latest homing temperature, paper thickness) and the test
sessions the UI starts. Everything lives under one data folder, by default
~/printer_data/prio_edge/loadcell, never in the config folder.

  state.json           latest Z homing temperature, paper thickness, ...
  sessions/index.json  one row per test session (newest last)
  sessions/s_000001.json  every touch of one session

Writes are atomic (temp file + rename) and run on a worker thread, so a
slow SD card never stalls Klipper's reactor."""
import json
import logging
import os
import queue
import threading
import time

MAX_SESSIONS = 500          # oldest session files are removed past this


def _atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Writer:
    """One background thread that writes JSON files in order."""
    def __init__(self):
        self.q = queue.Queue()
        self.t = threading.Thread(target=self._run, name='piro_lc-writer',
                                  daemon=True)
        self.t.start()

    def put(self, path, obj):
        self.q.put((path, obj))

    def _run(self):
        while True:
            path, obj = self.q.get()
            try:
                if callable(obj):
                    obj()
                else:
                    _atomic(path, obj)
            except Exception:
                logging.exception("piro_lc: could not write %s", path)


class Store:
    def __init__(self, root, writer=None):
        self.root = os.path.expanduser(root)
        self.sdir = os.path.join(self.root, 'sessions')
        try:
            os.makedirs(self.sdir, exist_ok=True)
        except OSError:
            logging.exception("piro_lc: cannot create %s", self.sdir)
        self.writer = writer or Writer()
        self.state = _read(os.path.join(self.root, 'state.json'), {})
        self.index = _read(os.path.join(self.sdir, 'index.json'), [])
        if not isinstance(self.index, list):
            self.index = []
        self.next_id = max([r.get('id', 0) for r in self.index] + [0]) + 1

    # -- state ------------------------------------------------------------
    def get(self, key, default=None):
        return self.state.get(key, default)

    def set(self, key, value):
        self.state[key] = value
        self.writer.put(os.path.join(self.root, 'state.json'),
                        dict(self.state))

    # -- sessions ---------------------------------------------------------
    def new_id(self):
        sid = self.next_id
        self.next_id += 1
        return sid

    def _path(self, sid):
        return os.path.join(self.sdir, 's_%06d.json' % sid)

    def save_session(self, sess):
        row = {k: sess[k] for k in ('id', 'kind', 'start', 'end', 'n',
                                    'summary', 'ok', 'error') if k in sess}
        self.index.append(row)
        drop = []
        if len(self.index) > MAX_SESSIONS:
            drop = [r['id'] for r in self.index[:-MAX_SESSIONS]]
            self.index = self.index[-MAX_SESSIONS:]
        self.writer.put(self._path(sess['id']), sess)
        self.writer.put(os.path.join(self.sdir, 'index.json'),
                        list(self.index))
        for sid in drop:
            p = self._path(sid)
            self.writer.put(p, lambda p=p: os.path.exists(p) and os.remove(p))

    def list(self, kind=None, limit=200):
        rows = [r for r in self.index if kind is None or r.get('kind') == kind]
        return rows[-limit:]

    def load(self, sid):
        d = _read(self._path(int(sid)), None)
        if d is None:
            raise KeyError("no session %s" % (sid,))
        return d

    def load_many(self, kind, limit):
        out = []
        for r in self.list(kind, limit):
            d = _read(self._path(r['id']), None)
            if d is not None:
                out.append(d)
        return out


def now():
    return time.time()
