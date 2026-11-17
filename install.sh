#!/usr/bin/env bash
# PIRO installer: the PA calibrator (PIRO-EDGE), the PIRO-TAP probe, or both.
# Run from the repo folder:
#   ./install.sh              install or update (asks which part the first
#                             time; later runs keep your choice)
#   ./install.sh --pa         PA calibrator only
#   ./install.sh --probe      PIRO-TAP probe only
#   ./install.sh --both       both
#   ./install.sh --uninstall  remove everything
# The Klipper module and the web page are installed as symlinks into this
# repo folder, so `git pull` (or the Update button in Mainsail's Machine
# page) updates everything. Keep the repo folder where it is.
# Optional env overrides: KLIPPER_DIR CONFIG_DIR UI_DIR NGINX_SITE KLIPPER_SERVICE
#                         MOONRAKER_CONF NO_RESTART=1 (skip service restarts)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
UI_DIR="${UI_DIR:-$HOME/pa_loadcell_ui}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/mainsail}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-}"
MOONRAKER_CONF="${MOONRAKER_CONF:-$CONFIG_DIR/moonraker.conf}"
SUDO="${SUDO-sudo}"; [ "$(id -u)" -eq 0 ] && SUDO=""
EXTRAS="$KLIPPER_DIR/klippy/extras"
MARK="# pa_loadcell"
RESTART_MOONRAKER=0
MODE_FILE="$REPO/.install_mode"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

# First service that exists. Avoids "systemctl | grep -q" (SIGPIPE + pipefail
# makes that report "not found" even when the service exists).
find_service() {
  local n
  for n in "$@"; do
    [ -n "$n" ] && systemctl cat "$n.service" >/dev/null 2>&1 && { echo "$n"; return 0; }
  done
  return 1
}
klipper_service() {
  local n list
  n="$(find_service "$KLIPPER_SERVICE" klipper)" && { echo "$n"; return 0; }
  list="$(systemctl list-unit-files --no-legend 'klipper*.service' 2>/dev/null || true)"
  n="$(echo "$list" | awk '$1 ~ /^klipper.*\.service$/ && $1 !~ /mcu/ {sub(/\.service$/,"",$1); print $1; exit}')"
  [ -n "$n" ] && { echo "$n"; return 0; }
  return 1
}

restart_services() {
  [ "${NO_RESTART:-0}" = 1 ] && { warn "NO_RESTART=1: restart Klipper, Moonraker and nginx manually"; return; }
  if [ -f "$NGINX_SITE" ] && command -v nginx >/dev/null; then
    if $SUDO nginx -t >/dev/null 2>&1; then $SUDO systemctl reload nginx; else warn "nginx -t failed, nginx not reloaded"; fi
  fi
  local svc
  if [ "$RESTART_MOONRAKER" = 1 ] && svc="$(find_service moonraker)"; then
    say "Restarting $svc"; $SUDO systemctl restart "$svc" || warn "Restart failed: $svc"
  fi
  if svc="$(klipper_service)"; then
    say "Restarting $svc"; $SUDO systemctl restart "$svc" || warn "Restart failed: $svc"
  else warn "Klipper service not found. Run FIRMWARE_RESTART"; fi
}

nginx_remove() {
  [ -f "$NGINX_SITE" ] && grep -q "$MARK" "$NGINX_SITE" || return 0
  $SUDO cp "$NGINX_SITE" "$NGINX_SITE.bak-pa_loadcell"
  $SUDO sed -i "/$MARK begin/,/$MARK end/d" "$NGINX_SITE"
}

