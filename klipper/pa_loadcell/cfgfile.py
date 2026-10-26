"""Saving test defaults from the page into pa_loadcell.cfg.

Only the [pa_loadcell] section is touched, only the keys in EDITABLE, and
inline comments are kept. Safety limits (min_temp, required_z, ...) and
storage options are never written from the page."""
import os
import re
from .errors import PAError

# page / Settings field -> config key
EDITABLE = {
    'pa_max': 'pa_max', 'upper_step': 'upper_step', 'pa_limit': 'pa_limit',
    'cycles': 'cycles', 'fast_flow': 'fast_flow', 'fast_time': 'fast_time',
    'slow_flow': 'slow_flow', 'slow_time': 'slow_time',
    'prime_length': 'prime_length', 'prime_flow': 'prime_flow',
    'prime_rest': 'prime_rest', 'rest_time': 'rest_time',
    'max_filament': 'max_filament', 'skip_first': 'skip_first_pulse',
    'park_x': 'park_x', 'park_y': 'park_y', 'test_x': 'test_x',
    'test_y': 'test_y', 'filament': 'filament'}

_SECTION = re.compile(r'^\s*\[([^\]]+)\]')


def _fmt(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, float):
        return ('%.6f' % v).rstrip('0').rstrip('.') or '0'
    s = str(v)
    if '\n' in s or '#' in s or ';' in s:
        raise PAError("Text values cannot contain # ; or line breaks")
    return s


def update_text(text, values):
    """Return the config text with `values` ({config_key: value}) written
    into [pa_loadcell]. Existing lines keep their inline comment; missing
    keys are added at the end of the section."""
    lines = text.split('\n')
    start = end = None
    for i, l in enumerate(lines):
        m = _SECTION.match(l)
        if m:
            if m.group(1).strip() == 'pa_loadcell':
                start = i
            elif start is not None and end is None:
                end = i
    if start is None:
        raise PAError("No [pa_loadcell] section in the config file")
    if end is None:
        end = len(lines)
    todo = dict(values)
    for i in range(start + 1, end):
        m = re.match(r'^([a-z_]+)(\s*[:=]\s*)([^#;]*?)(\s*[#;].*)?$', lines[i])
        if m and m.group(1) in todo:
            new = m.group(1) + ': ' + _fmt(todo.pop(m.group(1)))
            comment = (m.group(4) or '').strip()
            if comment:          # keep the comment in the same column
                col = len(m.group(1)) + len(m.group(2)) + len(m.group(3)) + \
                    len(m.group(4)) - len(m.group(4).lstrip())
                new += ' ' * max(1, col - len(new)) + comment
            lines[i] = new
    if todo:
        at = end
        while at > start + 1 and lines[at - 1].strip() == '':
            at -= 1
        lines[at:at] = ['%s: %s' % (k, _fmt(v)) for k, v in todo.items()]
    return '\n'.join(lines)


def config_path(printer, override=None):
    if override:
        return os.path.expanduser(override)
    try:
        main = printer.get_start_args().get('config_file')
    except Exception:
        main = None
    if not main:
        raise PAError("Printer config folder not found")
    return os.path.join(os.path.dirname(main), 'pa_loadcell.cfg')


def save(path, values):
    """Write {config_key: value} into the file (atomic, one backup)."""
    if not os.path.isfile(path):
        raise PAError("%s not found" % path)
    with open(path) as f:
        text = f.read()
    new = update_text(text, values)
    bak = path + '.bak-pa_loadcell-ui'
    with open(bak, 'w') as f:
        f.write(text)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(new)
    os.replace(tmp, path)
    return path
