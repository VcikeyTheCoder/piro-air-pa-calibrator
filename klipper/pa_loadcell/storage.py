"""Run -> PA values -> load cell data, stored as plain JSON files:
  <data_dir>/run_0001/meta.json   settings, tags, per-PA summaries
  <data_dir>/run_0001/seg_000.json  one file per PA value (own samples)
Meta is rewritten after every segment, so a cancelled or crashed test still
keeps everything recorded so far.

Analysis results (edge summaries, model picks, command fit) are derived
from the raw samples and can be recomputed at any time (reanalyze); the
raw samples are never changed. A run can be exported as one JSON file and
imported again (here or on another printer)."""
import bisect
import json
import os
import re
import shutil
import time
from . import analysis
from .errors import PAError

EXPORT_FORMAT = 'pa_loadcell_run'
EXPORT_VERSION = 1
DEFAULT_MODEL = 'fall_undershoot'


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

    @staticmethod
    def _opts(meta):
        a = meta.get('analysis') or {}
        return {'model': a.get('model') or DEFAULT_MODEL,
                'skip_first': bool(a.get('skip_first', False)),
                'dip_min': float(a.get('dip_min', analysis.DIP_MIN))}

    @staticmethod
    def _flows(meta, seg):
        fl = seg.get('flows')
        if fl:
            return fl
        st = meta.get('settings') or {}
        if 'fast_flow' in st and 'slow_flow' in st:
            return {'fast': st['fast_flow'], 'slow': st['slow_flow']}
        return None

    def _entry(self, meta, idx, seg):
        """Per-PA summary stored in meta (recomputed by reanalyze)."""
        opts = self._opts(meta)
        t, f = seg['t'], seg['f']
        entry = {
            'idx': idx, 'pa': seg.get('pa'), 'samples': len(t),
            'duration': t[-1] if t else 0.0,
            'partial': bool(seg.get('partial')), 'unit': seg.get('unit')}
        mk = seg.get('markers') or []
        if any(m.get('name') == 'fast' for m in mk):
            edge = {k: analysis.edge_summary(t, f, mk, k,
                                             skip_first=opts['skip_first'])
                    for k in ('rise', 'fall')}
            if edge['rise'] or edge['fall']:
                entry['edge'] = edge
            fit = analysis.command_fit(t, f, mk, self._flows(meta, seg))
            if fit:
                entry['cmd'] = fit
        return entry

    def _summarize(self, meta):
        opts = self._opts(meta)
        a = dict(meta.get('analysis') or {}, **opts)
        a['picks'] = analysis.all_picks(meta['segments'], opts['dip_min'])
        a['offset'] = analysis.timing_offset(meta['segments'])
        a['models'] = analysis.MODELS
        meta['analysis'] = a
        if meta.get('kind') == 'test' and meta.get('settings'):
            fl = self._flows(meta, {})
            if fl:
                meta['flows'] = fl
        return meta

    def add_segment(self, rid, seg):
        meta = _load(self._meta_path(rid))
        idx = len(meta['segments'])
        seg = dict(seg, samples=len(seg['t']))
        _atomic(os.path.join(self._dir(rid), 'seg_%03d.json' % idx), seg)
        meta['segments'].append(self._entry(meta, idx, seg))
        self._summarize(meta)
        _atomic(self._meta_path(rid), meta)
        return idx

    def finalize(self, rid, status, error=''):
        meta = _load(self._meta_path(rid))
        meta['status'], meta['error'] = status, error
        self._summarize(meta)
        _atomic(self._meta_path(rid), meta)

    def reanalyze(self, rid, model=None, skip_first=None, dip_min=None):
        """Recompute every per-PA summary and the picks from the raw
        samples with new analysis options. Raw data is left untouched."""
        meta = _load(self._meta_path(rid))
        a = dict(meta.get('analysis') or {})
        if model is not None:
            if model not in analysis.MODELS:
                raise PAError("unknown model '%s'" % model)
            a['model'] = model
        if skip_first is not None:
            a['skip_first'] = bool(skip_first)
        if dip_min is not None:
            if not 0 < dip_min < 0.5:
                raise PAError("dip_min must be between 0 and 0.5")
            a['dip_min'] = dip_min
        meta['analysis'] = a
        segs = []
        for e in meta.get('segments', []):
            seg = _load(os.path.join(self._dir(rid), 'seg_%03d.json'
                                     % e['idx']))
            segs.append(self._entry(meta, e['idx'], seg))
        meta['segments'] = segs
        meta['analysed'] = time.strftime('%Y-%m-%d %H:%M:%S')
        self._summarize(meta)
        _atomic(self._meta_path(rid), meta)
        return meta

    # -- export / import --------------------------------------------------
    def export_run(self, rid):
        meta = _load(self._meta_path(rid))
        segs = [_load(os.path.join(self._dir(rid), 'seg_%03d.json'
                                   % e['idx']))
                for e in meta.get('segments', [])]
        return {'format': EXPORT_FORMAT, 'version': EXPORT_VERSION,
                'exported': time.strftime('%Y-%m-%d %H:%M:%S'),
                'meta': meta, 'segments': segs}

    def import_run(self, data):
        if not isinstance(data, dict) or data.get('format') != EXPORT_FORMAT:
            raise PAError("not a PA load cell run file")
        if int(data.get('version', 0)) > EXPORT_VERSION:
            raise PAError("this file was made by a newer version; update "
                          "the plugin first")
        meta, segs = data.get('meta'), data.get('segments')
        if not isinstance(meta, dict) or not isinstance(segs, list):
            raise PAError("run file is incomplete")
        for sg in segs:
            if not (isinstance(sg, dict) and isinstance(sg.get('t'), list)
                    and isinstance(sg.get('f'), list)
                    and len(sg['t']) == len(sg['f'])):
                raise PAError("run file has a damaged segment")
        rid = max(self._ids(), default=0) + 1
        os.makedirs(self._dir(rid))
        try:
            new = dict(meta)
            new.update({'id': rid, 'segments': [],
                        'imported': {'from_id': meta.get('id'),
                                     'from_date': meta.get('date'),
                                     'at': time.strftime('%Y-%m-%d %H:%M:%S')}})
            nm = str(meta.get('name') or 'Run')
            new['name'] = nm if nm.endswith('(imported)') else nm + ' (imported)'
            if new.get('status') == 'running':
                new['status'] = 'cancelled'
            for i, sg in enumerate(segs):
                _atomic(os.path.join(self._dir(rid), 'seg_%03d.json' % i),
                        dict(sg, samples=len(sg['t'])))
                new['segments'].append({'idx': i})
            _atomic(self._meta_path(rid), new)
            self.reanalyze(rid)
        except Exception:
            shutil.rmtree(self._dir(rid), ignore_errors=True)
            raise
        return rid

    def list_runs(self):
        out = []
        for rid in reversed(self._ids()):
            try:
                m = _load(self._meta_path(rid))
            except (OSError, ValueError):
                continue
            out.append({k: m.get(k) for k in (
                'id', 'name', 'kind', 'date', 'status', 'filament',
                'nozzle_temp', 'pa_pick', 'notes', 'settings', 'imported')})
            out[-1]['segments'] = len(m.get('segments', []))
            a = m.get('analysis') or {}
            p = (a.get('picks') or {}).get(a.get('model') or DEFAULT_MODEL)
            out[-1]['model_pick'] = p and p.get('pa')
        return out

    def get_run(self, rid):
        meta = _load(self._meta_path(rid))
        if not (meta.get('analysis') or {}).get('picks'):
            # runs from older versions: picks computed on the fly from the
            # stored summaries (press Re-analyse to apply the new options)
            self._summarize(meta)
            meta['analysis']['stale'] = True
        return meta

    def get_segment(self, rid, idx, max_points=0, t0=None, t1=None):
        """One segment. t0/t1 (seconds, segment time) return only that
        stretch, so the History graph can load full detail for the part
        on screen; max_points then thins it with min/max buckets."""
        seg = _load(os.path.join(self._dir(rid), 'seg_%03d.json' % idx))
        t, f = seg['t'], seg['f']
        seg['span'] = [t[0], t[-1]] if t else [0.0, 0.0]
        if t0 is not None or t1 is not None:
            lo = bisect.bisect_left(t, t0) if t0 is not None else 0
            hi = bisect.bisect_right(t, t1) if t1 is not None else len(t)
            t, f = t[lo:hi], f[lo:hi]
            seg['range'] = [t0, t1]
        seg['t'], seg['f'] = decimate(t, f, max_points)
        seg['decimated'] = len(seg['t']) < len(t)
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
