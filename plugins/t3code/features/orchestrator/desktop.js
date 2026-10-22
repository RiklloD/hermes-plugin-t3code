/**
 * T3 Code · Orchestrator — orchestration V2 as part of the Hermes chat itself (no separate pane).
 *
 * A T3 Code feature module (see shared/desktop/shell.js); bundled into desktop/plugin.js by
 * scripts/build.mjs — edit this file, then rebuild.
 *
 *   - Delegated agents appear in Hermes's native Subagents frame above the composer (the backend
 *     merges them into subagent.list / subagent.* events): model, activity, elapsed, transcript,
 *     Steer and Stop, open the child chat. Results land in the chat as delegation cards.
 *   - Typed follow-ups use the composer's native queue (edit / steer / park after Stop).
 *   - This plugin adds the rest inside the composer chrome:
 *       composer.top     — a context strip, shown only when it has something to say:
 *                          "delegated task of …", "forked from … · Merge back", pending merges,
 *                          usage-limit auto-resume countdown, paused-after-Stop, and follow-ups
 *                          queued by agents / schedules / your phone (durable, server-side).
 *   - Schedules and recovery switches live in Settings → Plugins → T3 Code → Orchestrator.
 *   - Sidebar rows: ↳ for delegated child chats, ⚙N for chats with running children.
 */
import * as sdk from '@hermes/plugin-sdk'
import React from 'react'

const { host, atom, useValue, useQuery, queryClient, Tip, SESSION_ROW_AREAS, COMPOSER_AREAS } =
  sdk

const h = React.createElement
const { useState } = React

// T3 Code feature identity: NS keys palette commands and queries; the backend broadcasts
// plugin.t3code.orchestrator.<event> (features/orchestrator/core/engine.py).
const NS = 't3code.orchestrator'
const API_BASE = '/api/plugins/t3code/orchestrator'
const EVENT = name => `plugin.t3code.orchestrator.${name}`
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted'])

let ctxRef = null
let $now = atom(Date.now())
let $tasks = atom({})

const rest = (path, opts) =>
  ctxRef?.rest
    ? ctxRef.rest(path, { timeoutMs: 60_000, ...(opts || {}) })
    : window.hermesDesktop.api({ path: `${API_BASE}${path}`, timeoutMs: 60_000, ...(opts || {}) })
const post = (path, body) => rest(path, { method: 'POST', body })
const invalidate = () => queryClient.invalidateQueries({ queryKey: [NS] })

function errText(err) {
  const d = err?.detail || err?.body?.detail || err?.data?.detail
  return (d && (d.message || d)) || err?.message || String(err)
}

function toast(message, kind = 'info') {
  try {
    host.notify({ kind, message: String(message), title: 'Orchestrator' })
  } catch {}
}

async function act(fn, ok) {
  try {
    const res = await fn()
    if (ok) toast(typeof ok === 'function' ? ok(res) : ok, 'success')
    invalidate()
    return res
  } catch (err) {
    toast(errText(err), 'error')
    return null
  }
}

function openThread(key, profile, intent = 'tab') {
  if (!key) return
  try {
    host.openSession(key, { intent, profile: profile && profile !== 'default' ? profile : undefined })
  } catch (err) {
    toast(errText(err), 'error')
  }
}

const clock = ts => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—')

function until(ts, now) {
  const d = Math.max(0, Math.round(ts - now / 1000))
  if (d < 60) return `${d}s`
  if (d < 3600) return `${Math.floor(d / 60)}m`
  return `${Math.floor(d / 3600)}h ${Math.floor((d % 3600) / 60)}m`
}