# Is [name] (or [name ...]) active (not commented) in any config file?
active_section() {
  grep -rqE "^\[$1( [^]]*)?\]" "$CONFIG_DIR" --include='*.cfg' 2>/dev/null
}
# include line in printer.cfg, never below the SAVE_CONFIG block
add_include() {
  local PCFG="$CONFIG_DIR/printer.cfg" INC="$1"
  if grep -qx "\[include $INC\]" "$PCFG"; then echo "    [include $INC] already there"; return; fi
  [ -f "$PCFG.bak-pa_loadcell" ] || cp "$PCFG" "$PCFG.bak-pa_loadcell"
  python3 - "$PCFG" "$INC" <<'PY'
import re, sys
p, name = sys.argv[1], sys.argv[2]
lines = open(p).read().split('\n')
inc = '[include %s]' % name
for i, l in enumerate(lines):
    if re.match(r'#\*# <-+ SAVE_CONFIG', l):
        lines[i:i] = [inc, '']; break
else:
    lines += ['', inc, '']
open(p, 'w').write('\n'.join(lines))
PY
  echo "    added [include $INC]"
}
remove_include() {
  [ -f "$CONFIG_DIR/printer.cfg" ] && sed -i "/^\[include $1\]\$/d" "$CONFIG_DIR/printer.cfg" || true
}
link() {   # link <repo path> <extras name>
  rm -rf "$EXTRAS/$2"
  ln -s "$REPO/$1" "$EXTRAS/$2" || cp -r "$REPO/$1" "$EXTRAS/$2"
  if [ -d "$KLIPPER_DIR/.git" ] && ! grep -qx "klippy/extras/$2" "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null; then
    echo "klippy/extras/$2" >> "$KLIPPER_DIR/.git/info/exclude"   # keep Klipper's git clean
  fi
}

MODE=""
case "${1:-}" in
  --pa) MODE=pa ;; --probe) MODE=probe ;; --both) MODE=both ;;
  --uninstall|"") ;;
  *) die "Unknown option ${1}. Use --pa, --probe, --both or --uninstall" ;;
esac

if [ "${1:-}" = "--uninstall" ]; then
  say "Uninstalling"
  if active_section piro_tap; then
    die "printer config still has an active [piro_tap] section. Comment it out (and restore your Z endstop) first"
  fi
  rm -rf "$EXTRAS/pa_loadcell" "$EXTRAS/pa_eddy.py" "$EXTRAS/piro_tap" "$EXTRAS/piro_eddy.py" "$UI_DIR" "$MODE_FILE"
  [ -f "$CONFIG_DIR/printer.cfg" ] && sed -i -e '/^\[include pa_loadcell.cfg\]$/d' -e '/^\[include pa_auto.cfg\]$/d' -e '/^\[include piro_tap.cfg\]$/d' "$CONFIG_DIR/printer.cfg"
  python3 - "$CONFIG_DIR/.theme/navi.json" <<'PY' 2>/dev/null || true
import json, sys
p = sys.argv[1]; items = json.load(open(p))
items = [i for i in items if i.get('href') != '/loadcell/']
json.dump(items, open(p, 'w'), indent=2)
PY
  nginx_remove
  if [ -f "$MOONRAKER_CONF" ] && grep -q "$MARK begin" "$MOONRAKER_CONF"; then
    cp "$MOONRAKER_CONF" "$MOONRAKER_CONF.bak-pa_loadcell"
    sed -i "/$MARK begin/,/$MARK end/d" "$MOONRAKER_CONF"; RESTART_MOONRAKER=1
  fi
  restart_services
  say "Done. Kept $CONFIG_DIR/pa_loadcell.cfg, pa_auto.cfg, piro_tap.cfg, ~/printer_data/pa_loadcell and ~/printer_data/piro_tap"
  exit 0
fi

[ -d "$EXTRAS" ] || die "Klipper extras folder not found: $EXTRAS (set KLIPPER_DIR)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "printer.cfg not found in $CONFIG_DIR (set CONFIG_DIR)"

if [ -z "$MODE" ] && [ -f "$MODE_FILE" ]; then MODE="$(cat "$MODE_FILE")"; fi
if [ -z "$MODE" ]; then
  if [ -t 0 ]; then
    echo "What do you want to install?"
    echo "  1) PA calibrator (PIRO-EDGE)"
    echo "  2) PIRO-TAP probe (BTT Eddy nozzle probe)"
    echo "  3) Both"
    read -r -p "Choice [1-3]: " C
    case "$C" in 1) MODE=pa ;; 2) MODE=probe ;; 3) MODE=both ;; *) die "Choose 1, 2 or 3" ;; esac
  else
    MODE=pa       # updates of older installs (they had only the PA part)
  fi
