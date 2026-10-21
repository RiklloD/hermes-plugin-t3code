# T3 Code for Hermes

T3 Code's features for [Hermes Agent](https://github.com/NousResearch/hermes-agent), as **one plugin with independent features** — switch each on or off on its own; a failure in one is contained and the others keep working.

| Feature | What it does |
|---|---|
| **Inbox** | T3's thread sidebar: pinned, inbox, working, snoozed, settled; crash and usage-limit recovery |
| **Usage** | Claude Code, Codex, Z.ai, Muse Code and Antigravity tokens, cost and live limits (Command Center → Usage) |
| **Orchestrator** | Delegation to any provider/model (`t3_*` tools, `/t3`), forks + merge-back, durable queues, recovery, schedules |
| **Claude CLI** | Claude Code's native agent as the “Claude CLI” model provider |
| **Muse Code** | Meta's Muse Code agent (`muse serve`) as the “Muse Code” model provider |
| **Antigravity** | Google's official Antigravity ACP agent as the “Antigravity” model provider |
| **Native slash commands** | Each native agent's own commands (`/claude:compact`, `/muse:goal`, `/agy:plan`) in Hermes's slash menu |

## Latest changes

- **Inbox Working shelf:** a thread moves to Working the moment you send, even while its agent is still starting (Claude CLI takes a few seconds). Claude Code's own background shells, monitors and subagents keep a thread in Working after its turn ends.
- **Context meter:** Claude CLI chats refresh the meter from Claude's own `/context` after every turn, not only when the Context panel opens. A reading taken under another model or 200K/1M setting is dropped instead of showing the wrong window (host patch).
- **Claude accounts button:** Settings ▸ Plugins ▸ T3 Code ▸ Claude CLI can hide it from the status bar. Hiding it also stops its account check.
- **Keep the host patch through Hermes updates:** see "Updating Hermes" below.
- **Updated Hermes compatibility:** the host patch is rebased onto `9cb34e9e285dadd0c6d2463e9b094052530fc365`, preserving the new command-discovery/session-registry split and credential-route snapshots. Native turn timing and background-task fields are included in the gateway contracts.

- **Inbox thread mentions:** drag one thread or a selection onto a chat composer to insert `@session:` mentions, or type `@` to search the Inbox roster. Drops target the composer they land on, including tiled chats. The prebuilt Desktop bundle includes this feature.
- **Muse staged approvals:** each requirement gets its own permission prompt and exact decision payload; duplicate notifications are suppressed and stale answers are ignored after interruption, stage advancement, completion, or turn replacement. No stages are automatically approved. This adapter fix does not repair existing Muse approval-replay failures or rewrite session history.

Backend changes require a Hermes backend restart; Desktop changes hot-reload.

## Install

**Installing with an AI agent?** Point it at [INSTALL.md](INSTALL.md) — it is written for agents, with ordered steps, checks and stop conditions. Humans: continue below.

```sh
git clone https://github.com/RiklloD/hermes-plugin-t3code.git
cd hermes-plugin-t3code
# Quit Hermes before patching. Skip these two commands if this exact patch is already applied.
git -C ~/.hermes/hermes-agent apply --check "$PWD/patches/hermes-native-runtime.patch"
git -C ~/.hermes/hermes-agent apply "$PWD/patches/hermes-native-runtime.patch"
./install.sh            # or: ./install.sh /path/to/.hermes
```

The host patch is required for the three native providers and targets the Hermes commit listed below. The installer checks it is applied, backs up any existing plugin, then copies the complete plugin and prebuilt Desktop bundle. Install the declared `claude-agent-sdk>=0.2.164,<0.3` dependency through Hermes's plugin manager. No Node build is needed to use the included bundle.

Then restart the Hermes backend and enable **T3 Code** in Capabilities ▸ Plugins. Toggles per feature: Capabilities ▸ Plugins ▸ T3 Code ▸ settings, or Settings ▸ Plugins ▸ T3 Code. Inbox/Usage apply live; Orchestrator tools and the providers apply after a backend restart.

### Provider sign-ins (only for the providers you use)

