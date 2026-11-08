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
# Optional env overrides: KLIPPER_DIR KLIPPY_ENV CONFIG_DIR UI_DIR NGINX_SITE KLIPPER_SERVICE
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
  # Kalico's tap analysis (copied into PIRO-TAP) needs numpy
  PY_ENV="${KLIPPY_ENV:-$HOME/klippy-env}"
  if [ -x "$PY_ENV/bin/python" ]; then
    if ! "$PY_ENV/bin/python" -c "import numpy" 2>/dev/null; then
      say "Installing numpy into $PY_ENV (needed by PIRO-TAP)"
      "$PY_ENV/bin/pip" install -q numpy || warn "numpy install failed: run  $PY_ENV/bin/pip install numpy"
    fi
  else
    warn "Klipper's Python not found at $PY_ENV: make sure numpy is installed in it (set KLIPPY_ENV)"
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
                'method': ('klipper',), 'mode': ('fit', 'area')}
# 'mode: area' was the default only with the old comment: a value you chose
# later (Settings writes the new comment) is never changed again
ONLY_WITH = {'mode': ('algorithm judging the force curve', '')}
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
            if k in ONLY_WITH and m.group(1) == 'area' and \
                    ONLY_WITH[k][0] not in l:
                continue
            lines[i] = newl[k]; moved.append(k)
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
    # the old default rule (area) moves to the new default (fall); a mode
    # you changed to edge stays
    if grep -qE '^variable_mode:[[:space:]]*"area"[[:space:]]*# area = PIRO-AREA, edge = PIRO-EDGE' "$AUTO"; then
      cp "$AUTO" "$AUTO.bak-pa_loadcell"
      sed -i -E 's/^variable_mode:[[:space:]]*"area"[[:space:]]*# area = PIRO-AREA, edge = PIRO-EDGE.*/variable_mode: "fall"           # fall = slow-down area (default), area = PIRO-AREA, edge = PIRO-EDGE/' "$AUTO"
      echo "    mode area -> fall in your $AUTO (backup: pa_auto.cfg.bak-pa_loadcell)"
    fi
    echo "    kept your $AUTO (the latest version is $REPO/klipper/pa_auto.cfg)"
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
  3. Home, then  PIRO_TAP_CALIBRATE MODE=TAP_PERFORMANCE  and  SAVE_CONFIG
MSG
fi
echo "  Page: http://$H.local/loadcell/  (Mainsail sidebar, after a browser refresh)"
