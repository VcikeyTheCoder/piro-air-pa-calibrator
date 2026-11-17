"""Run -> PA values -> sensor data, stored as plain JSON files:
  <data_dir>/run_0001/meta.json     settings, tags, per-PA summaries
  <data_dir>/run_0001/seg_000.json  one file per PA value (own samples)
  <data_dir>/index.json             one summary row per run (History list)
Meta is rewritten after every segment, so a cancelled or crashed test still
keeps everything recorded so far. Every write is crash-safe (fileio): a power
cut leaves the old file or the new one. index.json only makes the History
list fast: it is rebuilt from the run folders whenever it is missing, damaged
or out of date, never the other way round.

Analysis results are derived from the raw samples and can be recomputed at
any time (reanalyze); the raw samples are never changed. Heavy work
(re-analysing, importing, the final analysis of a test) runs through
self.bg, which the plugin points at fileio.in_background so Klipper's main
loop keeps running. A run can be exported as one JSON file and imported
again (here or on another printer); imports are checked field by field."""
import bisect
import json
import math
import os
import re
import shutil
import time
from . import analysis
from .errors import PAError
from .fileio import atomic_json

EXPORT_FORMAT = 'pa_loadcell_run'
EXPORT_VERSION = 1
QUICK_BOOT = 100            # bootstrap draws while a test is still running
ROW_KEYS = ('id', 'name', 'kind', 'date', 'status', 'filament', 'nozzle_temp',
            'pa_pick', 'notes', 'settings', 'imported', 'auto', 'pa_method',
            'sensor')


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


