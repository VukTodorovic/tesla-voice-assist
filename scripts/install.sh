#!/usr/bin/env bash
# Install Tesla on this machine from this checkout, or update an existing install.
#
#   scripts/install.sh
#
# Installs a snapshot of the current code as its own program (uv tool, so work in
# this repo does not touch the running assistant until you install again), the GNOME
# Shell extension, the Super+Alt+Space shortcut, and a systemd user service that
# starts Tesla at every login and restarts it if it crashes. Run it again after
# changing the code to replace the running version.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
uuid="tesla@vuk.dev"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
state="$data/tesla"
wheels="$state/wheels"
ext_dir="$data/gnome-shell/extensions/$uuid"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
bin="$HOME/.local/bin/tesla"
installed="$state/installed.json"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
previous() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" "$installed" "$1" 2>/dev/null || true; }

commit="$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo uncommitted)"
[[ -n "$(git -C "$root" status --porcelain 2>/dev/null)" ]] && commit="$commit+dirty"
now="$(date '+%Y-%m-%d %H:%M')"
mkdir -p "$state" "$unit_dir"

step "whisper.cpp with Vulkan (GPU speech recognition)"
if ! ls "$wheels"/pywhispercpp-*.whl >/dev/null 2>&1; then
  "$root/scripts/build-whispercpp-vulkan.sh" --wheel ||
    echo "   Build failed; Tesla falls back to faster-whisper on the CPU."
fi
with=()
if wheel="$(ls "$wheels"/pywhispercpp-*.whl 2>/dev/null | head -1)" && [[ -n "$wheel" ]]; then
  with=(--with "$wheel")
  echo "   $wheel"
fi

step "Tesla $commit"
uv tool install --force --reinstall --python 3.12 "${with[@]}" "$root"

step "GNOME Shell extension"
ext_hash="$(cd "$root/gnome-extension/$uuid" && find . -type f -print0 | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-12)"
rm -rf "$ext_dir"  # the previous copy, or a development symlink into a repo
mkdir -p "$ext_dir"
cp -r "$root/gnome-extension/$uuid/." "$ext_dir/"
gnome-extensions enable "$uuid" 2>/dev/null ||
  gsettings set org.gnome.shell enabled-extensions "$(python3 -c "
import ast, sys
current = ast.literal_eval(sys.argv[1].replace('@as ', ''))
print([*current, sys.argv[2]] if sys.argv[2] not in current else current)" \
    "$(gsettings get org.gnome.shell enabled-extensions)" "$uuid")"
[[ "$(previous extension)" != "$ext_hash" ]] && relogin=1 || relogin=0

step "Secrets"
if [[ -f "$root/.env" ]]; then
  # The service reads ~/.config/tesla/env (e.g. GROQ_API_KEY); keep it private.
  conf="${XDG_CONFIG_HOME:-$HOME/.config}/tesla"
  mkdir -p "$conf"
  install -m 600 "$root/.env" "$conf/env"
  echo "   $root/.env -> $conf/env ($(sed -E 's/=.*//' "$root/.env" | grep -v '^#' | xargs))"
else
  echo "   no .env in the repo; nothing to copy"
fi

step "Shortcut Super+Alt+Space"
"$bin" setup-hotkey

step "systemd user service"
cp "$root/packaging/tesla.service" "$unit_dir/tesla.service"
systemctl --user daemon-reload
# A Tesla started by hand (uv run tesla) holds the D-Bus name; stop it gracefully.
service_pid="$(systemctl --user show -p MainPID --value tesla 2>/dev/null || echo 0)"
for pid in $(pgrep -u "$USER" -f '\.venv/bin/tesla( run)?$' || true); do
  [[ "$pid" == "$service_pid" ]] && continue
  echo "   stopping Tesla started by hand (pid $pid)"
  kill -INT "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
done
systemctl --user enable tesla.service >/dev/null
systemctl --user restart tesla.service

python3 - "$installed" "$commit" "$now" "$root" "$ext_hash" <<'EOF'
import json, sys
path, commit, at, repo, ext = sys.argv[1:]
json.dump({"commit": commit, "installed_at": at, "repo": repo, "extension": ext},
          open(path, "w"), indent=2)
EOF

step "Waiting for Tesla to come up"
since="$(date '+%Y-%m-%d %H:%M:%S' -d '-5 sec')"
ready=0
for _ in $(seq 1 90); do
  if journalctl --user -u tesla --since "$since" -o cat 2>/dev/null | grep -q "Tesla je spremna"; then
    ready=1
    break
  fi
  sleep 0.5
done
# "active" alone is not enough: a crash loop looks active between restarts.
if [[ "$ready" == 1 ]] && systemctl --user is-active --quiet tesla; then
  echo "   running: $("$bin" version)"
  journalctl --user -u tesla --since "$since" -o cat | grep -E "loaded|ready|spremna|stt:|ERROR|Error" | tail -6 | sed 's/^/   /'
else
  echo "   Tesla did not start:"
  journalctl --user -u tesla --since "$since" -o cat | tail -8 | sed 's/^/   /'
  echo "   Full logs: journalctl --user -u tesla -e"
  exit 1
fi

echo
echo "Logs:      journalctl --user -u tesla -f"
echo "Restart:   systemctl --user restart tesla"
echo "Uninstall: scripts/uninstall.sh"
if [[ "$relogin" == 1 ]]; then
  echo
  echo "The GNOME Shell extension changed: log out and back in to load the new notch."
fi