fi
case "$MODE" in pa|probe|both) ;; *) die "Bad install mode '$MODE' in $MODE_FILE" ;; esac
WANT_PA=0; WANT_PROBE=0
[ "$MODE" != probe ] && WANT_PA=1
[ "$MODE" != pa ] && WANT_PROBE=1
say "Install: $( [ $WANT_PA = 1 ] && echo -n 'PA calibrator ' )$( [ $WANT_PROBE = 1 ] && echo -n 'PIRO-TAP probe' )"

# a part that is still configured cannot be removed (Klipper would not start)
if [ "$WANT_PROBE" = 0 ] && active_section piro_tap; then
  die "[piro_tap] is active in your config: comment it out first, or install with --probe / --both"
fi
if [ "$WANT_PA" = 0 ] && { active_section pa_loadcell && ! grep -qx '\[include pa_loadcell.cfg\]' "$CONFIG_DIR/printer.cfg"; } ; then
  die "[pa_loadcell] is active in your config outside pa_loadcell.cfg: remove it first, or install with --pa / --both"
fi
if [ "$WANT_PA" = 0 ] && active_section pa_eddy; then
  die "[pa_eddy] is active in your config: comment it out first, or install with --pa / --both"
fi

say "Linking Klipper modules"
link shared/piro_eddy.py piro_eddy.py        # the Eddy hub, used by both parts
if [ "$WANT_PA" = 1 ]; then
  link klipper/pa_loadcell pa_loadcell
  link klipper/pa_eddy.py pa_eddy.py           # [pa_eddy]: Eddy as PA sensor only
else
  rm -rf "$EXTRAS/pa_loadcell" "$EXTRAS/pa_eddy.py"
fi
if [ "$WANT_PROBE" = 1 ]; then
  link piro_tap/klipper/piro_tap piro_tap
  if ! grep -q "setup_trigger_analog" "$EXTRAS/ldc1612.py" 2>/dev/null; then
    warn "This Klipper is too old for PIRO-TAP (needs v0.13.0-496 or newer): update Klipper before enabling [piro_tap]"
  fi
else
  rm -rf "$EXTRAS/piro_tap"
fi
echo "$MODE" > "$MODE_FILE"

if [ "$WANT_PROBE" = 1 ]; then
  say "Installing PIRO-TAP config"
  if [ -f "$CONFIG_DIR/piro_tap.cfg" ]; then echo "    kept your piro_tap.cfg (latest: $REPO/piro_tap/piro_tap.cfg)"
  else cp "$REPO/piro_tap/piro_tap.cfg" "$CONFIG_DIR/piro_tap.cfg"; echo "    $CONFIG_DIR/piro_tap.cfg ([piro_tap] commented out: see the notes in it)"; fi
  add_include piro_tap.cfg
else
  remove_include piro_tap.cfg
fi

if [ "$WANT_PA" = 1 ]; then
say "Installing config"
CFG="$CONFIG_DIR/pa_loadcell.cfg"
if [ -f "$CFG" ]; then
  say "Updating $CFG (existing values kept)"
  cp "$CFG" "$CFG.bak-pa_loadcell"
  python3 - "$CFG" "$REPO/klipper/pa_loadcell.cfg" <<'PY'
import re, sys
cfg, new = sys.argv[1], sys.argv[2]
lines = open(cfg).read().split('\n')
OBSOLETE = ('sweeps', 'extrude_len', 'slow_speed', 'fast_speed', 'settle', 'warmup',
            'pa_mode', 'pa_start', 'pa_end', 'pa_step', 'wobble',
            'wobble_axis', 'accel', 'model')