const CSS = `
.t3o-strip{display:flex;flex-direction:column;gap:2px;padding:4px 8px 2px;font-size:12.5px;color:var(--ui-text-secondary)}
.t3o-line{display:flex;align-items:center;gap:6px;min-width:0;min-height:24px}
.t3o-line .t3o-ic{flex-shrink:0;width:16px;text-align:center;color:var(--ui-text-tertiary)}
.t3o-line .t3o-tx{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t3o-line b{font-weight:500;color:var(--ui-text-primary)}
.t3o-line[data-tone=warn] .t3o-ic{color:var(--ui-yellow)}
.t3o-line[data-tone=info] .t3o-ic{color:var(--ui-blue)}
.t3o-btn{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;gap:4px;height:22px;padding:0 7px;border-radius:6px;font-size:12px;
  color:var(--ui-text-tertiary);cursor:pointer;white-space:nowrap;flex-shrink:0}
.t3o-btn:hover{background:var(--ui-row-hover-background);color:var(--ui-text-primary)}
.t3o-btn[data-primary]{color:var(--ui-accent)}
.t3o-btn[data-danger]:hover{color:var(--ui-red)}
.t3o-btn:disabled{opacity:.4;pointer-events:none}
.t3o-btn:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.t3o-q .t3o-qa{display:flex;opacity:0;transition:opacity .1s}
.t3o-q:hover .t3o-qa,.t3o-q:focus-within .t3o-qa{opacity:1}
.t3o-edit{flex:1;min-width:0;font:inherit;font-size:12.5px;padding:2px 6px;border-radius:5px;border:1px solid var(--ui-stroke-secondary);
  background:transparent;color:var(--ui-text-primary);outline:none}
.t3o-edit:focus{border-color:var(--ui-accent)}
.t3o-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.t3o-select,.t3o-input,.t3o-textarea{box-sizing:border-box;font:inherit;font-size:12.5px;padding:4px 7px;border-radius:6px;border:1px solid var(--ui-stroke-secondary);
  background:transparent;color:var(--ui-text-primary);outline:none;min-width:0}
.t3o-textarea{width:100%;min-height:60px;resize:vertical}
.t3o-badge{display:inline-flex;align-items:center;font-size:10.5px;padding:0 4px;height:15px;border-radius:4px;background:var(--ui-row-hover-background);
  color:var(--ui-text-tertiary);white-space:nowrap}
.t3o-badge[data-live]{color:var(--ui-blue)}
.t3o-settings{display:flex;flex-direction:column;gap:14px;font-size:13px;max-width:720px}
.t3o-settings h3{margin:0 0 4px;font-size:14px}
.t3o-card{border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:8px 10px}
.t3o-dim{color:var(--ui-text-tertiary);font-size:12px}
`

function Btn({ label, onClick, title, primary, danger, disabled }) {
  const b = h(
    'button',
    {
      className: 't3o-btn',
      'data-danger': danger ? '' : undefined,
      'data-primary': primary ? '' : undefined,
      disabled,
      onClick: e => {
        e.stopPropagation()
        onClick?.(e)
      },
      type: 'button'
    },
    label
  )
  return title && Tip ? h(Tip, { label: title }, b) : b
}

function Line({ icon, tone, children, actions }) {
  return h(
    'div',
    { className: 't3o-line', 'data-tone': tone },
    h('span', { className: 't3o-ic', 'aria-hidden': true }, icon),
    h('span', { className: 't3o-tx' }, children),
    actions
  )
}

// ─── data ───────────────────────────────────────────────────────────────────
function useOverview(key, profile) {
  return useQuery({
    enabled: !!key,
    queryFn: () =>
      rest(`/overview?session_key=${encodeURIComponent(key)}${profile ? `&profile=${encodeURIComponent(profile)}` : ''}`),
    queryKey: [NS, 'overview', key],
    // Orchestrator events invalidate this on every change; the poll is a backstop.
    refetchInterval: 15_000,
    retry: false
  })
}

function useFocused() {
  const key = useValue(host.state.focusedStoredSessionId)
  const profile = useValue(host.state.focusedSessionProfile || host.state.profile)
  return { key, profile }
}

