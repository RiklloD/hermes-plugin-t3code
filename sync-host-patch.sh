#!/bin/sh
# Rebuild patches/hermes-native-runtime.patch from the local Hermes branch that carries it.
#
# The host changes live as commits on a branch of hermes-agent (default lorik/host), which
# `hermes update` keeps merged with upstream (updates.parked_branch_strategy: update_in_place).
# This takes that branch's net diff against upstream, limited to the files the patch already
# covers (pass extra paths to add new ones), and stamps SOURCE.json with the upstream base.
#
#   sh sync-host-patch.sh [branch] [extra/path ...]
set -eu
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT=${HERMES_SOURCE_ROOT:-$HOME/.hermes/hermes-agent}
BRANCH=${1:-lorik/host}
[ $# -gt 0 ] && shift
PATCH="$SRC/patches/hermes-native-runtime.patch"

git -C "$AGENT" fetch -q origin main
BASE=$(git -C "$AGENT" merge-base origin/main "$BRANCH")
FILES=$( (grep '^diff --git' "$PATCH" | awk '{print substr($3,3)}'; for f in "$@"; do echo "$f"; done) | sort -u)

# shellcheck disable=SC2086
git -C "$AGENT" diff --no-color "$BASE" "$BRANCH" -- $FILES > "$PATCH.new"
mv "$PATCH.new" "$PATCH"
sed -i.bak "s/\"hermes_base_commit\": \"[0-9a-f]*\"/\"hermes_base_commit\": \"$BASE\"/" "$SRC/SOURCE.json"
rm -f "$SRC/SOURCE.json.bak"

# The published patch must apply to a clean upstream tree.
TMP=$(mktemp -d)
git -C "$AGENT" worktree add -q --detach "$TMP/wt" "$BASE"
if git -C "$TMP/wt" apply --check "$PATCH"; then ok=1; else ok=0; fi
git -C "$AGENT" worktree remove --force "$TMP/wt"
rm -rf "$TMP"
[ "$ok" = 1 ] || { echo "Patch does not apply to $BASE" >&2; exit 1; }
echo "patches/hermes-native-runtime.patch rebuilt from $BRANCH on $(echo "$BASE" | cut -c1-10) ($(grep -c '^diff --git' "$PATCH") files)"