# earlier defaults: a value nobody changed moves to the PIRO-EDGE default
OLD_DEFAULTS = {'cycles': ('3', '4', '6'), 'prime_steady': ('0',), 'fast_flow': ('19.24',), 'fast_time': ('0.25',),
                'slow_flow': ('1.92',), 'prime_length': ('40',), 'prime_flow': ('5',),
                'prime_rest': ('0',), 'rest_time': ('0',), 'pa_max': ('0.08',),
                'max_filament': ('400',),
                'method': ('klipper',), 'mode': ('fit', 'area', 'fall')}
# an old mode default only moves with its old comment: a value you chose
# later (Settings keeps the comment of the line) is kept
ONLY_WITH = {('mode', 'area'): 'algorithm judging the force curve',
             ('mode', 'fall'): 'rule judging the force curve: fall = slow-down area is 0'}
# comments that changed: the value is kept, the comment updated
NEW_COMMENT = {'max_values': '# area: most PA values one search tests'}
key = lambda l: (re.match(r'^([a-z_]+)\s*:', l) or [None, None])[1]
RENAMED = {'pa_method': 'method', 'pick': 'mode'}   # older option names
for i, l in enumerate(lines):
    k = key(l)
    if k in RENAMED and not any(key(x) == RENAMED[k] for x in lines):
        lines[i] = RENAMED[k] + l[len(k):]
have = set()
for i, l in enumerate(lines):
    k = key(l)
    if k in OBSOLETE or (k == 'method' and not re.match(
            r'^method\s*:\s*(push|klipper)\b', l)):     # 'method' of old versions
        lines[i] = '# no longer used: ' + l
    elif k:
        have.add(k)
newl = {key(l): l for l in open(new).read().split('\n') if key(l)}
moved = []
for k, olds in OLD_DEFAULTS.items():              # untouched old default -> new
    for i, l in enumerate(lines):
        m = re.match(r'^%s\s*:\s*([^\s#]+)' % k, l)
        if key(l) == k and m and m.group(1) in olds and k in newl and newl[k] != l:
            need = ONLY_WITH.get((k, m.group(1)))
            if need is not None and need not in l:
                continue
            lines[i] = newl[k]; moved.append(k)
for k, old in NEW_COMMENT.items():
    for i, l in enumerate(lines):
        if key(l) == k and old in l and k in newl:
            val = re.match(r'^[a-z_]+\s*:\s*([^\s#]*)', l).group(1)
            nl = newl[k]
            c = nl.index('#') if '#' in nl else len(nl)
            head = '%s: %s' % (k, val)
            lines[i] = head + ' ' * max(1, c - len(head)) + nl[c:]
add = [newl[k] for k in newl if k not in have]
if add:
    # at the end of the [pa_loadcell] section, never below another section
    # (a [pa_eddy] section may follow it in the same file)
    start = next((i for i, l in enumerate(lines)
                  if re.match(r'^\s*\[pa_loadcell\]', l)), None)
    end = len(lines)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r'^\s*#?\s*\[[a-z_ ]+\]', lines[i])), len(lines))
    at = end
    while at > (start or 0) + 1 and lines[at - 1].strip() == '':
        at -= 1
    lines[at:at] = ['# added by install.sh'] + add
open(cfg, 'w').write('\n'.join(lines))
print('    added: %s' % (', '.join(key(l) for l in add) or 'none'))
print('    updated to new defaults: %s' % (', '.join(moved) or 'none'))
PY
else
  cp "$REPO/klipper/pa_loadcell.cfg" "$CFG"
  LC_SEC="$(grep -rhE '^\[load_cell(_probe)?( [^]]+)?\]' "$CONFIG_DIR" --include='*.cfg' 2>/dev/null | head -n1 | tr -d '[]' || true)"
  if [ -n "$LC_SEC" ]; then
    sed -i "s|^load_cell:.*|load_cell: $LC_SEC|" "$CFG"; say "Load cell section: [$LC_SEC]"
  else
    warn "No [load_cell] section in $CONFIG_DIR. Set load_cell: in pa_loadcell.cfg"
  fi
fi