# -- checking imported files ---------------------------------------------
def _num(x):
    """A finite number or None (strings, NaN, lists ... -> None)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _txt(x, n=200):
    return str(x)[:n] if isinstance(x, (str, int, float)) and \
        not isinstance(x, bool) else ''


def _clean_settings(st):
    if not isinstance(st, dict):
        return {}
    out = {}
    for k, v in list(st.items())[:200]:
        k = _txt(k, 40)
        if isinstance(v, bool) or _num(v) is not None:
            out[k] = v if isinstance(v, bool) else _num(v)
        elif isinstance(v, str):
            out[k] = v[:120]
    return out


def _clean_marker(m):
    if not isinstance(m, dict):
        return None
    t0, t1 = _num(m.get('t0')), _num(m.get('t1'))
    if t0 is None or t1 is None:
        return None
    out = {'name': _txt(m.get('name'), 20), 't0': t0, 't1': t1}
    if _num(m.get('sweep')) is not None:
        out['sweep'] = int(_num(m['sweep']))
    if m.get('label') is not None:
        out['label'] = _txt(m['label'], 80)
    return out


def _clean_series(t, f, what):
    if not (isinstance(t, list) and isinstance(f, list) and len(t) == len(f)):
        raise PAError("Run file has a damaged %s" % what)
    tt, ff = [_num(x) for x in t], [_num(x) for x in f]
    if None in tt or None in ff:
        raise PAError("Run file has a damaged %s (not numbers)" % what)
    return tt, ff


def clean_segment(sg):
    """A segment from a run file, with only known fields of the right type."""
    if not isinstance(sg, dict):
        raise PAError("Run file has a damaged segment")
    t, f = _clean_series(sg.get('t'), sg.get('f'), 'segment')
    out = {'t': t, 'f': f, 'pa': _num(sg.get('pa')),
           'unit': _txt(sg.get('unit'), 10) or 'g',
           'partial': bool(sg.get('partial')),
           'markers': [m for m in map(_clean_marker, sg.get('markers') or [])
                       if m] if isinstance(sg.get('markers'), list) else []}
    if _num(sg.get('rate')) is not None:
        out['rate'] = _num(sg['rate'])
    for k in ('method', 'stage', 'search_note', 'sensor'):
        if sg.get(k) is not None:
            out[k] = _txt(sg[k], 40)
    if sg.get('repeat'):
        out['repeat'] = True
    fl = sg.get('flows')
    if isinstance(fl, dict) and _num(fl.get('fast')) is not None \
            and _num(fl.get('slow')) is not None:
        out['flows'] = {'fast': _num(fl['fast']), 'slow': _num(fl['slow'])}
    ch = sg.get('channels')               # second sensor of a 'both' run
    if isinstance(ch, dict):
        good = {}
        for name, c in list(ch.items())[:4]:
            if isinstance(c, dict):
                ct, cf = _clean_series(c.get('t'), c.get('f'), 'sensor channel')
                good[_txt(name, 20)] = {'t': ct, 'f': cf,
                                        'unit': _txt(c.get('unit'), 10),
                                        'rate': _num(c.get('rate'))}
        if good:
            out['channels'] = good
    return out


def clean_meta(meta):
    """Run metadata from a file: numbers stay numbers, text stays short text,
    unknown structures are dropped (the analysis is recomputed anyway)."""
    if not isinstance(meta, dict):
        raise PAError("Run file incomplete")
    out = {}
    for k in ('name', 'kind', 'date', 'status', 'filament', 'notes', 'error',
              'method', 'pa_method', 'sensor', 'primary'):
        if meta.get(k) is not None:
            out[k] = _txt(meta[k], 2000 if k == 'notes' else 200)
    for k in ('id', 'nozzle_temp', 'filament_area', 'filament_mm',
              'smooth_time'):
        if _num(meta.get(k)) is not None:
            out[k] = _num(meta[k])
    out['pa_pick'] = _num(meta.get('pa_pick'))
    out['settings'] = _clean_settings(meta.get('settings'))
    for k in ('leg_mm', 'push', 'line', 'flows'):
        v = meta.get(k)
        if isinstance(v, dict):
            out[k] = {_txt(a, 40): _num(b) for a, b in v.items()
                      if _num(b) is not None}
    a = meta.get('analysis')
    out['analysis'] = {'skip_first': bool(a.get('skip_first', True))} \
        if isinstance(a, dict) else {'skip_first': True}
    pr = meta.get('prime')
    if isinstance(pr, dict):
        lv = pr.get('levels') if isinstance(pr.get('levels'), list) else []
        out['prime'] = {'mm': _num(pr.get('mm')),
                        'steady': bool(pr.get('steady')),
                        'levels': [x for x in map(_num, lv)
                                   if x is not None][:200]}
    au = meta.get('auto')
    if isinstance(au, dict):
        out['auto'] = {k: _num(au.get(k)) for k in (
            'applied', 'target_temp', 'wall_flow', 'smooth', 'reused', 'time',
            'last_used')}
        out['auto'].update({k: _txt(au.get(k), 120) for k in (
            'reason', 'filament', 'pa_method', 'mode')})
        out['auto']['fallback'] = bool(au.get('fallback'))
    se = meta.get('search')
    if isinstance(se, dict) and isinstance(se.get('steps'), list):
        steps = []
        for st in se['steps'][:64]:
            if isinstance(st, dict):
                steps.append({
                    'pa': _num(st.get('pa')), 'idx': _num(st.get('idx')),
                    'dip': _num(st.get('dip')), 'limit': _num(st.get('limit')),
                    'balance': _num(st.get('balance')),
                    'prediction': _num(st.get('prediction')),
                    'stage': _txt(st.get('stage'), 20),
                    'verdict': _txt(st.get('verdict'), 20)})
        out['search'] = {'steps': steps}
        for k in ('result', 'unc', 'prediction', 'floor'):
            out['search'][k] = _num(se.get(k))
        out['search']['grade'] = _txt(se.get('grade'), 10)
    return out


class RunStore:
    def __init__(self, root):
        self.root = os.path.expanduser(root)
        os.makedirs(self.root, exist_ok=True)
        self.bg = lambda fn, args: fn(*args)     # plugin: fileio.in_background
        self.busy = set()                        # runs being re-analysed
        self._index = None

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

    # -- safe reading ------------------------------------------------------
    def _meta(self, rid):
        """meta.json of a run, or PAError (missing / damaged), never a crash."""
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            raise PAError("Bad run number")
        try:
            m = _load(self._meta_path(rid))
        except FileNotFoundError:
            raise PAError("Run %d not found" % rid)
        except (OSError, ValueError):
            raise PAError("Run %d is damaged (meta.json unreadable)" % rid)
        if not isinstance(m, dict):
            raise PAError("Run %d is damaged" % rid)
        return m

    def _seg(self, rid, idx):
        try:
            sg = _load(os.path.join(self._dir(int(rid)), 'seg_%03d.json'
                                    % int(idx)))
        except FileNotFoundError:
            raise PAError("Run %s has no PA value %s" % (rid, idx))
        except (OSError, ValueError, TypeError):
            raise PAError("Run %s, PA value %s is damaged" % (rid, idx))
        if not isinstance(sg, dict) or not isinstance(sg.get('t'), list) \
                or not isinstance(sg.get('f'), list):
            raise PAError("Run %s, PA value %s is damaged" % (rid, idx))
        return sg

    # -- the index (History list) ------------------------------------------
    def _index_path(self):
        return os.path.join(self.root, 'index.json')

    @classmethod
    def _row(cls, m):
        row = {k: m.get(k) for k in ROW_KEYS if k != 'settings'}
        st = m.get('settings') or {}
        row['settings'] = {k: st.get(k) for k in ('mode', 'method', 'fast_flow')
                           if k in st}
        row['segments'] = len(m.get('segments', []))
        r = (m.get('analysis') or {}).get('result') or {}
        row['result'] = {k: r.get(k) for k in ('pa', 'unc', 'grade')} \
            if r.get('pa') is not None else None
        row['stale'] = cls._needs_analysis(m)
        return row

    def _load_index(self):
        if self._index is not None:
            return self._index
        try:
            idx = _load(self._index_path())
            rows = idx.get('runs') if isinstance(idx, dict) else None
            self._index = {int(k): v for k, v in rows.items()} \
                if isinstance(rows, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            self._index = {}
        return self._index

    def _save_index(self):
        try:
            atomic_json(self._index_path(),        # a cache: no fsync
                        {'version': 1, 'runs': {str(k): v for k, v in
                                                self._index.items()}},
                        sync=False)
        except OSError:
            pass                  # only a cache: rebuilt next time

    def _mtime(self, rid):
        try:
            return os.stat(self._meta_path(rid)).st_mtime_ns
        except OSError:
            return None

    def _set_row(self, rid, meta):
        row = self._row(meta)
        row['mtime'] = self._mtime(rid)
        self._load_index()[rid] = row

    def _save_meta(self, rid, meta):
        atomic_json(self._meta_path(rid), meta)
        self._set_row(rid, meta)
        self._save_index()

    def new_run(self, kind, meta):
        rid = max(self._ids(), default=0) + 1
        os.makedirs(self._dir(rid))
        meta = dict(meta, id=rid, kind=kind, name='Run %d' % rid,
                    date=time.strftime('%Y-%m-%d %H:%M:%S'),
                    status='running', error='', segments=[],
                    pa_pick=None, notes='')
        self._save_meta(rid, meta)
        return rid

    def mark_interrupted(self, active=None):
        """At start-up: runs still 'running' were cut off (power, crash,
        restart). Marked 'interrupted' so they never look like they are
        still going. -> number marked"""
        self.list_runs()                       # index covers every folder
        n = 0
        for rid, row in list(self._load_index().items()):
            if row.get('status') != 'running' or rid == active:
                continue
            try:
                m = self._meta(rid)
                if m.get('status') == 'running':
                    m['status'] = 'interrupted'
                    m['error'] = m.get('error') or \
                        'Klipper stopped during this run'
                    self._save_meta(rid, m)
                    n += 1
            except (PAError, OSError):
                continue
        return n

    @staticmethod
    def _opts(meta):
        a = meta.get('analysis') or {}
        return {'skip_first': bool(a.get('skip_first', True))}

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
        sm = meta.get('smooth_time')
        aw = (meta.get('settings') or {}).get('area_window')
        if any(m.get('name') == 'fast' for m in mk):
            edge = {k: analysis.edge_summary(t, f, mk, k,
                                             skip_first=opts['skip_first'])
                    for k in ('rise', 'fall')}
            if edge['rise'] or edge['fall']:
                entry['edge'] = edge
            fit = analysis.command_fit(t, f, mk, self._flows(meta, seg))
            if fit:
                entry['cmd'] = fit
            sc = analysis.edge_score(t, f, mk, sm if sm else 0.04,
                                     opts['skip_first'], area_window=aw)
            if sc:
                entry['piro'] = sc
            # a second sensor recorded alongside (sensor: both): its own
            # scores, same markers, same clock
            for name, ch in (seg.get('channels') or {}).items():
                csc = analysis.edge_score(ch['t'], ch['f'], mk,
                                          sm if sm else 0.04,
                                          opts['skip_first'], area_window=aw)
                if csc:
                    entry.setdefault('channels', {})[name] = {
                        'piro': csc, 'unit': ch.get('unit'),
                        'samples': len(ch['t'])}
        if seg.get('search_note') and not seg.get('stage'):
            entry['stage'] = seg['search_note']        # older search runs
        for k in ('stage', 'repeat', 'sensor', 'dropped'):
            if seg.get(k) is not None:
                entry[k] = seg[k]
        return entry

    @staticmethod
    def _rule(meta):
        st = meta.get('settings') or {}
        mode = st.get('mode') or st.get('pick') or 'edge'
        mode = 'area' if mode == 'fit' else mode
        return mode if mode in analysis.RULES else 'edge'    # unknown: edge

    def _summarize(self, meta, boot=analysis.BOOT):
        opts = self._opts(meta)
        a = dict(meta.get('analysis') or {}, **opts)
        for old in ('picks', 'models', 'model', 'ozn_cross', 'dip_min',
                    'stale'):
            a.pop(old, None)
        if meta.get('kind') == 'test':
            # runs made before the mode setting keep the edge unless
            # re-analysed with another mode
            segs = meta['segments']
            a['result'] = analysis.run_result(segs, self._rule(meta), boot)
            # sensor: both -> the other sensor's answer, for comparison
            names = []
            for e in segs:
                for name in (e.get('channels') or {}):
                    if name not in names:
                        names.append(name)
            other = {}
            for name in names:
                csegs = [dict(e, piro=e['channels'][name]['piro'])
                         for e in segs if name in (e.get('channels') or {})]
                r = analysis.run_result(csegs, self._rule(meta), boot)
                other[name] = {k: r.get(k) for k in (
                    'pa', 'unc', 'grade', 'why', 'method', 'warnings')}
            if other:
                a['channels'] = other
            else:
                a.pop('channels', None)
        a['offset'] = analysis.timing_offset(meta['segments'])
        a['boot'] = boot
        meta['analysis'] = a
        if meta.get('kind') == 'test' and meta.get('settings'):
            fl = self._flows(meta, {})
            if fl:
                meta['flows'] = fl
        return meta

    def add_segment(self, rid, seg):
        """Store one PA value. While the test runs the run's summary uses a
        quick bootstrap; finalize() redoes it in full."""
        meta = self._meta(rid)
        idx = len(meta['segments'])
        seg = dict(seg, samples=len(seg['t']))
        atomic_json(os.path.join(self._dir(rid), 'seg_%03d.json' % idx), seg)
        meta['segments'].append(self._entry(meta, idx, seg))
        self._summarize(meta, QUICK_BOOT if meta.get('kind') == 'test'
                        else analysis.BOOT)
        self._save_meta(rid, meta)
        return idx

    def set_search(self, rid, search):
        meta = self._meta(rid)
        meta['search'] = search
        self._save_meta(rid, meta)

    def update_meta(self, rid, values):
        meta = self._meta(rid)
        meta.update(values)
        self._save_meta(rid, meta)

    def update_auto(self, rid, values):
        """Auto PA record of a run (applied value, conditions, reuse)."""
        self._not_busy(rid)
        meta = self._meta(rid)
        a = dict(meta.get('auto') or {})
        a.update(values)
        meta['auto'] = a
        self._save_meta(rid, meta)

    def last_auto(self):
        """(id, meta) of the newest run made by auto PA, or (None, None)."""
        for row in self.list_runs():
            if row.get('auto'):
                try:
                    return row['id'], self._meta(row['id'])
                except PAError:
                    continue
        return None, None

    def finalize(self, rid, status, error=''):
        """End of a run: status, and the full analysis (in the background)."""
        self.busy.add(rid)
        try:
            meta = self.bg(_finalize_job, (self.root, rid, status, error))
        except Exception:
            try:                         # background failed: here, in full
                meta = _finalize_job(self.root, rid, status, error)
            except Exception:
                meta = self._meta(rid)   # analysis failed: still save status
                meta['status'], meta['error'] = status, error
                atomic_json(self._meta_path(rid), meta)
        finally:
            self.busy.discard(rid)
        self._set_row(rid, meta)
        self._save_index()
        return meta

    def _reanalyze_inline(self, rid, skip_first=None, mode=None):
        meta = self._meta(rid)
        a = dict(meta.get('analysis') or {})
        if skip_first is not None:
            a['skip_first'] = bool(skip_first)
        meta['analysis'] = a
        if mode:
            mode = 'area' if mode == 'fit' else mode
            meta['settings'] = dict(meta.get('settings') or {}, mode=mode)
        segs = []
        for e in meta.get('segments', []):
            seg = self._seg(rid, e['idx'])
            segs.append(self._entry(meta, e['idx'], seg))
        meta['segments'] = segs
        meta['analysed'] = time.strftime('%Y-%m-%d %H:%M:%S')
        self._summarize(meta)
        atomic_json(self._meta_path(rid), meta)
        return meta

    def reanalyze(self, rid, skip_first=None, mode=None):
        """Recompute every per-PA summary and the answer from the raw
        samples (in the background). Raw data is left untouched."""
        if rid in self.busy:
            raise PAError("Run %d is being analysed" % rid)
        self._meta(rid)                          # exists and readable
        self.busy.add(rid)
        try:
            meta = self.bg(_reanalyze_job, (self.root, rid, skip_first, mode))
        except PAError:
            raise
        except Exception as e:
            raise PAError("Analysis of run %d failed: %s" % (rid, e))
        finally:
            self.busy.discard(rid)
        self._set_row(rid, meta)
        self._save_index()
        return meta

    # -- export / import --------------------------------------------------
    def export_run(self, rid):
        meta = self._meta(rid)
        segs = [self._seg(rid, e['idx']) for e in meta.get('segments', [])]
        return {'format': EXPORT_FORMAT, 'version': EXPORT_VERSION,
                'exported': time.strftime('%Y-%m-%d %H:%M:%S'),
                'meta': meta, 'segments': segs}

    def import_run(self, data):
        if not isinstance(data, dict) or data.get('format') != EXPORT_FORMAT:
            raise PAError("Not a run file")
        ver = _num(data.get('version'))
        if ver is None:
            raise PAError("Run file has no valid version")
        if ver > EXPORT_VERSION:
            raise PAError("Run file from a newer version. Update the plugin")
        segs = data.get('segments')
        if not isinstance(segs, list) or not segs or len(segs) > 64:
            raise PAError("Run file incomplete")
        meta = clean_meta(data.get('meta'))
        segs = [clean_segment(sg) for sg in segs]
        rid = max(self._ids(), default=0) + 1
        os.makedirs(self._dir(rid))
        try:
            new = dict(meta)
            new.update({'id': rid, 'segments': [],
                        'kind': meta.get('kind') if meta.get('kind') in
                        ('test', 'manual') else 'test',
                        'imported': {'from_id': meta.get('id'),
                                     'from_date': meta.get('date'),
                                     'at': time.strftime('%Y-%m-%d %H:%M:%S')}})
            nm = str(meta.get('name') or 'Run')
            new['name'] = nm if nm.endswith('(imported)') else nm + ' (imported)'
            if not new.get('status'):
                new['status'] = 'complete'
            elif new['status'] == 'running':
                new['status'] = 'cancelled'
            for i, sg in enumerate(segs):
                atomic_json(os.path.join(self._dir(rid), 'seg_%03d.json' % i),
                            dict(sg, samples=len(sg['t'])))
                new['segments'].append({'idx': i})
            self._save_meta(rid, new)
            self.reanalyze(rid)
        except Exception:
            shutil.rmtree(self._dir(rid), ignore_errors=True)
            self._load_index().pop(rid, None)
            self._save_index()
            raise
        return rid

    @staticmethod
    def _needs_analysis(meta):
        """Made by an older version: no stored answer, no PIRO-AREA pulse
        data or no fall result -> re-analysed once when opened."""
        if meta.get('kind') != 'test' or meta.get('status') == 'running' \
                or not meta.get('segments'):
            return False
        a = meta.get('analysis') or {}
        if 'area_fall' not in (a.get('result') or {}) or \
                a.get('boot', 500) < 500:            # quick summary only
            return True
        return any(('piro' not in e or 'area' not in e['piro'])
                   for e in meta['segments'] if not e.get('partial'))

    def list_runs(self):
        """History rows, newest first, from the index (fast). Run folders the
        index does not know are read once (no analysis) and added."""
        idx = self._load_index()
        ids = self._ids()
        known = set(ids)
        changed = False
        for rid in ids:
            if rid not in idx or idx[rid].get('mtime') != self._mtime(rid):
                try:
                    self._set_row(rid, self._meta(rid))
                except PAError:
                    idx.pop(rid, None)
                    continue
                changed = True
        for rid in [r for r in idx if r not in known]:
            del idx[rid]
            changed = True
        if changed:
            self._save_index()
        return [dict(idx[r]) for r in sorted(idx, reverse=True)]

    def get_run(self, rid, analyse=True):
        meta = self._meta(rid)
        if analyse and self._needs_analysis(meta) and rid not in self.busy:
            # a run from an older version: re-analyse it once (raw samples
            # are not changed)
            try:
                return self.reanalyze(rid)
            except PAError:
                pass
        return meta

    def get_segment(self, rid, idx, max_points=0, t0=None, t1=None):
        """One segment. t0/t1 (seconds, segment time) return only that
        stretch, so the History graph can load full detail for the part
        on screen; max_points then thins it with min/max buckets."""
        seg = self._seg(rid, idx)
        t, f = seg['t'], seg['f']
        seg['span'] = [t[0], t[-1]] if t else [0.0, 0.0]
        if t0 is not None or t1 is not None:
            lo = bisect.bisect_left(t, t0) if t0 is not None else 0
            hi = bisect.bisect_right(t, t1) if t1 is not None else len(t)
            t, f = t[lo:hi], f[lo:hi]
            seg['range'] = [t0, t1]
        seg['t'], seg['f'] = decimate(t, f, max_points)
        seg['decimated'] = len(seg['t']) < len(t)
        for ch in (seg.get('channels') or {}).values():
            ct, cf = ch['t'], ch['f']
            if t0 is not None or t1 is not None:
                a = bisect.bisect_left(ct, t0) if t0 is not None else 0
                b = bisect.bisect_right(ct, t1) if t1 is not None else len(ct)
                ct, cf = ct[a:b], cf[a:b]
            ch['t'], ch['f'] = decimate(ct, cf, max_points)
        return seg

    def _not_busy(self, rid):
        if rid in self.busy:
            raise PAError("Run %d is being analysed, try again in a moment"
                          % rid)

    def set_tag(self, rid, pa_pick=None, notes=None, name=None):
        self._not_busy(rid)
        m = self._meta(rid)
        if pa_pick is not None:
            m['pa_pick'] = pa_pick if pa_pick != '' else None
        if notes is not None:
            m['notes'] = str(notes)[:2000]
        if name:
            m['name'] = str(name)[:120]
        self._save_meta(rid, m)

    def delete(self, rid):
        if rid in self.busy:
            raise PAError("Run %d is being analysed" % rid)
        shutil.rmtree(self._dir(rid), ignore_errors=True)
        if self._load_index().pop(rid, None) is not None:
            self._save_index()


# -- work done in the background process (module level: picklable) -------
def _reanalyze_job(root, rid, skip_first, mode):
    return RunStore(root)._reanalyze_inline(rid, skip_first, mode)


def _finalize_job(root, rid, status, error):
    st = RunStore(root)
    meta = st._meta(rid)
    meta['status'], meta['error'] = status, error
    st._summarize(meta)
    atomic_json(st._meta_path(rid), meta)
    return meta
