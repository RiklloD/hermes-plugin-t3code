# T3 Code · Muse Code

A feature of the `t3code` plugin, ported from T3 Code's `packages/provider-muse`. Hermes discovers it through the `<home>/plugins/model-providers/muse-code` symlink that t3code maintains; it registers only while t3code is enabled and its `muse_code` switch is on.

Choose **Muse Code** in the model picker in a **new chat**. Hermes shows the conversation; the installed `muse` CLI owns the prompt, tools, agent loop, approvals and persisted session. The plugin speaks the Muse Session Protocol (newline JSON-RPC over `muse serve`) directly, as T3 does through `@muse-code/sdk`: `session/start` with a minted UUIDv7 id, `session/resume` on later turns or after a restart, `turn/start` / `turn/interrupt`, `approval/decide`, `userInput/answer`.

Install Muse Code and run `muse login`. Models come from `model/list` (a read-only, no-shell, no-write host). MSP has no auth signal, so a logged-out account shows on the first turn.

## Settings (`plugins.entries.t3code.settings`)

| Key | Default | |
|---|---|---|
| `muse_binary_path` | `muse` on PATH | Muse Code binary |
| `muse_runtime_mode` | `approval-required` | `full-access` disables the sandbox and approves everything (`allowAll`); needs a new chat |
| `muse_reasoning_effort` | model default | unsupported values fall back to the model's default |

Approvals Muse cannot settle itself (its judge approves many) open the Hermes approval prompt; questions use Hermes's question UI. Hermes undo/regenerate, forks and Hermes tools are not native-session operations, as with Claude CLI.

## Orchestrator and Usage

With the Orchestrator on, Muse gets the `t3_*` tools through a session MCP server (`t3-code`, optional mode, as in T3), its subagents and workflow children appear in the Subagents frame with Stop/Steer, and Orchestrator forks use `session/fork`. Its 5-hour/weekly windows (`usage/changed`) are saved to `plugin-data/t3code/muse-usage.json` for Usage ▸ Limits; Usage ▸ Tokens reads its session journals.
