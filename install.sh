#!/bin/sh
# Install T3 Code for Hermes: plugin + prebuilt Desktop bundle + required Hermes host patch.
# Usage: ./install.sh [HERMES_HOME]   (default: $HERMES_HOME or ~/.hermes)
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${1:-${HERMES_HOME:-$HOME/.hermes}}
AGENT="$HOME_DIR/hermes-agent"

if [ ! -d "$HOME_DIR" ]; then echo "Hermes home not found: $HOME_DIR" >&2; exit 1; fi

# 1. Host patch (native providers Claude CLI / Muse Code / Antigravity need it).
#    Checked first so a Hermes version the patch does not apply to fails before any copy.
if git -C "$AGENT" rev-parse --git-dir >/dev/null 2>&1; then
  if git -C "$AGENT" apply --check "$SRC/patches/hermes-native-runtime.patch" 2>/dev/null; then
    git -C "$AGENT" apply "$SRC/patches/hermes-native-runtime.patch"
    echo "Applied patches/hermes-native-runtime.patch to hermes-agent"
  elif git -C "$AGENT" apply --reverse --check "$SRC/patches/hermes-native-runtime.patch" 2>/dev/null; then
    echo "Host patch already applied"
  else
    echo "ERROR: host patch does not apply to $AGENT (Hermes has moved on)." >&2
    echo "Patch base: hermes-agent 1fd18277267152893a8a24792b79213e7ca3f899." >&2
    echo "Try: git -C \"$AGENT\" apply --3way \"$SRC/patches/hermes-native-runtime.patch\"" >&2
    exit 1
  fi
else
  echo "WARNING: $AGENT is not a git checkout; apply patches/hermes-native-runtime.patch manually." >&2
fi

# 2. Plugin + prebuilt Desktop bundle (no Node build needed; sources + scripts/build.mjs included).
mkdir -p "$HOME_DIR/plugins" "$HOME_DIR/desktop-plugins/t3code"
rm -rf "$HOME_DIR/plugins/t3code"
cp -R "$SRC/plugins/t3code" "$HOME_DIR/plugins/t3code"
find "$HOME_DIR/plugins/t3code" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$SRC/desktop-plugins/t3code/plugin.js" "$HOME_DIR/desktop-plugins/t3code/plugin.js"
echo "Installed plugin -> $HOME_DIR/plugins/t3code"
echo "Installed Desktop bundle -> $HOME_DIR/desktop-plugins/t3code/plugin.js"

# 3. Claude Agent SDK (declared in plugin.yaml; Hermes normally installs it on enable).
PY=""
for c in "$AGENT/venv/bin/python" "$HOME_DIR"/tools/python-*/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
if [ -n "$PY" ]; then
  "$PY" -c "import claude_agent_sdk" 2>/dev/null || \
    "$PY" -m pip install -q "claude-agent-sdk>=0.2.164,<0.3" || \
    echo "WARNING: could not install claude-agent-sdk with $PY; install it into Hermes's Python." >&2
fi

cat <<'EOF'

Done. Next:
  1. Restart the Hermes backend, then enable "T3 Code" in Capabilities > Plugins.
  2. Claude CLI:  install Claude Code and run: claude  (sign in once)
  3. Muse Code:  put `muse` on PATH and run: muse login
  4. Antigravity: <hermes python> ~/.hermes/plugins/t3code/features/antigravity/setup.py login
     (or just open an Antigravity chat; sign-in opens on the first turn)
EOF