say "Installing auto PA macro"
AUTO="$CONFIG_DIR/pa_auto.cfg"
if [ -f "$AUTO" ]; then
  if cmp -s "$AUTO" "$REPO/klipper/pa_auto.cfg"; then
    echo "    up to date"
  else
    # a newer pa_auto.cfg: the new file with every variable_ value of yours
    # (position, flick, push, ...) carried over; the old one is backed up
    python3 - "$AUTO" "$REPO/klipper/pa_auto.cfg" <<'PY'
import re, shutil, sys, time
mine_p, new_p = sys.argv[1], sys.argv[2]
mine, new = open(mine_p).read().split('\n'), open(new_p).read().split('\n')
ver = lambda ls: max([int(m.group(1)) for m in (re.match(r'^# pa_auto version:\s*(\d+)', l) for l in ls) if m] or [1])
if ver(mine) >= ver(new):
    print('    kept your %s (version %d)' % (mine_p, ver(mine))); sys.exit(0)
SEC = re.compile(r'^\[gcode_macro\s+([^\]]+)\]')
VAR = re.compile(r'^variable_([a-z0-9_]+)\s*:\s*(.*?)(\s+#.*)?$')
def sections(ls):
    out, cur = {}, None
    for i, l in enumerate(ls):
        m = SEC.match(l)
        if m:
            cur = m.group(1).strip(); out[cur] = []
        elif cur and l.startswith('variable_'):
            v = VAR.match(l)
            if v:
                out[cur].append((v.group(1), v.group(2).strip(), v.group(3) or '', i))
    return out
ms, ns = sections(mine), sections(new)
# old defaults that move to the new default: mode fall (and area with
# its old comment) -> edge
MOVE = {('PA_AUTO_RUN', 'mode'): lambda val, c: val in ('"fall"',) or
        (val == '"area"' and 'area = PIRO-AREA, edge = PIRO-EDGE' in c)}
kept, moved, extra = [], [], []
for sec, vars_ in ns.items():
    mine_v = {k: (v, c) for k, v, c, _ in ms.get(sec, [])}
    last = None
    for k, v, c, i in vars_:
        last = i
        if k not in mine_v:
            continue
        mv, mc = mine_v[k]
        if MOVE.get((sec, k), lambda a, b: False)(mv, mc):
            moved.append('%s %s' % (k, mv)); continue
        if mv != v:
            head = 'variable_%s: %s' % (k, mv)
            col = len(new[i]) - len(c.lstrip()) if c else 0
            new[i] = head + (' ' * max(1, col - len(head)) + c.strip() if c else '')
            kept.append(k)
    # your own variables the new file does not have: kept in the same macro
    own = [(k, v, c) for k, v, c, _ in ms.get(sec, []) if k not in {x[0] for x in vars_}]
    if own and last is not None:
        new[last + 1:last + 1] = ['variable_%s: %s%s' % (k, v, c) for k, v, c in own]
        extra += [k for k, _, _ in own]
        for s2, vs in ns.items():        # line numbers below moved down
            ns[s2] = [(a, b, cc, j + len(own) if j > last else j) for a, b, cc, j in vs]
# macros of yours that are not in the new file: added at the end
text_m = '\n'.join(mine)
for sec in ms:
    if sec not in ns:
        blk = re.search(r'^\[gcode_macro\s+%s\].*?(?=^\[|\Z)' % re.escape(sec), text_m, re.M | re.S)
        if blk:
            new += ['', '# kept from your old pa_auto.cfg', blk.group(0).rstrip()]
            extra.append(sec)
bak = '%s.bak-%s' % (mine_p, time.strftime('%Y%m%d-%H%M%S'))
shutil.copy(mine_p, bak)
out = '\n'.join(new)
open(mine_p, 'w').write(out if out.endswith('\n') else out + '\n')
print('    updated %s to version %d, backup %s' % (mine_p, ver(new), bak))
print('    your values kept: %s' % (', '.join(kept) or 'none (all defaults)'))
if moved:
    print('    moved to the new default: %s -> edge' % ', '.join(moved))
if extra:
    print('    your extra variables / macros kept: %s' % ', '.join(extra))
