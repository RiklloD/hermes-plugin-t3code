# T3 Code · Orchestrator

T3 Code's orchestration V2, built into the Hermes chat. A feature of the `t3code` plugin. Agent half: `agent.py` + `core/`. REST: `api.py` (at `/api/plugins/t3code/orchestrator/`). Desktop: `desktop.js`. Web dashboard tab: `web.js`.

**In the chat (no separate panel):**

- Delegated agents appear in Hermes's native **Subagents frame above the composer**: model, live activity, elapsed time, transcript, Steer, Stop, and open the child chat. Results land in the chat as delegation cards.
- Typed follow-ups use the composer's **native queue** (edit, steer, park after Stop).
- A **context strip above the input** appears only when relevant:
  - "Delegated task of …" in child chats;
  - "Forked from … · Merge back" in fork chats;
  - merges included with your next message;
  - usage-limit auto-resume countdown with Cancel;
  - "Paused after Stop · Resume";
  - follow-ups queued by agents, schedules or your phone (edit, reorder, steer, remove).
- Schedules and recovery switches: **Settings → Plugins → T3 Code → Orchestrator**. Also the web dashboard tab and `/t3` on mobile.

| T3 feature | Hermes implementation |
|---|---|
| Agents delegate work | `t3_delegate` creates a real child session on any provider/model from the live catalog (`t3_capabilities`). The child gets only the brief. Async by default. The result returns to the parent as a delegation card when the parent is idle. `mode=wait` blocks. `client_request_id` is idempotent. Depth and concurrency are limited. |
| Cross-profile delegation | `t3_delegate` with `target.profile` runs the child AS another Hermes profile, with its own memory, skills, tools, config, workspace and default model (`t3_capabilities` lists the profiles). The result returns to the calling profile's thread. Turn it off with the `cross_profile_delegation` setting. A profile configured with `model.openai_runtime: codex_app_server` gives a real Codex CLI handoff. |
| Visible background work | The child's model, status, tool activity, todos, streamed tail, duration and result show in the Agents pane and as ↳ / ⚙ badges in the sidebar. A user **Stop** on the parent cancels every descendant task, even after the parent's turn has ended. It also holds the queue and pending wakes until the next user message. |
| Fork and merge back | Fork from any run, including failed or interrupted ones. Hermes-loop threads: `session.branch` copy, optionally switching provider/model (session scope only). Claude CLI threads (latest run): native `fork_session` via the claude-cli plugin. Native targets get a portable, bounded context handoff. Merge-back injects the fork's delta after the fork point: with the next message (`pre_llm_call`) on Hermes threads, or as a context turn on native runtimes. Every handoff is stored in `transfers`. |
| Steer or queue | Durable per-thread queue in SQLite. It survives restarts and drains in order when the thread is idle. Items can be added, edited, reordered, removed, held, or promoted into the running turn (steer = inject after the next tool call, restart = redirect). |
| Recovery & automation | **Continue threads after restarts** (default on, T3's `RestartContinuation`): after an update, quit or crash, every turn the restart cut gets "Continue where you left off." (see *Restart continuation* below). **Resume on usage limit** (default on): a turn that fails with a provider `resets_at` is resumed after the reset. **Schedules**: interval, fixed time with weekdays, or webhook (loopback listener, HMAC signature, `{{body.x}}` placeholders). Runs post into the bound thread (queued if busy) or start a new thread. Manage schedules from the pane, the web tab, or `/t3 schedule …` in any messaging app. |

## Architecture

- `core/authority.py` is the backend on current Hermes, where Desktop chats are `local-…` sessions owned by the messaging gateway's session authority (one per served profile), not `tui_gateway` sessions. RPCs run through an in-process operator `AuthorityConnection` on the gateway loop, under the Desktop user's subject, so children are normal sidebar chats. Events come from a wrap of `SessionEvents.publish`, and user Stop / prompts from wraps of `AuthorityConnection.interrupt` / `submit`. Cross-profile children go to the sibling authority serving that profile. `gw` uses it whenever `gateway.run_api._ACTIVE_RUNNER` has an authority. Not ported yet: transcript-copy forks (`unsupported`), the Subagents frame intercepts, and restart continuation (the authority's durable FIFO recovers cut turns itself).
- `core/gw.py` dispatches gateway RPCs in-process inside `hermes serve` (`session.create`, `prompt.submit`, `session.steer|redirect|interrupt|branch|resume`, `config.set`). It observes events through a passive tap on `server.write_json` and wraps `session.interrupt` and `prompt.submit` as observers. No Hermes core files are edited.
- `core/engine.py` is the process-wide engine. An owner file lock means only one process runs side effects. A 2-second tick handles queue drains, result delivery, transfers, recoveries, schedules and reconciliation.
- `core/store.py` keeps state in `~/.hermes/plugin-data/t3-orchestrator/orchestrator.db`, shared by all profiles (the folder keeps its pre-merge name).
- Automatic turns use core's claim and admission path (`_notif_claim_turn` + `_run_prompt_submit`), so they respect busy sessions and the Stop latch, and only an admitted turn counts as delivered.

## Behaviour notes

- Identity: records use the session's lineage root (the Desktop's durable id). Live lookups follow compression to the current tip, so a compressed parent or child keeps its tasks, queue and Stop.
- A child whose turn ends while its own delegated tasks still run goes to `waiting_children`. Its result is published only after their results come back and it finishes again.
- Stop and cancel write durable intent across the whole task tree, including grandchildren of finished children. The owning backend enforces it, so `/t3 cancel` from a phone really stops work.
- Automatic turns (queue drains, result cards, recoveries, schedules) count as sent only once the host admits them. The claim, the hold/Stop re-check and the submission run under the lock the Stop observer takes.
- Claude CLI parents get the same six tools as `mcp__t3__*` through an in-process SDK MCP server (claude-cli `t3_bridge.py`). Muse Code and Antigravity parents get them as T3 Code does it: a session MCP server (`shared/orchestration.py`, a loopback Streamable-HTTP endpoint with a per-runtime bearer token). Steering a native session is interrupt-and-restart.
- Native subagents join the Subagents frame too (`chatframe.native_update`): Muse Code subagents (Stop = `subagent/stop`, Steer = `subagent/sendMessage`) and workflow children (Stop = `workflow/cancel`), and Antigravity subagent batches (status and result only; the agent offers no per-child control). Forks of Muse Code chats use `session/fork`; Antigravity has no native fork, so its forks carry a portable handoff.
- **Restart continuation** (`core/restart.py` + `Engine._continue_restarted`, after T3's `RestartContinuation.ts` / `RestartBackgroundNote.ts`):
  - *Intent*: core's exit path interrupts every turn, which clears its crash marker. So `prepare_for_shutdown` (a wrap of `server._stop_turns_before_exit`) records each running turn first: prompt, runtime, background work, Stop or pending-question state. Against crashes, each backend keeps a `live_work` row per running turn (refreshed every 2 s). Rows a dead process left behind, and core's crash markers, become intent at the next start. Frames from the interrupted turns are ignored, so a cut child task is not reported as interrupted.
  - *Delivery* (before queue drains, so queued messages wait behind the cut turn): skipped when the user sent something since, pressed Stop, the chat is settled, snoozed (Inbox) or archived, it is waiting on an approval or question, the turn was a slash command, or it already restarted 3 times in a row (`MAX_CONTINUATION_CHAIN`). Otherwise it retries until the chat is idle, for up to 6 h.
  - *Text*: "Continue where you left off." For Hermes-loop crashes, core's own note carrying the request (a hard crash may have persisted nothing). Background work the restart killed (native subagents, background processes that did not survive) is listed in a bounded note before it. A settled turn that was waiting on a subagent or monitor is woken with the note alone. Plain shells only add the note to the next message.
  - A delegated child that is not continued is finalized as interrupted, so its parent gets a result. Hermes-loop crash markers still go through core's bounded auto-continue on `session.resume`.
  - The Inbox's own crash resume stands down while this switch is on.
- Usage-limit resumes re-arm if the same window fails again, at most three times.
- Signed webhooks only fill `{{body…}}` placeholders, and replays within 24h are ignored.

## Limits

- Delegation, forks, queue drains and recovery need the Desktop backend (`hermes serve`). Other processes record intent, which that backend acts on.
- Child agents can't ask the user questions mid-task (no client is attached). Open the child thread to talk to it.
- Native Claude CLI forks are pinned to the source's last native message, and only the latest run can be forked natively. Earlier points use the transcript path. A custom Claude `config_directory` falls back to forking the latest state.
- Webhook URLs are loopback. Put your own tunnel in front for internet senders, and always set a signature.
- After updating the plugin's code, restart the backend. The core is loaded once per process.

## Tests

```
HERMES_HOME=$(mktemp -d) hermes-agent/scripts/run-in-hermes-env bash -c '"$__HERMES_TEST_PYTHON" -m pytest plugins/t3code/features/orchestrator -q'
```
