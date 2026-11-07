"""Recent taps for the web page: a summary list in memory and one small
JSON file per tap (written without fsync: losing the last taps on a power
cut is fine, slowing a mesh down is not)."""
import collections
import json
import logging
import os
import re
import time

KEEP_FILES = 400


class TapStore:
    def __init__(self, root, keep=KEEP_FILES):
        self.root = os.path.join(os.path.expanduser(root), 'taps')
        self.keep = keep
        self.recent = collections.deque(maxlen=200)
        self.next_id = 1
        try:
            os.makedirs(self.root, exist_ok=True)
            ids = self._ids()
            self.next_id = (max(ids) + 1) if ids else 1
            for i in ids[-60:]:
                try:
                    with open(self._path(i)) as f:
                        self.recent.append(json.load(f)['summary'])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        except OSError:
            logging.exception("piro_tap: tap folder not usable")

    def _path(self, i):
        return os.path.join(self.root, 'tap_%06d.json' % i)

    def _ids(self):
        out = []
        for n in os.listdir(self.root):
            m = re.fullmatch(r'tap_(\d+)\.json', n)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def add(self, summary, detail):
        i = self.next_id
        self.next_id += 1
        summary = dict(summary, id=i, time=round(time.time(), 1))
        self.recent.append(summary)
        try:
            text = json.dumps({'summary': summary, 'detail': detail},
                              separators=(',', ':'))
            tmp = self._path(i) + '.tmp'
            with open(tmp, 'w') as f:
                f.write(text)
            os.replace(tmp, self._path(i))
            if i % 50 == 0:
                self._prune()
        except (OSError, TypeError, ValueError):
            logging.exception("piro_tap: saving tap %d failed", i)
        return summary

    def _prune(self):
        ids = self._ids()
        for i in ids[:-self.keep]:
            try:
                os.remove(self._path(i))
            except OSError:
                pass

    def list(self, limit=100):
        return list(self.recent)[-limit:]

    def get(self, i):
        try:
            with open(self._path(int(i))) as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            raise KeyError("Tap %s not found" % (i,))