// ─── composer.top: the in-chat context strip ────────────────────────────────
function QueueLine({ item, n, ids, sessionKey, running }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(item.text)
  const q = body => post(`/queue/${encodeURIComponent(sessionKey)}`, body)

  if (editing) {
    return h(
      'div',
      { className: 't3o-line' },
      h('span', { className: 't3o-ic' }, '↻'),
      h('input', {
        autoFocus: true,
        className: 't3o-edit',
        onChange: e => setText(e.target.value),
        onKeyDown: e => {
          if (e.key === 'Enter') act(() => q({ action: 'edit', id: item.id, text })).then(() => setEditing(false))
          if (e.key === 'Escape') setEditing(false)
        },
        value: text
      }),
      h(Btn, { label: 'Save', onClick: () => act(() => q({ action: 'edit', id: item.id, text })).then(() => setEditing(false)), primary: true })
    )
  }

  const source = item.createdBy === 'schedule' ? 'scheduled' : item.createdBy === 'agent' ? 'from agent' : 'queued'
  return h(
    'div',
    { className: 't3o-line t3o-q' },
    h('span', { className: 't3o-ic' }, '↻'),
    h('span', { className: 't3o-tx', onDoubleClick: () => setEditing(true), title: item.text }, h('span', { className: 't3o-dim' }, `${source} · `), item.text),
    h(
      'span',
      { className: 't3o-qa' },
      n > 1 ? h(Btn, { label: '↑', onClick: () => act(() => q({ action: 'reorder', before_id: ids[n - 2], id: item.id })), title: 'Move up' }) : null,
      n < ids.length ? h(Btn, { label: '↓', onClick: () => act(() => q({ action: 'reorder', before_id: ids[n + 1] || null, id: item.id })), title: 'Move down' }) : null,
      h(Btn, { label: 'Edit', onClick: () => setEditing(true) }),
      running ? h(Btn, { label: 'Steer', onClick: () => act(() => q({ action: 'promote', how: 'steer', id: item.id }), 'Steered into the running turn'), primary: true }) : null,
      h(Btn, { danger: true, label: '✕', onClick: () => act(() => q({ action: 'remove', id: item.id })), title: 'Remove' })
    )
  )
}

function ContextStrip() {
  const { key, profile } = useFocused()
  const now = useValue($now)
  const { data } = useOverview(key, profile)

  if (!key || !data) return null

  const lines = []
  const lin = data.lineage || {}
  const queue = data.queue || { items: [] }
  const armed = (data.recoveries || []).filter(r => r.status === 'armed' && r.kind !== 'restart')
  const restarting = (data.recoveries || []).filter(r => r.status === 'armed' && r.kind === 'restart')
  const pendingDelivery = (data.tasks || []).filter(t => t.delivery === 'pending').length

  if (data.asChild) {
    const t = data.asChild
    lines.push(
      h(Line, {
        actions: h(Btn, { label: 'Open parent', onClick: () => openThread(t.parentSessionKey, t.parentProfile) }),
        icon: '↳',
        key: 'child',
        tone: 'info'
      }, 'Delegated task of ', h('b', null, data.parentTitle || 'its parent chat'), ` · ${t.status}${t.crossProfile ? ` · as ${t.profile}` : ''}`)
    )
  }

  if (lin.forked_from) {
    lines.push(
      h(Line, {
        actions: h(
          React.Fragment,
          null,
          h(Btn, { label: 'Open source', onClick: () => openThread(lin.forked_from.source_key, profile) }),
          h(Btn, {
            label: 'Merge back',
            onClick: () => act(() => post('/merge', { fork_key: key, profile }), r => r?.note || 'Merge queued'),
            primary: true,
            title: 'Bring what this fork did back into the source chat'
          })
        ),
        icon: '⑂',
        key: 'fork'
      }, 'Forked from ', h('b', null, data.forkedFromTitle || 'another chat'))
    )
  }

  for (const x of data.pendingTransfers || []) {
    lines.push(
      h(Line, { icon: '⇄', key: x.id, tone: 'info' },
        x.type === 'merge_back' ? 'Work merged from a fork' : 'Context handoff',
        x.delivery === 'next_message' ? ' is included with your next message' : ' arrives when this chat is idle')
    )
  }

  for (const r of armed) {
    lines.push(
      h(Line, {
        actions: h(Btn, { danger: true, label: 'Cancel', onClick: () => act(() => post(`/recoveries/${r.id}/cancel`)) }),
        icon: '⏳',
        key: r.id,
        tone: 'warn'
      }, 'Usage limit reached — continues automatically at ', h('b', null, clock(r.reset_at)), ` (in ${until(r.reset_at, now)})`)
    )
  }

  for (const r of restarting) {
    lines.push(
      h(Line, {
        actions: h(Btn, { danger: true, label: 'Cancel', onClick: () => act(() => post(`/recoveries/${r.id}/cancel`)) }),
        icon: '↻',
        key: r.id,
        tone: 'info'
      }, 'Hermes restarted — this turn continues where it left off')
    )
  }

  if (queue.held && (queue.items.length || pendingDelivery)) {
    const what = [queue.items.length ? `${queue.items.length} queued` : null, pendingDelivery ? `${pendingDelivery} result(s) waiting` : null]
      .filter(Boolean)
      .join(', ')
    lines.push(
      h(Line, {
        actions: h(Btn, { label: 'Resume', onClick: () => act(() => post(`/queue/${encodeURIComponent(key)}`, { action: 'release' })), primary: true }),
        icon: '⏸',
        key: 'held',
        tone: 'warn'
      }, `Paused after Stop — ${what}`)
    )
  }

  const ids = queue.items.map(i => i.id)
  queue.items.forEach((item, i) =>
    lines.push(h(QueueLine, { ids, item, key: item.id, n: i + 1, running: data.running, sessionKey: key }))
  )

  if (!lines.length) return null
  return h('div', { className: 't3o-strip' }, h('style', null, CSS), lines)
}

