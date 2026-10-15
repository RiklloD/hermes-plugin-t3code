// T3 Code · Orchestrator — web dashboard tab: delegated tasks, schedules, recovery and settings.
// Source of dashboard/dist/index.js (copied there by scripts/build.mjs; edit this file).
(function () {
  'use strict'

  const SDK = window.__HERMES_PLUGIN_SDK__
  const { React } = SDK
  const { useState, useEffect, useCallback } = SDK.hooks
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input } = SDK.components
  const h = React.createElement
  const API = '/api/plugins/t3code/orchestrator'
  const call = (path, method, body) =>
    SDK.fetchJSON(API + path, method ? { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined } : undefined)
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString() : '—')
  const dur = s => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`)
  const TONE = { completed: 'default', running: 'secondary', starting: 'secondary', waiting: 'outline', failed: 'destructive', cancelled: 'outline', interrupted: 'outline' }

  function usePoll(path, ms) {
    const [data, setData] = useState(null)
    const [error, setError] = useState(null)
    const load = useCallback(() => call(path).then(setData, setError), [path])
    useEffect(() => {
      load()
      const t = setInterval(load, ms)
      return () => clearInterval(t)
    }, [load, ms])
    return { data, error, reload: load }
  }

  function Tasks() {
    const { data, reload } = usePoll('/tasks?scope=recent&limit=100', 5000)
    const [open, setOpen] = useState(null)
    const tasks = (data && data.tasks) || []
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, `Delegated agents (${tasks.filter(t => !['completed', 'failed', 'cancelled', 'interrupted'].includes(t.status)).length} active)`)),
      h(CardContent, { className: 'space-y-2' },
        tasks.length === 0 ? h('p', { className: 'text-sm text-muted-foreground' }, 'No delegated tasks yet.') : null,
        tasks.map(t => h('div', { key: t.taskId, className: 'border rounded p-2 text-sm' },
          h('div', { className: 'flex items-center gap-2' },
            h(Badge, { variant: TONE[t.status] || 'outline' }, t.status),
            h('span', { className: 'font-medium flex-1 truncate', role: 'button', onClick: () => setOpen(open === t.taskId ? null : t.taskId) }, t.title),
            h('span', { className: 'text-muted-foreground' }, `${t.provider || '?'} · ${t.model || '?'} · ${dur(t.durationSeconds || 0)}`),
            ['completed', 'failed', 'cancelled', 'interrupted'].includes(t.status) ? null
              : h(Button, { size: 'sm', variant: 'outline', onClick: () => call(`/tasks/${t.taskId}/cancel`, 'POST').then(reload) }, 'Stop')),
          t.progress && t.progress.lastTool && !t.summary ? h('div', { className: 'text-xs text-muted-foreground mt-1 truncate' }, `${t.progress.lastTool} ${t.progress.lastToolHint || ''}`) : null,
          open === t.taskId && (t.summary || t.error) ? h('pre', { className: 'text-xs whitespace-pre-wrap mt-2 max-h-72 overflow-auto' }, t.error && t.status !== 'completed' ? `${t.error}\n\n${t.summary || ''}` : t.summary) : null))))
  }

  function Schedules() {
    const { data, reload } = usePoll('/schedules', 10000)
    const [prompt, setPrompt] = useState('')
    const [every, setEvery] = useState('60')
    const [daily, setDaily] = useState('')
    const [err, setErr] = useState('')
    const list = (data && data.schedules) || []
    const create = () => {
      const schedule = daily ? { type: 'fixed_time', timeOfDay: daily } : { type: 'interval', everyMs: Math.max(1, +every) * 60000 }
      call('/schedules', 'POST', { prompt, schedule, bind_to_current_thread: false }).then(() => { setPrompt(''); setErr(''); reload() }, e => setErr(String(e.message || e)))
    }
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, `Schedules (${list.length})`)),
      h(CardContent, { className: 'space-y-2' },
        list.map(s => h('div', { key: s.id, className: 'border rounded p-2 text-sm flex items-center gap-2' },
          h(Badge, { variant: s.enabled ? 'default' : 'outline' }, s.enabled ? 'on' : 'paused'),
          h('div', { className: 'flex-1 min-w-0' },
            h('div', { className: 'font-medium truncate' }, s.title),
            h('div', { className: 'text-xs text-muted-foreground truncate' }, `${s.describe} · next ${when(s.nextRunAt)} · ${s.runCount} runs${s.webhookUrl ? ' · ' + s.webhookUrl : ''}`)),
          h(Button, { size: 'sm', variant: 'outline', onClick: () => call(`/schedules/${s.id}`, 'PATCH', { enabled: !s.enabled }).then(reload) }, s.enabled ? 'Pause' : 'Resume'),
          h(Button, { size: 'sm', variant: 'outline', onClick: () => call(`/schedules/${s.id}/run`, 'POST').then(reload) }, 'Run now'),
          h(Button, { size: 'sm', variant: 'outline', onClick: () => call(`/schedules/${s.id}`, 'DELETE').then(reload) }, 'Delete'))),
        h('div', { className: 'flex flex-wrap items-center gap-2 pt-2' },
          h(Input, { placeholder: 'Prompt for a new thread each run…', value: prompt, onChange: e => setPrompt(e.target.value), style: { flex: 1, minWidth: 240 } }),
          h('span', { className: 'text-sm' }, 'every'),
          h(Input, { type: 'number', value: every, onChange: e => { setEvery(e.target.value); setDaily('') }, style: { width: 80 } }),
          h('span', { className: 'text-sm' }, 'min, or daily at'),
          h(Input, { type: 'time', value: daily, onChange: e => setDaily(e.target.value), style: { width: 120 } }),
          h(Button, { disabled: !prompt.trim(), onClick: create }, 'Create')),
        err ? h('p', { className: 'text-sm text-destructive' }, err) : null))
  }

  function Recovery() {
    const { data: settings, reload } = usePoll('/settings', 15000)
    const { data: rec } = usePoll('/recoveries', 10000)
    const set = patch => call('/settings', 'POST', patch).then(reload)
    const armed = ((rec && rec.recoveries) || []).filter(r => r.status === 'armed' && r.kind !== 'restart')
    const toggle = (k, label) => h('label', { className: 'flex items-center gap-2 text-sm' },
      h('input', { type: 'checkbox', checked: !!(settings && settings[k]), onChange: e => set({ [k]: e.target.checked }) }), label)
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, 'Recovery & automation')),
      h(CardContent, { className: 'space-y-2' },
        toggle('continue_after_restart', 'Continue threads after restarts (update, quit or crash)'),
        toggle('resume_on_usage_limit', 'Resume threads automatically when a provider usage limit resets'),
        armed.length ? armed.map(r => h('div', { key: r.id, className: 'text-sm' }, `${r.session_key}: resumes after ${when(r.reset_at)}`))
          : h('p', { className: 'text-sm text-muted-foreground' }, 'No usage-limit resumes armed.')))
  }

  function Page() {
    return h('div', { className: 'space-y-4' },
      h('p', { className: 'text-sm text-muted-foreground' },
        'T3 orchestration V2 for Hermes. Delegation, queues and forks run inside the Desktop backend; the Agents pane in Hermes Desktop shows them per chat. On a phone, use /t3 in any connected messaging app.'),
      h(Tasks), h(Schedules), h(Recovery))
  }

  window.__HERMES_PLUGINS__.register('t3code', Page)
})()