- **Claude CLI:** install [Claude Code](https://claude.com/product/claude-code), run `claude` once and sign in.
- **Muse Code:** put `muse` on `PATH`, run `muse login`.
- **Antigravity:** first install the pinned, checksum-verified 1.3.0 runtime (skip this if T3 Code already downloaded it under `~/.t3/tools/antigravity-acp`), then sign in:
  ```sh
  <hermes python> ~/.hermes/plugins/t3code/features/antigravity/setup.py install
  <hermes python> ~/.hermes/plugins/t3code/features/antigravity/setup.py login
  ```
  or simply open an Antigravity chat — sign-in opens in the browser on the first turn. The sign-in is shared by every Hermes profile (`~/.hermes/plugin-data/t3code/antigravity/profile`) and kept separate from the Antigravity IDE/CLI and from T3 Code.

Model lists come from your accounts: Muse lists its models live; Antigravity lists your account's models once it has signed in (until then it falls back to the list T3 Code cached for the same account).

## Requirements

- Hermes Agent `>=0.21.5` with the Desktop app (host patch verified against hermes-agent `9cb34e9e285dadd0c6d2463e9b094052530fc365`)
- Python 3.11–3.14 (Hermes's own environment)
- Optional, per provider: `claude` CLI, `muse` CLI, Google account for Antigravity
- Node + Hermes's `esbuild` only if you rebuild the Desktop bundle: `node plugins/t3code/scripts/build.mjs`

## Host patch

The native providers take over a whole chat turn through Hermes's native-agent-runtime interface, which stock Hermes does not expose to plugins. `patches/hermes-native-runtime.patch` adds it:

- `providers/base.py`: `agent_runtime` opt-in on provider profiles
- `agent/native_runtime.py` (new): session-owned runtime lifecycle, turn streaming, interrupt, fork state
- small hooks in `agent_init`, `agent_runtime_helpers`, `turn_facade`, `run_agent`, manual compression and session auto-continue
- `agent/native_commands.py` (new): the native agents' own slash commands in Hermes's slash menu
- `website/docs/developer-guide/plugins/agent-runtime-plugin.md`
- `apps/desktop/.../thread/status.tsx`: the turn timer counts the whole turn instead of restarting at 0 whenever a native tool call lands (desktop source only; takes effect when Hermes Desktop is rebuilt from source)

Apply manually with:

```sh
git -C ~/.hermes/hermes-agent apply patches/hermes-native-runtime.patch
```

If it no longer applies cleanly after a Hermes update, try `git apply --3way`, or port the diff. The patch preserves the current shared command-discovery and session-registry layout and declares native activity fields in the generated gateway contracts. An unrelated Z.ai 429/1311 entitlement fix from the author's machine is **not** included.

## Updating Hermes without losing the host patch

`hermes update` stashes uncommitted source edits, pulls, and re-applies them; a conflict leaves the patch in `git stash` and the native providers stop working. Commit the patch on a branch instead and let the updater merge upstream into it:

```sh
cd ~/.hermes/hermes-agent
git switch -c my/host && git add -A && git commit -m "t3code host patch"
git config rerere.enabled true            # a conflict resolved once stays resolved
```

Then set `updates.parked_branch_strategy: update_in_place` in `config.yaml` (in every profile that has an `updates:` section, since each profile reads its own). `hermes update` and the Desktop Update button then merge `origin/main` into the branch and never switch away from it. On a merge conflict the update stops and the old code keeps running; resolve it with `git merge origin/main` on the branch and run the update again. Keep the source tree clean: an uncommitted file makes the updater skip the code update.

Maintainers: `sh sync-host-patch.sh [branch] [new/path ...]` rebuilds `patches/hermes-native-runtime.patch` from the branch, checks that it applies to a clean upstream tree, and stamps `SOURCE.json`.

## Layout

```
plugins/t3code/          the plugin (features/, shared/, dashboard/, scripts/)
desktop-plugins/t3code/  prebuilt Desktop bundle (generated by plugins/t3code/scripts/build.mjs)
patches/                 the required Hermes host patch
install.sh / uninstall.sh
```

Full details per feature live in `plugins/t3code/README.md` and each `features/*/README.md`.

## Uninstall

```sh
./uninstall.sh   # moves plugin/UI into a backup; leaves account data, other profiles and the host patch untouched
```

## Attribution

Ported from [T3 Code](https://github.com/pingdotgg/t3code) (MIT, © T3 Tools Inc.) — see `plugins/t3code/features/claude_cli/THIRD_PARTY_NOTICES.md`. Claude Code / Claude Agent SDK, Muse Code and Antigravity are upstream products with their own terms; none of their binaries are vendored here (Antigravity's runtime is downloaded from Google at setup, checksum-verified).

## License

MIT — see [LICENSE](LICENSE).