// ─── settings page: recovery, cross-profile, schedules ─────────────────────
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function NewSchedule({ onDone }) {
  const [prompt, setPrompt] = useState('')
  const [kind, setKind] = useState('interval')
  const [every, setEvery] = useState(60)
  const [unit, setUnit] = useState('m')
  const [time, setTime] = useState('09:00')
  const [days, setDays] = useState([1, 2, 3, 4, 5])
  const { key, profile } = useFocused()
  const [bind, setBind] = useState(!!key)
  const create = () => {
    const schedule =
      kind === 'interval'
        ? { everyMs: Math.max(1, every) * (unit === 'm' ? 60_000 : unit === 'h' ? 3_600_000 : 86_400_000), type: 'interval' }
        : kind === 'fixed_time'
          ? { timeOfDay: time, type: 'fixed_time', ...(days.length && days.length < 7 ? { weekdays: days } : {}) }
          : { type: 'webhook' }
    act(() => post('/schedules', { bind_to_current_thread: bind, profile, prompt, schedule, session_key: bind ? key : null }), r =>
      r?.webhookUrl ? `Webhook: ${r.webhookUrl}` : 'Scheduled'
    ).then(r => r && onDone())
  }
  return h(
    'div',
    { className: 't3o-card', style: { display: 'flex', flexDirection: 'column', gap: 6 } },
    h('textarea', { className: 't3o-textarea', onChange: e => setPrompt(e.target.value), placeholder: 'Prompt to run…', value: prompt }),
    h(
      'div',
      { className: 't3o-row' },
      h('select', { className: 't3o-select', onChange: e => setKind(e.target.value), value: kind },
        h('option', { value: 'interval' }, 'Every…'), h('option', { value: 'fixed_time' }, 'At a time'), h('option', { value: 'webhook' }, 'On webhook')),
      kind === 'interval'
        ? h(React.Fragment, null,
            h('input', { className: 't3o-input', min: 1, onChange: e => setEvery(+e.target.value), style: { width: 64 }, type: 'number', value: every }),
            h('select', { className: 't3o-select', onChange: e => setUnit(e.target.value), value: unit },
              h('option', { value: 'm' }, 'minutes'), h('option', { value: 'h' }, 'hours'), h('option', { value: 'd' }, 'days')))
        : kind === 'fixed_time'
          ? h('input', { className: 't3o-input', onChange: e => setTime(e.target.value), type: 'time', value: time })
          : null
    ),
    kind === 'fixed_time'
      ? h('div', { className: 't3o-row' }, WEEKDAYS.map((d, i) =>
          h('label', { key: d, style: { display: 'flex', gap: 3, alignItems: 'center' } },
            h('input', { checked: days.includes(i), onChange: e => setDays(e.target.checked ? [...days, i].sort() : days.filter(x => x !== i)), type: 'checkbox' }), d)))
      : null,
    key ? h('label', { style: { display: 'flex', gap: 6 } }, h('input', { checked: bind, onChange: e => setBind(e.target.checked), type: 'checkbox' }), 'Post into the current chat (otherwise a new chat per run)') : null,
    h('div', { className: 't3o-row' }, h(Btn, { disabled: !prompt.trim(), label: 'Create', onClick: create, primary: true }), h(Btn, { label: 'Cancel', onClick: onDone }))
  )
}

