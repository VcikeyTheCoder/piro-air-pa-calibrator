#!/usr/bin/env bash
# PA Load Cell Analyzer installer. Run from the repo folder:
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove
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

if [ "${1:-}" = "--uninstall" ]; then
  say "Uninstalling"
  rm -rf "$EXTRAS/pa_loadcell" "$UI_DIR"
  [ -f "$CONFIG_DIR/printer.cfg" ] && sed -i -e '/^\[include pa_loadcell.cfg\]$/d' -e '/^\[include pa_auto.cfg\]$/d' "$CONFIG_DIR/printer.cfg"
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
  say "Done. Kept $CONFIG_DIR/pa_loadcell.cfg, pa_auto.cfg and ~/printer_data/pa_loadcell"
  exit 0
fi

[ -d "$EXTRAS" ] || die "Klipper extras folder not found: $EXTRAS (set KLIPPER_DIR)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "printer.cfg not found in $CONFIG_DIR (set CONFIG_DIR)"

say "Linking Klipper module"
rm -rf "$EXTRAS/pa_loadcell"
ln -s "$REPO/klipper/pa_loadcell" "$EXTRAS/pa_loadcell" || cp -r "$REPO/klipper/pa_loadcell" "$EXTRAS/pa_loadcell"
# keep Klipper's git tree clean so Moonraker does not flag it as dirty
if [ -d "$KLIPPER_DIR/.git" ] && ! grep -qx 'klippy/extras/pa_loadcell' "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null; then
  echo 'klippy/extras/pa_loadcell' >> "$KLIPPER_DIR/.git/info/exclude"
fi

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
            'method', 'pa_mode', 'pa_start', 'pa_end', 'pa_step', 'wobble',
            'wobble_axis', 'accel', 'model')
# earlier defaults: a value nobody changed moves to the PIRO-EDGE default
OLD_DEFAULTS = {'cycles': ('3', '4'), 'fast_flow': ('19.24',), 'fast_time': ('0.25',),
                'slow_flow': ('1.92',), 'prime_length': ('40',), 'prime_flow': ('5',),
                'prime_rest': ('0',), 'rest_time': ('0',), 'pa_max': ('0.08',)}
key = lambda l: (re.match(r'^([a-z_]+)\s*:', l) or [None, None])[1]
have = set()
for i, l in enumerate(lines):
    k = key(l)
    if k in OBSOLETE:
        lines[i] = '# no longer used: ' + l
    elif k:
        have.add(k)
newl = {key(l): l for l in open(new).read().split('\n') if key(l)}
moved = []
for k, olds in OLD_DEFAULTS.items():              # untouched old default -> new
    for i, l in enumerate(lines):
        m = re.match(r'^%s\s*:\s*([^\s#]+)' % k, l)
        if key(l) == k and m and m.group(1) in olds and k in newl:
            lines[i] = newl[k]; moved.append(k)
add = [newl[k] for k in newl if k not in have]
if add:
    while lines and lines[-1] == '':
        lines.pop()
    lines += ['# added by install.sh'] + add + ['']
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
    echo "    kept your $AUTO (the latest version is $REPO/klipper/pa_auto.cfg)"
  fi
else
  cp "$REPO/klipper/pa_auto.cfg" "$AUTO"; echo "    $AUTO"
fi

say "Adding includes to printer.cfg"
PCFG="$CONFIG_DIR/printer.cfg"
BACKED=0
for INC in pa_loadcell.cfg pa_auto.cfg; do
  if grep -qx "\[include $INC\]" "$PCFG"; then
    echo "    [include $INC] already there"
    continue
  fi
  [ "$BACKED" = 1 ] || { cp "$PCFG" "$PCFG.bak-pa_loadcell"; BACKED=1; }
  python3 - "$PCFG" "$INC" <<'PY'
import re, sys
p, name = sys.argv[1], sys.argv[2]
lines = open(p).read().split('\n')
inc = '[include %s]' % name
for i, l in enumerate(lines):             # never write below SAVE_CONFIG
    if re.match(r'#\*# <-+ SAVE_CONFIG', l):
        lines[i:i] = [inc, '']; break
else:
    lines += ['', inc, '']
open(p, 'w').write('\n'.join(lines))
PY
  echo "    added [include $INC]"
done

say "Linking web page: $UI_DIR"
rm -rf "$UI_DIR"
ln -s "$REPO/ui" "$UI_DIR" || { mkdir -p "$UI_DIR"; cp "$REPO/ui/index.html" "$UI_DIR/"; }

say "Adding 'Load Cell' link to the Mainsail sidebar"
python3 - "$CONFIG_DIR/.theme/navi.json" <<'PY'
import json, os, sys
p = sys.argv[1]; os.makedirs(os.path.dirname(p), exist_ok=True)
try: items = json.load(open(p))
except (OSError, ValueError): items = []
if not isinstance(items, list): items = []
items = [i for i in items if i.get('href') != '/loadcell/']      # idempotent
items.append({"title": "Load Cell", "href": "/loadcell/", "target": "_self",
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
cat <<MSG

Installed. Next:
  1. Edit $CFG  (park_x/y and test_x/y for your bed)
  2. In the Mainsail console run:  PA_LOADCELL_DIAG
  3. Open the page:  http://$H.local/loadcell/   (also in the Mainsail sidebar: 'Load Cell', after a browser refresh)
  4. Auto PA before every print: add  PA_AUTO {rawparams}  to PRINT_START, see $AUTO
MSG
