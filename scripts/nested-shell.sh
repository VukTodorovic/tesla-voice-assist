#!/usr/bin/env bash
# Run a nested GNOME Shell (in a window) with the Tesla extension and daemon,
# isolated from your real session's settings. For developing the extension.
#   scripts/nested-shell.sh            # 1600x900 window
# Inside, trigger Tesla with:
#   gdbus call --session --dest dev.vuk.TeslaShell --object-path /dev/vuk/TeslaShell --method dev.vuk.TeslaShell.Activate
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
uuid="tesla@vuk.dev"
state="${TESLA_NESTED_DIR:-${XDG_RUNTIME_DIR}/tesla-nested}"
mkdir -p "$state"
# The extension under development, in a data dir only the nested shell sees, so the
# installed copy (scripts/install.sh) stays untouched.
mkdir -p "$state/data/gnome-shell/extensions"
ln -sfn "$root/gnome-extension/$uuid" "$state/data/gnome-shell/extensions/$uuid"
# Separate dconf database so enabling the extension here does not touch your session.
printf 'user-db:teslanested\n' > "$state/dconf-profile"
export DCONF_PROFILE="$state/dconf-profile"
export MUTTER_DEBUG_DUMMY_MODE_SPECS="${TESLA_NESTED_SIZE:-1600x900}"
exec dbus-run-session -- bash -c '
  gsettings set org.gnome.shell enabled-extensions "[\"'"$uuid"'\"]"
  gsettings set org.gnome.shell welcome-dialog-last-shown-version "999"
  echo "$DBUS_SESSION_BUS_ADDRESS" > "'"$state"'/bus"
  XDG_DATA_HOME="'"$state"'/data" gnome-shell --nested --wayland > "'"$state"'/shell.log" 2>&1 &
  shell=$!
  sleep 4
  cd "'"$root"'" && uv run tesla ${TESLA_ARGS:-} run > "'"$state"'/daemon.log" 2>&1 &
  wait $shell
'