function SettingsPage() {
  const settings = useQuery({ queryFn: () => rest('/settings'), queryKey: [NS, 'settings'] })
  const sched = useQuery({ queryFn: () => rest('/schedules'), queryKey: [NS, 'schedules'], refetchInterval: 15000 })
  const [adding, setAdding] = useState(false)
  const s = settings.data || {}
  const toggle = (k, label, hint) =>
    h('label', { style: { display: 'flex', gap: 8, alignItems: 'flex-start' } },
      h('input', { checked: !!s[k], onChange: e => act(() => post('/settings', { [k]: e.target.checked })), type: 'checkbox' }),
      h('span', null, label, hint ? h('div', { className: 't3o-dim' }, hint) : null))
  return h(
    'div',
    { className: 't3o-settings' },
    h('style', null, CSS),
    h('div', null,
      h('h3', null, 'Recovery & delegation'),
      toggle('continue_after_restart', 'Continue threads after restarts', 'After an update, quit or crash, turns and delegated tasks the restart cut short get “Continue where you left off.”, with a note of background work it cancelled. Not for settled, snoozed or archived chats, chats waiting on your answer, or a turn that restarted 3 times in a row.'),
      toggle('resume_on_usage_limit', 'Resume when a usage limit resets', 'Sends “Continue where you left off.” once the provider limit lifts.'),
      toggle('cross_profile_delegation', 'Allow delegating to other profiles', 'A chat may hand a task to another profile, which runs as itself (its memory, persona, model).'),
      toggle('harness_switch', 'Switch harness inside a chat', 'Pick Claude CLI in a chat that started on another model (or the reverse): the chat continues with a context handoff instead of needing a new chat.')),
    h('div', null,
      h('div', { className: 't3o-row', style: { justifyContent: 'space-between' } }, h('h3', null, 'Scheduled tasks'), h(Btn, { label: '+ New', onClick: () => setAdding(true), primary: true })),
      adding ? h(NewSchedule, { onDone: () => setAdding(false) }) : null,
      (sched.data?.schedules || []).map(x =>
        h('div', { className: 't3o-card', key: x.id, style: { marginTop: 6 } },
          h('div', { className: 't3o-row', style: { justifyContent: 'space-between' } },
            h('b', null, x.title), h('span', { className: 't3o-dim' }, `${x.describe}${x.enabled && x.nextRunAt ? ` · next ${clock(x.nextRunAt)}` : x.enabled ? '' : ' · paused'}`)),
          x.webhookUrl ? h('div', { className: 't3o-dim', title: x.webhookNote }, x.webhookUrl) : null,
          h('div', { className: 't3o-row' },
            h(Btn, { label: x.enabled ? 'Pause' : 'Resume', onClick: () => act(() => rest(`/schedules/${x.id}`, { body: { enabled: !x.enabled }, method: 'PATCH' })) }),
            h(Btn, { label: 'Run now', onClick: () => act(() => post(`/schedules/${x.id}/run`), r => `Run: ${r?.status}`) }),
            x.bindSessionKey ? h(Btn, { label: 'Open chat', onClick: () => openThread(x.bindSessionKey, x.profile) }) : null,
            h(Btn, { danger: true, label: 'Delete', onClick: () => act(() => rest(`/schedules/${x.id}`, { method: 'DELETE' })) })))),
      !(sched.data?.schedules || []).length && !adding ? h('div', { className: 't3o-dim' }, 'No schedules. Agents can create them with t3_schedule; on mobile use /t3 schedule.') : null)
  )
}

