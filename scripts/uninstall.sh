#!/usr/bin/env bash
# Remove what scripts/install.sh set up. Keeps your config (~/.config/tesla) and the
# downloaded models (~/.local/share/tesla/models); delete those by hand if you want.
set -euo pipefail

uuid="tesla@vuk.dev"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/tesla.service"

systemctl --user disable --now tesla.service 2>/dev/null || true
rm -f "$unit"
systemctl --user daemon-reload

[[ -x "$HOME/.local/bin/tesla" ]] && "$HOME/.local/bin/tesla" setup-hotkey --remove || true
gnome-extensions disable "$uuid" 2>/dev/null || true
rm -rf "$data/gnome-shell/extensions/$uuid"

uv tool uninstall tesla 2>/dev/null || true
rm -f "$data/tesla/installed.json"
echo "Tesla uninstalled. Log out and back in to unload the notch extension."