print('    gcode: parts are the new version; copy your own edits over from the backup')
PY
  fi
else
  cp "$REPO/klipper/pa_auto.cfg" "$AUTO"; echo "    $AUTO"
fi

say "Adding includes to printer.cfg"
add_include pa_loadcell.cfg
add_include pa_auto.cfg
else
  remove_include pa_loadcell.cfg
  remove_include pa_auto.cfg
fi   # PA part

say "Linking web page: $UI_DIR"
rm -rf "$UI_DIR"
ln -s "$REPO/ui" "$UI_DIR" || { mkdir -p "$UI_DIR"; cp "$REPO/ui/index.html" "$REPO/ui/mini.html" "$UI_DIR/"; }

say "Adding the page to the Mainsail sidebar"
python3 - "$CONFIG_DIR/.theme/navi.json" "$MODE" <<'PY'
import json, os, sys
p = sys.argv[1]; os.makedirs(os.path.dirname(p), exist_ok=True)
try: items = json.load(open(p))
except (OSError, ValueError): items = []
if not isinstance(items, list): items = []
items = [i for i in items if i.get('href') != '/loadcell/']      # idempotent
title = "PIRO-TAP" if sys.argv[2] == "probe" else "Load Cell"
items.append({"title": title, "href": "/loadcell/", "target": "_self",
              "position": 85,
              "icon": "M16,11.78L20.24,4.45L21.97,5.45L16.74,14.5L10.23,10.75L5.46,19H22V21H2V3H4V17.54L9.5,8L16,11.78Z"})
json.dump(items, open(p, 'w'), indent=2)
PY

if [ -f "$MOONRAKER_CONF" ]; then
  if grep -q "$MARK begin" "$MOONRAKER_CONF"; then echo "    update manager already configured"
  else
    say "Adding Moonraker update entry"
    cp "$MOONRAKER_CONF" "$MOONRAKER_CONF.bak-pa_loadcell"
    ORIGIN="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
    if [ -n "$ORIGIN" ]; then
      printf '\n%s begin\n[update_manager pa_loadcell]\ntype: git_repo\npath: %s\norigin: %s\nprimary_branch: main\nmanaged_services: klipper\n%s end\n' \
        "$MARK" "$REPO" "$ORIGIN" "$MARK" >> "$MOONRAKER_CONF"
      RESTART_MOONRAKER=1
    else warn "No git origin. Update entry skipped"; fi
  fi
fi

if [ -f "$NGINX_SITE" ]; then
  if grep -q "$MARK" "$NGINX_SITE"; then echo "    nginx already configured"
  else
    say "Configuring nginx (/loadcell/)"
    $SUDO cp "$NGINX_SITE" "$NGINX_SITE.bak-pa_loadcell"
    $SUDO sed -i "0,/^[[:space:]]*server[[:space:]]*{/s||&\n    $MARK begin\n    location /loadcell/ { alias $UI_DIR/; index index.html; }\n    $MARK end|" "$NGINX_SITE"
  fi
else
  warn "nginx site $NGINX_SITE not found. Serve $UI_DIR manually"
fi

restart_services
H="$(hostname)"
echo
echo "Installed ($MODE). Next:"
if [ "$WANT_PA" = 1 ]; then cat <<MSG
  PA calibrator:
  1. Edit $CFG  (park_x/y and test_x/y for your bed)
  2. In the Mainsail console run:  PA_LOADCELL_DIAG
  3. Auto PA before every print: add  PA_AUTO {rawparams}  to PRINT_START, see $AUTO
MSG
fi
if [ "$WANT_PROBE" = 1 ]; then cat <<MSG
  PIRO-TAP probe:
  1. Read and edit $CONFIG_DIR/piro_tap.cfg, remove your other probe, uncomment [piro_tap]
  2. FIRMWARE_RESTART, then  PIRO_TAP_CALIBRATE MODE=THRESHOLD  and  SAVE_CONFIG
MSG
fi
echo "  Page: http://$H.local/loadcell/  (Mainsail sidebar, after a browser refresh)"
