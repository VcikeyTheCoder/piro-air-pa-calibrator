"""Custom page themes, stored on the printer so every browser sees them:
  <data_dir>/themes/<id>.json
A theme picks one of the built-in layouts and overrides colours. Values are
checked so a theme file can only change colours, sizes and fonts."""
import json
import os
import re
from .errors import PAError

FORMAT = 'piro_edge_theme'
LAYOUTS = ('piro', 'mainsail', 'fluidd', 'ratos', 'cat', 'terminal')
TOKENS = ('bg', 'panel', 'panel2', 'head', 'ph', 'rule', 'text', 'dim',
          'trace', 'accent', 'onaccent', 'bad', 'good', 'fair', 'grid',
          'fast', 'slow', 'radius', 'font', 'cmd')
_VALUE = re.compile(r'^[#a-zA-Z0-9().,%"\' -]{1,80}$')
MAX_BYTES = 16 * 1024


def _slug(s):
    s = re.sub(r'[^a-z0-9]+', '-', str(s).lower()).strip('-')
    return s[:40] or 'theme'


def clean(obj):
    """Validated copy of a theme dict, or PAError."""
    if not isinstance(obj, dict):
        raise PAError("Not a theme file")
    if obj.get('format', FORMAT) != FORMAT:
        raise PAError("Not a PIRO-EDGE theme file")
    name = str(obj.get('name') or '').strip()[:40]
    if not name:
        raise PAError("Theme name missing")
    base = obj.get('base', 'piro')
    if base not in LAYOUTS:
        raise PAError("base must be one of: %s" % ', '.join(LAYOUTS))
    mode = obj.get('mode', 'dark')
    if mode not in ('dark', 'light'):
        raise PAError("mode must be dark or light")
    toks = obj.get('tokens') or {}
    if not isinstance(toks, dict):
        raise PAError("tokens must be an object")
    out = {}
    for k, v in toks.items():
        if k not in TOKENS:
            raise PAError("Unknown token '%s'" % k)
        v = str(v).strip()
        low = v.lower()
        if not _VALUE.match(v) or 'url' in low or 'expression' in low:
            raise PAError("Token '%s' value not allowed" % k)
        out[k] = v
    return {'format': FORMAT, 'version': 1, 'id': _slug(obj.get('id') or name),
            'name': name, 'base': base, 'mode': mode,
            'author': str(obj.get('author') or '')[:40], 'tokens': out}


class ThemeStore:
    def __init__(self, root):
        self.dir = os.path.join(os.path.expanduser(root), 'themes')
        os.makedirs(self.dir, exist_ok=True)

    def list(self):
        out = []
        for n in sorted(os.listdir(self.dir)):
            if not n.endswith('.json'):
                continue
            try:
                with open(os.path.join(self.dir, n)) as f:
                    out.append(clean(json.load(f)))
            except (OSError, ValueError, PAError):
                continue
        return out

    def save(self, obj):
        if len(json.dumps(obj)) > MAX_BYTES:
            raise PAError("Theme file too large")
        t = clean(obj)
        if t['id'] in LAYOUTS:
            t['id'] += '-custom'
        path = os.path.join(self.dir, t['id'] + '.json')
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(t, f, indent=1)
        os.replace(tmp, path)
        return t

    def delete(self, tid):
        tid = _slug(tid)
        p = os.path.join(self.dir, tid + '.json')
        if os.path.isfile(p):
            os.remove(p)