// ─── sidebar badges ─────────────────────────────────────────────────────────
function RowBadge({ sessionId }) {
  const tasks = useValue($tasks)
  let child = null
  let running = 0
  for (const t of Object.values(tasks)) {
    if (t.childSessionKey === sessionId) child = t
    if (t.parentSessionKey === sessionId && !TERMINAL.has(t.status)) running++
  }
  if (!child && !running) return null
  return h(
    React.Fragment,
    null,
    h('style', null, CSS),
    child ? h('span', { className: 't3o-badge', 'data-live': !TERMINAL.has(child.status) ? '' : undefined, title: `Delegated task · ${child.provider}/${child.model}` }, '↳') : null,
    running ? h('span', { className: 't3o-badge', 'data-live': '', title: `${running} delegated agent(s) running` }, `⚙${running}`) : null
  )
}

async function refreshTasks() {
  try {
    const res = await rest('/tasks?scope=recent&limit=200')
    const map = {}
    for (const t of res?.tasks || []) map[t.taskId] = t
    $tasks.set(map)
  } catch {}
}

export default {
  id: 'orchestrator',
  name: 'Orchestrator',
  register(ctx) {
    ctxRef = ctx
    $now = atom(Date.now())
    $tasks = atom({})
    ctx.setInterval(() => $now.set(Date.now()), 1000)
    ctx.setInterval(() => !document.hidden && void refreshTasks(), 30_000)
    void refreshTasks()

    for (const name of ['task.updated', 'queue.updated', 'lineage.updated', 'recovery.updated', 'schedules.updated', 'session.stopped', 'session.turn']) {
      ctx.onEvent(EVENT(name), ev => {
        if (name === 'task.updated' && ev?.payload?.taskId) $tasks.set({ ...$tasks.get(), [ev.payload.taskId]: ev.payload })
        if (name === 'recovery.updated' && ev?.payload?.kind === 'restart' && ev.payload.status === 'fired') {
          toast(ev.payload.title ? `${ev.payload.summary}: “${ev.payload.title}”` : ev.payload.summary)
        }
        invalidate()
      })
    }

    const top = COMPOSER_AREAS?.top || 'composer.top'
    ctx.register({ area: top, id: 'context-strip', order: 50, render: () => h(ContextStrip, null) })

    if (SESSION_ROW_AREAS?.trailing) {
      ctx.register({ area: SESSION_ROW_AREAS.trailing, data: { render: ({ sessionId }) => h(RowBadge, { sessionId }) }, id: 'row-badge' })
    }

    ctx.registerSettingsPage?.({ icon: 'git-branch', id: 'settings', render: () => h(SettingsPage, null), title: 'Orchestrator' })

    ctx.registerMany([
      {
        area: 'palette',
        data: {
          id: `${NS}.stop`,
          keywords: ['stop', 'background', 'agents', 'cancel', 'subagents'],
          label: 'Stop delegated agents of this chat',
          run: () => {
            const key = host.state.focusedStoredSessionId.get()
            if (key) act(() => post(`/sessions/${encodeURIComponent(key)}/stop-background`), r => `Stopped ${r?.stopped ?? 0} task(s)`)
          }
        },
        id: 'stop'
      },
      {
        area: 'palette',
        data: {
          id: `${NS}.fork`,
          keywords: ['fork', 'branch'],
          label: 'Fork this chat (latest turn)',
          run: () => {
            const key = host.state.focusedStoredSessionId.get()
            const profile = host.state.focusedSessionProfile?.get?.()
            if (key) act(() => post('/fork', { profile, session_key: key }), r => `Forked (${r?.strategy})`).then(r => r && openThread(r.forkSessionKey, profile))
          }
        },
        id: 'fork'
      }
    ])
  }
}
