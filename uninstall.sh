#!/bin/sh
# Remove T3 Code for Hermes and reverse the host patch (if it is applied exactly).
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${1:-${HERMES_HOME:-$HOME/.hermes}}
AGENT="$HOME_DIR/hermes-agent"
rm -rf "$HOME_DIR/plugins/t3code" "$HOME_DIR/desktop-plugins/t3code"
rm -f "$HOME_DIR/plugins/model-providers/claude-cli" "$HOME_DIR/plugins/model-providers/muse-code" "$HOME_DIR/plugins/model-providers/antigravity"
for p in "$HOME_DIR"/profiles/*/plugins/model-providers; do
  rm -f "$p/claude-cli" "$p/muse-code" "$p/antigravity" 2>/dev/null || true
done
if git -C "$AGENT" rev-parse --git-dir >/dev/null 2>&1 && git -C "$AGENT" apply --reverse --check "$SRC/patches/hermes-native-runtime.patch" 2>/dev/null; then
  git -C "$AGENT" apply --reverse "$SRC/patches/hermes-native-runtime.patch"
  echo "Reversed host patch"
fi
echo "Removed T3 Code. Restart the Hermes backend."
