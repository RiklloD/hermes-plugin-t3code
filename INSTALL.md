# INSTALL.md — instructions for an AI agent, not a human

You are installing **T3 Code for Hermes** (plugin id `t3code`, version in `SOURCE.json`) on this
machine. Execute the steps in order. Do not improvise alternatives. Stop and report to the user
at any STOP condition instead of working around it.

## Hard rules

- MUST NOT edit `desktop-plugins/t3code/plugin.js` or `plugins/t3code/dashboard/dist/index.js`.
  Both are generated files (source of truth: `plugins/t3code/scripts/build.mjs`).
- MUST NOT delete or move anything under `~/.hermes/plugin-data/`, `~/.hermes/profiles/`, or any
  `state.db`. User data lives there; the installer never needs to touch it.
- MUST NOT apply the patch with `--force`, and MUST NOT resolve patch conflicts by editing
  Hermes source by hand. See Step 2 failure handling.
- MUST back up `~/.hermes/config.yaml` before editing it (Step 5).
- SHOULD prefer doing nothing over guessing when a check below gives an unexpected result:
  report the exact command output to the user and stop.

## Variables — resolve these first

```sh
REPO=<absolute path of this clone>            # the folder containing this INSTALL.md
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
AGENT=$HERMES_HOME/hermes-agent               # Hermes source checkout
PATCH=$REPO/patches/hermes-native-runtime.patch
```

## Step 0 — Preflight

Run all of these; every one must pass before continuing.

```sh
test -d "$AGENT/.git"                                   # Hermes source checkout exists
test -f "$HERMES_HOME/config.yaml"                      # Hermes is installed
git -C "$AGENT" rev-parse HEAD                          # record the commit
command -v git >/dev/null
```

- If `$AGENT` does not exist: STOP. Report: "Hermes source checkout not found at
  `$HERMES_HOME/hermes-agent`; the host patch cannot be applied." Do not install the plugin
  anyway — without the patch the three native providers (Claude CLI, Muse Code, Antigravity)
  cannot run.
- Record the Hermes commit. It is expected to be the `hermes_base_commit` in `SOURCE.json`
  (`9cb34e9e285dadd0c6d2463e9b094052530fc365`). A different commit is not an automatic failure,
  but Step 2 decides.

## Step 2 — Host patch (required, idempotent)

```sh
# Case A: already applied — this succeeds. Then skip to Step 3.
git -C "$AGENT" apply --reverse --check "$PATCH"

# Case B: not applied yet — this must succeed before applying.
git -C "$AGENT" apply --check "$PATCH"
git -C "$AGENT" apply "$PATCH"
```

Failure handling:

- If `--check` fails: run `git -C "$AGENT" apply --3way "$PATCH"`. If that leaves conflicts,
  STOP. Report the conflicted files and tell the user the patch targets the commit
  in `SOURCE.json` no longer matches their Hermes version and needs porting. MUST NOT hand-edit
  Hermes files to make it fit.
- Note for the user: an unrelated Z.ai 429/1311 fix that may exist on the author's machine is
  deliberately NOT in this patch. Do not add it.

## Step 3 — Install the plugin

```sh
"$REPO/install.sh"            # or: "$REPO/install.sh" /path/to/.hermes
```

`install.sh` refuses to run unless Step 2 is done, backs up any existing
`~/.hermes/plugins/t3code` into `~/.hermes/backups/`, then copies the plugin and the prebuilt
Desktop bundle. It does not modify `config.yaml`, profiles, or account data.

Verify:

```sh
test -f "$HERMES_HOME/plugins/t3code/plugin.yaml"
test -f "$HERMES_HOME/desktop-plugins/t3code/plugin.js"
```

## Step 4 — Python dependency

The plugin declares `claude-agent-sdk>=0.2.164,<0.3` (see `plugins/t3code/plugin.yaml`).
Install it **through Hermes's plugin manager** (Capabilities ▸ Plugins ▸ T3 Code) so it lands in
Hermes's own Python environment. MUST NOT `pip install` into the system Python or into a random
venv. If you cannot drive the plugin manager, tell the user this one action is theirs.

