#!/bin/sh
# Preserve files in a backup; only unlink providers owned by this plugin.
set -eu
HOME_DIR=${1:-${HERMES_HOME:-$HOME/.hermes}}
BACKUP="$HOME_DIR/backups/t3code-uninstall-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BACKUP"
for pair in claude-cli:claude_cli muse-code:muse_code antigravity:antigravity; do
  name=${pair%%:*}; feature=${pair##*:}
  link="$HOME_DIR/plugins/model-providers/$name"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$HOME_DIR/plugins/t3code/features/$feature" ]; then
    rm "$link"
  fi
done
[ ! -e "$HOME_DIR/plugins/t3code" ] || mv "$HOME_DIR/plugins/t3code" "$BACKUP/plugin"
[ ! -e "$HOME_DIR/desktop-plugins/t3code" ] || mv "$HOME_DIR/desktop-plugins/t3code" "$BACKUP/desktop"
echo "Plugin preserved in $BACKUP. Restart Hermes. Other profiles, account data and the host patch were left untouched."
