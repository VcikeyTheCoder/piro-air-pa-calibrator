"""Run -> PA values -> load cell data, stored as plain JSON files:
  <data_dir>/run_0001/meta.json   settings, tags, per-PA summaries
  <data_dir>/run_0001/seg_000.json  one file per PA value (own samples)
Meta is rewritten after every segment, so a cancelled or crashed test still
keeps everything recorded so far."""
import json
import os
import re
import shutil
import time


def _atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'))
    os.replace(tmp, path)


def _load(path):
    with open(path) as f:
        return json.load(f)


def decimate(t, f, max_points):
    """Min/max bucket decimation: keeps peaks and edges."""
    n = len(t)
    if not max_points or n <= max_points:
        return t, f
    nb = max(1, max_points // 2)
    step = n / nb
    ot, of = [], []
    for b in range(nb):
        lo = int(b * step)
        hi = max(int((b + 1) * step), lo + 1)
        idx = range(lo, min(hi, n))
        i_min, i_max = min(idx, key=f.__getitem__), max(idx, key=f.__getitem__)
        for i in sorted({i_min, i_max}):
            ot.append(t[i])
            of.append(f[i])
    return ot, of


class RunStore:
    def __init__(self, root):
        self.root = os.path.expanduser(root)
        os.makedirs(self.root, exist_ok=True)

    def _dir(self, rid):
        return os.path.join(self.root, 'run_%04d' % rid)

    def _meta_path(self, rid):
        return os.path.join(self._dir(rid), 'meta.json')

    def _ids(self):
        ids = []
        for n in os.listdir(self.root):
            m = re.fullmatch(r'run_(\d+)', n)
            if m:
                ids.append(int(m.group(1)))
        return sorted(ids)

    def new_run(self, kind, meta):
        rid = max(self._ids(), default=0) + 1
        os.makedirs(self._dir(rid))
        meta = dict(meta, id=rid, kind=kind, name='Run %d' % rid,
                    date=time.strftime('%Y-%m-%d %H:%M:%S'),
                    status='running', error='', segments=[],
                    pa_pick=None, notes='')
        _atomic(self._meta_path(rid), meta)
        return rid

    def add_segment(self, rid, seg):
        meta = _load(self._meta_path(rid))
        idx = len(meta['segments'])
        seg = dict(seg, samples=len(seg['t']))
        _atomic(os.path.join(self._dir(rid), 'seg_%03d.json' % idx), seg)
        meta['segments'].append({
            'idx': idx, 'pa': seg.get('pa'), 'samples': seg['samples'],
            'duration': seg['t'][-1] if seg['t'] else 0.0,
            'partial': bool(seg.get('partial')), 'unit': seg.get('unit')})
        _atomic(self._meta_path(rid), meta)
        return idx

    def finalize(self, rid, status, error=''):
        meta = _load(self._meta_path(rid))
        meta['status'], meta['error'] = status, error
        _atomic(self._meta_path(rid), meta)

    def list_runs(self):
        out = []
        for rid in reversed(self._ids()):
            try:
                m = _load(self._meta_path(rid))
            except (OSError, ValueError):
                continue
            out.append({k: m.get(k) for k in (
                'id', 'name', 'kind', 'date', 'status', 'filament',
                'nozzle_temp', 'pa_pick', 'notes', 'settings')})
            out[-1]['segments'] = len(m.get('segments', []))
        return out

    def get_run(self, rid):
        return _load(self._meta_path(rid))

    def get_segment(self, rid, idx, max_points=0):
        seg = _load(os.path.join(self._dir(rid), 'seg_%03d.json' % idx))
        seg['t'], seg['f'] = decimate(seg['t'], seg['f'], max_points)
        return seg

    def set_tag(self, rid, pa_pick=None, notes=None, name=None):
        m = _load(self._meta_path(rid))
        if pa_pick is not None:
            m['pa_pick'] = pa_pick if pa_pick != '' else None
        if notes is not None:
            m['notes'] = notes
        if name:
            m['name'] = name
        _atomic(self._meta_path(rid), m)

    def delete(self, rid):
        shutil.rmtree(self._dir(rid), ignore_errors=True)
