# T3 Code · Claude CLI

A feature of the `t3code` plugin. Hermes discovers it through the `<home>/plugins/model-providers/claude-cli` symlink that t3code maintains. It registers only while t3code is enabled and its `claude_cli` switch is on.

Choose **Claude CLI** in Hermes's normal model picker. Hermes displays the conversation; the installed Claude Code CLI owns the prompt, tools, agent loop, permissions, native context and persisted session. The plugin controls it through the official Python Claude Agent SDK. Hermes tools and its agent prompt are not sent to Claude.

This is a prepared first implementation, not a claim of complete T3 Code feature parity. It needs the accompanying generic native-runtime host patch; it cannot run on unpatched Hermes merely by copying this directory.

## Install

The port bundle's `README.md` gives the exact host-patch and local-plugin installation commands. Install the official Claude Code CLI and run `claude auth login` first. Use Hermes's package manager for the declared SDK dependency; do not pip-install into Hermes's managed environment.

After installation, fully restart Hermes Desktop and its backend, create a **new chat**, choose **Claude CLI**, choose a model, select your workspace and send a message. Native runtime selection belongs to a new conversation. Model changes within Claude CLI preserve the native session.

The provider discovers models from the installed CLI's account handshake without a model inference call. A temporary probe disables project/user hooks and MCP servers and does not persist a session. If discovery fails, the CLI's stable `default`, `sonnet`, `opus` and `haiku` aliases remain available; availability still depends on native account access.

## Models and context window

The account handshake's `resolvedModel` supplies the picker IDs, so the current Opus, Sonnet and Haiku releases show their actual versions (including 5.5) without a hardcoded release list. The CLI's aliases remain valid when typed and are the offline discovery fallback.

For a versioned model with native 1M context, the regular picker row uses its full native window. The `[200k]` row applies Claude's real 200K cap, not just a Hermes display override. A **Context 1M / Context 200K** menu appears in the desktop status bar while a versioned Claude CLI model is selected. It changes only the focused chat, is disabled during a turn, and preserves Claude's native conversation across the process restart required to change the context cap. Claude still owns compaction. Older 4.6 models do not gain 1M access unless the native account exposes an entitled variant.

After updating the Python feature, restart the Hermes backend to load it; the generated desktop half hot-reloads independently. No account credentials, global Claude settings or unrelated profile configurations are changed by a context selection.

## Two native accounts

Sign in to the second account with `CLAUDE_CONFIG_DIR="$HOME/.claude-account2" claude auth login --claudeai`. Keep the first account signed in. After fully quitting and reopening Hermes, select **Claude CLI 2** in a **new chat** for the second subscription; **Claude CLI** retains the original account. Both support native tools, model discovery, context selection and account-bound resume. The **Claude** status menu shows each login; **Usage** shows separate account totals and subscription limits. There is no proxy or automatic routing. `claude_second_config_directory` optionally overrides the second directory.

## Settings

Hermes stores these under `plugins.entries.t3code.settings` as `claude_*` keys (declared in t3code's `plugin.yaml`, so they appear on its settings page). The pre-merge `plugins.entries.claude-cli.settings` is still read as a fallback:

```yaml
plugins:
  isolation: in_process
  entries:
    t3code:
      settings:
        claude_cli: true
        claude_executable: claude
        claude_permission_mode: default
        claude_auth_mode: subscription
        claude_setting_sources: [user, project, local]
        claude_config_directory: ""
```

`default` retains Claude's own permission policy. When Claude asks for permission, the bridge opens the existing Hermes approval request; unanswered requests are denied. Claude's own already-allowed operations do not generate extra requests. `AskUserQuestion` uses Hermes's existing question UI. Stop interrupts Claude and drains its terminal result before another turn can run.

`subscription` clears ambient API-key and backend override variables from the child environment so Hermes credentials do not accidentally replace native login. `native` preserves those overrides for an intentionally configured API/backend route. Native settings may themselves select authentication helpers. The plugin never reads or exports Claude's token store. `config_directory` selects a separate native Claude configuration directory; changing it requires a new conversation. `executable` overrides the runtime binary; the structural Hermes provider check still requires the installed `claude` command to resolve.

Native settings, CLAUDE.md, hooks, skills and configured MCP servers are handled by Claude. Set `setting_sources: []` to opt out of user/project/local settings. Native execution is local to the selected workspace; Hermes's Docker/SSH terminal backends do not wrap Claude tools.

## First-version boundaries

- Native session resume, streamed root text/thinking, native tool cards/results, permissions, questions and Stop are bridged. Root native subagent tool calls remain usable; detailed child progress is not rendered as independent Hermes child sessions.
- Hermes keeps a display mirror. Native history is never reconstructed by replaying it. Workspace changes, copied native session identities and rewound mirrors are refused before another native task executes.
- Hermes undo/regenerate, transcript forks, Hermes manual compression, MoA, fallback providers and Hermes tool/skill execution are not native-session operations. Start a new chat for a different harness. Claude's automatic compaction remains active.
- Hermes effort/fast controls, native slash-command interception and advanced T3 background-task management are not wired in this version. Use native settings where applicable. Auxiliary Hermes model-only requests need a separate provider.
- Usage comes from native result messages. Native USD totals are not reported as subscription charges.
- `plugins.isolation: host` is not supported for session-owned runtimes. The plugin runs in-process and owns one separate Claude process per active conversation.
- Desktop JSON-RPC and picker integration have been exercised with the real CLI. The packaged Electron window has not been visually tested or activated in the user's normal installation.

## Development

With patched Hermes and this plugin's declared SDK dependency available in the interpreter:

```sh
python -m unittest discover -s tests -v
```

Host contract/regression tests live in the host patch under `tests/agent/test_native_runtime.py`; run them with Hermes's canonical `scripts/run_tests.sh`. See the bundle's `VALIDATION.md` for actual live checks and receipts.

The design follows T3 Code's official-SDK/native-executable architecture. See `THIRD_PARTY_NOTICES.md` for the inspected upstream revision and license attribution.