## Step 5 — Enable the plugin

Preferred: user enables **T3 Code** in Capabilities ▸ Plugins, then restarts the Hermes backend.

If you are explicitly authorized to edit config instead:

1. Back up: `cp "$HERMES_HOME/config.yaml" "$HERMES_HOME/config.yaml.bak-t3code-$(date +%Y%m%d%H%M%S)"`.
2. Ensure `t3code` is not in `plugins.disabled`, and — if `plugins.enabled` is a list — that
   `t3code` is in it. Preserve every other key and all formatting-sensitive values. Change
   nothing else.
3. Feature toggles (optional): `plugins.entries.t3code.settings.<feature>: false` disables one
   of `inbox`, `usage`, `orchestrator`, `claude_cli`, `muse_code`, `antigravity`. Missing means
   on. Leave all on unless the user asked otherwise.
4. Tell the user to restart the Hermes backend. Inbox/Usage apply live; Orchestrator tools and
   the three providers only register after a backend restart (Hermes discovers model providers
   at startup).

Note: `antigravity_api_key`, if ever set, is stored in plain text in `config.yaml`. Warn the
user before setting it for them.

## Step 6 — Provider sign-ins (only for providers the user wants)

Run nothing here unless the user asked for that provider.

- **Claude CLI:** `claude` must be installed and signed in (`claude` once, interactively — that
  sign-in is the user's; do not attempt it non-interactively).
- **Muse Code:** `muse` on `PATH`, then `muse login` (user's sign-in).
- **Antigravity:**

  ```sh
  <hermes python> "$HERMES_HOME/plugins/t3code/features/antigravity/setup.py" install
  <hermes python> "$HERMES_HOME/plugins/t3code/features/antigravity/setup.py" login
  ```

  `install` downloads the pinned 1.3.0 ACP runtime and verifies its SHA-256; skip it if
  `~/.t3/tools/antigravity-acp` already exists. `login` opens a browser — hand it to the user.
  The sign-in is shared by all Hermes profiles
  (`~/.hermes/plugin-data/t3code/antigravity/profile`) and stays separate from the Antigravity IDE.

## Step 7 — Verify the install

After the backend restart, confirm each item and report results verbatim:

```sh
git -C "$AGENT" apply --reverse --check "$PATCH" && echo "patch: applied"
ls -l "$HERMES_HOME/plugins/model-providers/"   # expect symlinks: claude-cli, muse-code, antigravity (created on backend start)
test -f "$HERMES_HOME/desktop-plugins/t3code/plugin.js" && echo "desktop bundle: present"
```

In the app: T3 Code appears under Capabilities ▸ Plugins; Command Center ▸ Usage loads; a new
chat offers Claude CLI / Muse Code / Antigravity as model providers (only those signed in will
actually answer). If a provider link (`plugins/model-providers/<name>`) points somewhere else,
that is a pre-existing user install — the plugin deliberately leaves it alone; report it, do
not replace it.

## Updating

Re-run Steps 2–3 on a fresh clone/pull. If Hermes itself was updated, Step 2 decides whether
the patch still applies. The Desktop bundle hot-reloads; Python changes need a backend restart.
To make the patch survive future Hermes updates, offer the user the branch setup in README.md
("Updating Hermes without losing the host patch"); do not set it up without their consent.

## Uninstall

```sh
"$REPO/uninstall.sh"
```

Moves the plugin and Desktop bundle into `~/.hermes/backups/`, removes only the provider
symlinks this plugin created, and leaves account data, other profiles and the host patch
untouched. Reverting the host patch is a separate, user-only decision:
`git -C "$AGENT" apply --reverse "$PATCH"`.

## Report format

Finish by reporting, one line each: Hermes commit found, patch state (already applied /
applied / failed), installer result, dependency state, enabled state, which providers were
set up, and any STOP condition hit with its exact error output.
