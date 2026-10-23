#!/usr/bin/env bash
# PA Load Cell Analyzer installer. Run from the repo folder:
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove
# Optional env overrides: KLIPPER_DIR CONFIG_DIR UI_DIR NGINX_SITE KLIPPER_SERVICE
#                         NO_RESTART=1 (skip nginx reload + klipper restart)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"
UI_DIR="${UI_DIR:-$HOME/pa_loadcell_ui}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/mainsail}"
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
SUDO="${SUDO-sudo}"; [ "$(id -u)" -eq 0 ] && SUDO=""
EXTRAS="$KLIPPER_DIR/klippy/extras"
MARK="# pa_loadcell"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

restart_services() {
  [ "${NO_RESTART:-0}" = 1 ] && { warn "NO_RESTART=1: restart klipper and nginx yourself"; return; }
  if [ -f "$NGINX_SITE" ] && command -v nginx >/dev/null; then
    if $SUDO nginx -t >/dev/null 2>&1; then $SUDO systemctl reload nginx; else warn "nginx -t failed; not reloading"; fi
  fi
  if systemctl list-unit-files 2>/dev/null | grep -q "^$KLIPPER_SERVICE.service"; then
    say "Restarting $KLIPPER_SERVICE"; $SUDO systemctl restart "$KLIPPER_SERVICE"
  else warn "service '$KLIPPER_SERVICE' not found: restart Klipper manually (FIRMWARE_RESTART)"; fi
}

nginx_remove() {
  [ -f "$NGINX_SITE" ] && grep -q "$MARK" "$NGINX_SITE" || return 0
  $SUDO cp "$NGINX_SITE" "$NGINX_SITE.bak-pa_loadcell"
  $SUDO sed -i "/$MARK begin/,/$MARK end/d" "$NGINX_SITE"
}

if [ "${1:-}" = "--uninstall" ]; then
  say "Uninstalling"
  rm -rf "$EXTRAS/pa_loadcell" "$UI_DIR"
  [ -f "$CONFIG_DIR/printer.cfg" ] && sed -i '/^\[include pa_loadcell.cfg\]$/d' "$CONFIG_DIR/printer.cfg"
  nginx_remove
  restart_services
  say "Done. Config ($CONFIG_DIR/pa_loadcell.cfg) and recorded data (~/printer_data/pa_loadcell) were kept."
  exit 0
fi

[ -d "$EXTRAS" ] || die "Klipper extras folder not found: $EXTRAS (set KLIPPER_DIR)"
[ -f "$CONFIG_DIR/printer.cfg" ] || die "printer.cfg not found in $CONFIG_DIR (set CONFIG_DIR)"

say "Installing Klipper module"
rm -rf "$EXTRAS/pa_loadcell"
cp -r "$REPO/klipper/pa_loadcell" "$EXTRAS/pa_loadcell"
find "$EXTRAS/pa_loadcell" -name __pycache__ -prune -exec rm -rf {} +
# keep Klipper's git tree clean so Moonraker does not flag it as dirty
if [ -d "$KLIPPER_DIR/.git" ] && ! grep -qx 'klippy/extras/pa_loadcell' "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null; then
  echo 'klippy/extras/pa_loadcell' >> "$KLIPPER_DIR/.git/info/exclude"
fi

say "Installing config"
CFG="$CONFIG_DIR/pa_loadcell.cfg"
if [ -f "$CFG" ]; then
  warn "$CFG exists: kept your settings (new defaults: $REPO/klipper/pa_loadcell.cfg)"
else
  cp "$REPO/klipper/pa_loadcell.cfg" "$CFG"
  LC_SEC="$(grep -rhE '^\[load_cell(_probe)?( [^]]+)?\]' "$CONFIG_DIR" --include='*.cfg' 2>/dev/null | head -n1 | tr -d '[]' || true)"
  if [ -n "$LC_SEC" ]; then
    sed -i "s|^load_cell:.*|load_cell: $LC_SEC|" "$CFG"; say "Detected load cell section: [$LC_SEC]"
  else
    warn "No [load_cell] section found in $CONFIG_DIR: set 'load_cell:' in pa_loadcell.cfg"
  fi
fi

say "Adding include to printer.cfg"
PCFG="$CONFIG_DIR/printer.cfg"
if grep -qx '\[include pa_loadcell.cfg\]' "$PCFG"; then
  echo "    already included"
else
  cp "$PCFG" "$PCFG.bak-pa_loadcell"
  python3 - "$PCFG" <<'PY'
import re, sys
p = sys.argv[1]; lines = open(p).read().split('\n')
inc = '[include pa_loadcell.cfg]'
for i, l in enumerate(lines):             # never write below SAVE_CONFIG
    if re.match(r'#\*# <-+ SAVE_CONFIG', l):
        lines[i:i] = [inc, '']; break
else:
    lines += ['', inc, '']
open(p, 'w').write('\n'.join(lines))
PY
fi

say "Installing web page to $UI_DIR"
rm -rf "$UI_DIR"; mkdir -p "$UI_DIR"; cp "$REPO/ui/index.html" "$UI_DIR/"

if [ -f "$NGINX_SITE" ]; then
  if grep -q "$MARK" "$NGINX_SITE"; then echo "    nginx already configured"
  else
    say "Configuring nginx (/loadcell/)"
    $SUDO cp "$NGINX_SITE" "$NGINX_SITE.bak-pa_loadcell"
    $SUDO sed -i "0,/^[[:space:]]*server[[:space:]]*{/s||&\n    $MARK begin\n    location /loadcell/ { alias $UI_DIR/; index index.html; }\n    $MARK end|" "$NGINX_SITE"
  fi
else
  warn "nginx site $NGINX_SITE not found: serve $UI_DIR yourself (see README)"
fi

restart_services
H="$(hostname)"
cat <<MSG

Installed. Next:
  1. Edit $CFG  (park_x/y and test_x/y for your bed)
  2. In the Mainsail console run:  PA_LOADCELL_DIAG
  3. Open the page:  http://$H.local/loadcell/
MSG
