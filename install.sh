#!/bin/sh
# Copy the plugin and prebuilt UI; apply the included host patch manually first.
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${1:-${HERMES_HOME:-$HOME/.hermes}}
AGENT=${HERMES_SOURCE_ROOT:-$HOME_DIR/hermes-agent}
if ! git -C "$AGENT" apply --reverse --check "$SRC/patches/hermes-native-runtime.patch" 2>/dev/null; then
  echo "Quit Hermes and apply the included host patch manually first:" >&2
  echo "git -C \"$AGENT\" apply --check \"$SRC/patches/hermes-native-runtime.patch\"" >&2
  echo "git -C \"$AGENT\" apply \"$SRC/patches/hermes-native-runtime.patch\"" >&2
  exit 1
fi
mkdir -p "$HOME_DIR/plugins" "$HOME_DIR/desktop-plugins/t3code"
if [ -e "$HOME_DIR/plugins/t3code" ]; then
  BACKUP="$HOME_DIR/backups/t3code-$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$BACKUP"
  mv "$HOME_DIR/plugins/t3code" "$BACKUP/t3code"
  echo "Existing plugin saved in $BACKUP"
fi
cp -R "$SRC/plugins/t3code" "$HOME_DIR/plugins/t3code"
if [ -f "$HOME_DIR/desktop-plugins/t3code/plugin.js" ]; then
  cp "$HOME_DIR/desktop-plugins/t3code/plugin.js" "$HOME_DIR/desktop-plugins/t3code/plugin.js.bak"
fi
cp "$SRC/desktop-plugins/t3code/plugin.js" "$HOME_DIR/desktop-plugins/t3code/plugin.js"
echo "Copied T3 Code. Enable it in Hermes Capabilities > Plugins, install its declared dependencies through Hermes, then restart the backend."
echo "Claude: claude auth login. Muse: muse login."
echo "Antigravity: use Hermes Python to run features/antigravity/setup.py install, then setup.py login."
