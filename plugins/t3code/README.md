# T3 Code for Hermes

T3 Code's features in Hermes, shipped as **one plugin** (`t3code`) with **independent features**. Install, enable, and update one package; switch each feature on or off by itself. A bug in one feature stays in that feature.

| Feature | What it does | Was |
|---|---|---|
| **Inbox** | T3's thread sidebar: pinned, inbox, working, snoozed, settled; crash and usage-limit recovery | `t3-inbox` |
| **Usage** | Claude Code / Codex / Z.ai tokens, cost and live limits in Command Center → Usage | `t3-usage` |
| **Orchestrator** | Delegation to any provider/model, forks + merge-back, durable queues, recovery, schedules (`t3_*` tools, `/t3`) | `t3-orchestrator` |
| **Claude CLI** | Claude Code's native agent as the "Claude CLI" model provider | `claude-cli` |
| **Muse Code** | Meta's Muse Code agent (MSP over `muse serve`) as the "Muse Code" model provider | new (T3 `provider-muse`) |
| **Antigravity** | Google's official Antigravity ACP agent as the "Antigravity" model provider | new (T3 Antigravity driver) |

## Switching features on and off

- **Capabilities ▸ Plugins ▸ T3 Code ▸ settings** (the Hermes plugin manager): one switch per feature (`inbox`, `usage`, `orchestrator`, `claude_cli`, `muse_code`, `antigravity`).
- **Settings ▸ Plugins ▸ T3 Code**: the same switches, plus each feature's status and errors. The page also has a **Restart** button for a feature that failed, and each feature's own settings as sub-pages.
- Config: `plugins.entries.t3code.settings.<feature>: false` (per profile; missing means on).

Inbox and Usage apply live. For the Orchestrator, the UI and REST apply live, but its agent tools change only after a backend restart. Claude CLI, Muse Code and Antigravity apply after a backend restart, because Hermes discovers model providers at startup.

## Layout

```
t3code/
├── plugin.yaml              one manifest: feature switches + Claude CLI options (config_schema), tools, hooks
├── __init__.py              agent entry → shared/features.register_agent_features
├── shared/                  the only code features share (keep it small)
│   ├── features.py          feature table, toggles, status, guarded startup, Claude CLI provider link
│   ├── hermes_data.py       Hermes root + per-profile state.db (Inbox + Usage)
│   ├── stdio_rpc.py         newline JSON-RPC over a child's stdio (Muse MSP + Antigravity ACP)
│   ├── native_turns.py      NativeAgentRuntime turn plumbing for the stdio providers
│   ├── native_commands.py   the native agents' slash-command cache (Hermes's slash menu)
│   └── desktop/
│       ├── shell.js         the Desktop plugin: starts/stops features from the toggles, T3 Code settings page
│       └── isolation.js     per-feature scoped ctx, error boundaries, guarded callbacks, storage migration
├── features/
│   ├── inbox/               api.py (REST) · desktop.js (UI)
│   ├── usage/               api.py · desktop.js
│   ├── orchestrator/        agent.py (tools, hook, /t3) · core/ (engine) · api.py · desktop.js · web.js
│   ├── claude_cli/          model provider (__init__.py, sdk_runtime.py, …)
│   ├── muse_code/           model provider (__init__.py, catalog.py, protocol.py, runtime.py)
│   └── antigravity/         model provider (… + release.py runtime install, setup.py install/login)
├── dashboard/
│   ├── manifest.json        web dashboard tab + REST mount
│   ├── plugin_api.py        mounts features/<f>/api.py at /api/plugins/t3code/<f>/ — each isolated
│   └── dist/index.js        GENERATED from features/orchestrator/web.js
└── scripts/build.mjs        builds the generated files (Desktop bundle + web tab)
```

The Desktop half is GENERATED into `~/.hermes/desktop-plugins/t3code/plugin.js`, a standalone Desktop plugin that is on by default. It is not shipped as `plugins/t3code/desktop/`, because Hermes installs a package's desktop half switched off until you enable it by hand.

To change a feature, edit only its folder under `features/`. After you change any `desktop.js`, `web.js` or `shared/desktop/*`, rebuild:

```sh
node plugins/t3code/scripts/build.mjs            # --check fails if generated files are stale
```

Hermes Desktop hot-reloads the rebuilt bundle by itself. Python changes need a backend restart.

## How isolation works

| Failure | What happens |
|---|---|
| A feature's REST module fails to import | Only `/api/plugins/t3code/<feature>/…` answers 503 with the error; the other features mount |
| A feature is switched off | Its REST answers 404 (checked per request), its UI is torn down; the other features keep running |
| A feature throws while registering (UI or agent) | Its partial registrations are rolled back and it is marked failed, with Restart on the settings page; the others start normally |
| A feature's panel crashes while rendering | That panel shows the error and Retry; small slots (row badges, status bar) just hide. Other panels render |
| A feature's timer, event listener, socket or palette command throws | The error is recorded against that feature (shown on the settings page), never thrown into the host |
| The backend is unreachable | The Desktop half keeps the last known switches and features keep running |
| The Claude CLI provider link can't be created | The Orchestrator still registers; the link failure is reported |
| `shared/` is broken, or the generated Desktop bundle has a syntax error | All of T3 Code is affected. This is the one shared risk, so keep `shared/` small |
| A feature hangs the backend process or the renderer thread | Not containable by error handling (same process). That would need process isolation |

## Native slash commands

In a chat on Claude CLI, Muse Code or Antigravity, the slash menu lists that agent's own commands first, and
picking one sends `/name args` to the agent itself. Names Hermes already uses stay Hermes commands; the
native one is namespaced: `/claude:compact`, `/muse:goal`, `/agy:plan`.

| Agent | Where the list comes from | How it runs |
|---|---|---|
| Claude CLI | Claude Code's initialize response (built-ins, skills, plugin and project commands), per workspace | Claude Code runs the prompt itself |
| Muse Code | `skill/list` (live session, or a throwaway read-only host) + `/compact`, `/goal`, `/rename` | skills as `skill` input parts; built-ins as MSP methods |
| Antigravity | the agent's ACP `available_commands_update` (`/plan`, `/logout`) | the agent parses the prompt |

Lists are cached in `plugin-data/t3code/commands.json` and refreshed in the background; a live session's
own list always wins. The hook needs the `agent/native_commands.py` host seam (Hermes gateway).

## Data and compatibility

- Data stays where it was: `plugin-data/t3-inbox/lifecycle.json` and `plugin-data/t3-orchestrator/orchestrator.db`.
- Desktop storage from the old plugins (`hermes.plugin.t3-inbox.*`, etc.) is copied into `hermes.plugin.t3code.<feature>.*` the first time each feature starts.
- Claude CLI settings are read from `plugins.entries.t3code.settings.claude_*`, falling back to the old `plugins.entries.claude-cli.settings`.
- The Claude CLI provider keeps its id `claude-cli`. Hermes discovers providers from `<home>/plugins/model-providers/`, so `model-providers/claude-cli` is a symlink to `features/claude_cli`. t3code recreates it whenever the link is missing. The provider only registers when t3code is enabled in that profile and the `claude_cli` switch is on.
- REST moved from `/api/plugins/t3-<feature>/` to `/api/plugins/t3code/<feature>/`. Orchestrator Desktop events moved from `plugin.t3-orchestrator.*` to `plugin.t3code.orchestrator.*`.

Feature details: `features/orchestrator/README.md`, `features/usage/README.md`, `features/claude_cli/README.md`.

