# T3 Code · Antigravity

A feature of the `t3code` plugin, ported from T3 Code's Antigravity driver. Hermes discovers it through the `<home>/plugins/model-providers/antigravity` symlink; it registers only while t3code is enabled and its `antigravity` switch is on.

Runs Google's official **Antigravity ACP agent** (`agy_acp_server`, not the `agy` CLI) with its own sign-in, separate from the Antigravity IDE/CLI. Like T3: one private `GEMINI_HOME` shared by every Hermes profile (`~/.hermes/plugin-data/t3code/antigravity/profile`), file token storage, ambient Google credentials stripped, `authenticate` eagerly, `session/resume` instead of replaying history, cancel waits for the prompt to settle. Your global skills (`~/.gemini/config/skills`, `~/.gemini/antigravity-cli/skills`) are linked into the profile.

## Setup

```sh
<hermes python> <home>/plugins/model-providers/antigravity/setup.py install   # only if no runtime yet
<hermes python> <home>/plugins/model-providers/antigravity/setup.py login     # Google sign-in in the browser
```

The runtime is resolved from `antigravity_binary_path`, then Hermes's managed install (`~/.hermes/plugin-data/t3code/antigravity-acp`), then T3 Code's (`~/.t3/tools/antigravity-acp`), so a runtime T3 already downloaded is reused. `install` downloads T3's pinned 1.3.0 release and verifies size and SHA-256. The exact commands are in the provider's setup status. A first turn without a sign-in also opens the Google page and waits.

Models come from your account. Each session saves the list it reports; until the first Hermes session, the catalog T3 Code cached for the same account (`~/.t3/caches/antigravity.json`) is used; discovery only spawns a probe when nothing is cached (the agent unpacks ~1 GB per launch).

## Settings (`plugins.entries.t3code.settings`)

| Key | Default | |
|---|---|---|
| `antigravity_auth_method` | `oauth-personal` | `oauth-business`, `gemini-api-key`, `agent-platform` |
| `antigravity_api_key` / `_gcp_project` / `_gcp_location` | — | for API-key / Enterprise / Agent Platform methods |
| `antigravity_runtime_mode` | `approval-required` | `auto-accept-edits` (auto_edit), `full-access` (yolo) |
| `antigravity_binary_path` | managed | manual install; `localharness_external` must sit beside it |
| `antigravity_profile_directory` | shared | GEMINI_HOME |

Use Antigravity's own `/plan`. It cannot rewind its conversation, so Hermes undo/regenerate is unavailable, as with the other native runtimes.

## Orchestrator and Usage

With the Orchestrator on, Antigravity gets the `t3_*` tools through an ACP `http` MCP server, and each subagent batch (`invoke_subagent` + `start_subagent`) appears in the Subagents frame with its tasks and result. Usage ▸ Limits shows Google's Gemini and shared Claude/GPT 5-hour and weekly windows; Usage ▸ Tokens reads its conversation databases (also the Antigravity IDE/CLI's and T3 Code's).
