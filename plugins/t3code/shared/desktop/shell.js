/**
 * T3 Code · Desktop shell — the one Desktop plugin Hermes loads for T3 Code.
 *
 * Starts each feature's Desktop half (features/<key>/desktop.js) in its own scoped context
 * (./isolation.js) and keeps the set in step with the plugin's feature toggles:
 *   - toggles come from the T3 Code backend (GET /api/plugins/t3code/features — the same
 *     `plugins.entries.t3code.settings` the Hermes plugin manager edits), cached locally so the
 *     features start immediately on launch and keep running if the backend is unreachable;
 *   - switching a feature off stops only that feature; switching it on starts it, live;
 *   - a feature that throws while registering is rolled back and marked failed (Retry on the
 *     T3 Code settings page); the others keep running.
 *
 * Settings ▸ Plugins ▸ T3 Code: an overview (switches, status, errors) with each feature's own
 * settings as sub-pages. Bundled into desktop/plugin.js by scripts/build.mjs.
 */
import * as sdk from '@hermes/plugin-sdk'
import React from 'react'

import inbox from '../../features/inbox/desktop.js'
import orchestrator from '../../features/orchestrator/desktop.js'
import usage from '../../features/usage/desktop.js'
import claudeCli from '../../features/claude_cli/desktop.js'
import goals from '../../features/goals/desktop.js'
import battery from '../../features/battery/desktop.js'

import { FeatureBoundary, featureContext, Invoke, migrateLegacyStorage } from './isolation.js'

const { host, atom, useValue, ToggleRow, ListRow, Button } = sdk
const h = React.createElement

const PLUGIN_ID = 't3code'
const TOGGLE_POLL_MS = 30_000

// Desktop halves, in settings order. `legacy` = the plugin id this feature shipped as before the
// merge (its local storage is carried over once).
const DESKTOP_FEATURES = [
  { key: 'inbox', label: 'Inbox', legacy: 't3-inbox', module: inbox },
  { key: 'usage', label: 'Usage', legacy: 't3-usage', module: usage },
  { key: 'goals', label: 'Goals', module: goals },
  { key: 'orchestrator', label: 'Orchestrator', legacy: 't3-orchestrator', module: orchestrator },
  { key: 'claude_cli', label: 'Claude CLI', module: claudeCli },
  { key: 'battery', label: 'Battery saver', module: battery }
]
// Shown on the overview only (no Desktop half), so every toggle lives in one place.
const AGENT_ONLY = [
  { key: 'muse_code', label: 'Muse Code' },
  { key: 'antigravity', label: 'Antigravity' }
]
const DESCRIPTIONS = {
  goals: 'A persistent T3-style goal strip above the composer, independent of the selected model or native harness.',
  inbox: 'T3’s thread sidebar: pinned, inbox, working, snoozed and settled, with crash and usage-limit recovery.',
  usage: 'Claude Code, Codex and Z.ai tokens, cost and live limits in Command Center → Usage.',
  orchestrator: 'Delegated agents, forks and merge-back, durable queues, recovery and schedules. The agent tools change after a backend restart.',
  claude_cli: 'Claude Code’s native agent as the “Claude CLI” model provider. Applies after a backend restart.',
  muse_code: 'Meta’s Muse Code agent as the “Muse Code” model provider (muse login). Applies after a backend restart.',
  antigravity: 'Google’s Antigravity agent as the “Antigravity” model provider (own Google sign-in). Applies after a backend restart.',
  battery: 'Holds spinners and shimmers to 1 frame a second while Hermes is unfocused or untouched for 30 s; they resume on any input. Applies live.'
}

let ctxRef = null
let $toggles = null // { [key]: boolean }
let $remote = null // { features: [...] | null, error, at }
let $local = null // { [key]: { state: 'off' | 'running' | 'failed', error, lastError, errors } }
let live = new Map() // key -> { disposers: Set<() => void> }
let pages = new Map() // key -> settings page a feature registered
let disposeSettings = null
let settingsQueued = false

function errText(error) {
  return error instanceof Error ? error.message : String(error)
}

function patchLocal(key, patch) {
  const all = $local.get()

  $local.set({ ...all, [key]: { state: 'off', error: null, lastError: null, errors: 0, ...all[key], ...patch } })
}

function reporter(feature) {
  return (error, where) => {
    console.error(`[t3code:${feature.key}] ${where} failed`, error)
    const prev = $local.get()[feature.key] || {}

    patchLocal(feature.key, { lastError: `${where}: ${errText(error)}`, errors: (prev.errors || 0) + 1 })
  }
}

// ─── feature lifecycle ──────────────────────────────────────────────────────

function stopFeature(key) {
  const entry = live.get(key)

  if (!entry) {
    return
  }

  live.delete(key)

  for (const dispose of [...entry.disposers].reverse()) {
    try {
      dispose()
    } catch (error) {
      console.error(`[t3code:${key}] cleanup failed`, error)
    }
  }
}

function failFeature(feature, error) {
  console.error(`[t3code:${feature.key}] failed to start`, error)
  stopFeature(feature.key)
  patchLocal(feature.key, { state: 'failed', error: errText(error) })

  try {
    host.notify({ kind: 'error', message: `${feature.label} failed to start: ${errText(error)}. Other T3 Code features keep running.`, title: 'T3 Code' })
  } catch {}
}

function startFeature(feature) {
  if (live.has(feature.key)) {
    return
  }

  const entry = { disposers: new Set() }

  live.set(feature.key, entry)
  migrateLegacyStorage(ctxRef, feature)

  try {
    const result = feature.module.register(featureContext(ctxRef, feature, entry, { report: reporter(feature), settings }))

    if (result && typeof result.then === 'function') {
      result.then(undefined, error => live.get(feature.key) === entry && failFeature(feature, error))
    }
  } catch (error) {
    failFeature(feature, error)

    return
  }

  patchLocal(feature.key, { state: 'running', error: null })
}

function restartFeature(feature) {
  stopFeature(feature.key)
  patchLocal(feature.key, { state: 'off', error: null, lastError: null, errors: 0 })

  if ($toggles.get()[feature.key] !== false) {
    startFeature(feature)
  }
}

/** Bring the running set in line with the toggles. A failed feature stays down until Retry. */
function applyToggles() {
  const toggles = $toggles.get()

  for (const feature of DESKTOP_FEATURES) {
    const on = toggles[feature.key] !== false
    const state = $local.get()[feature.key]?.state

    if (on && !live.has(feature.key) && state !== 'failed') {
      startFeature(feature)
    } else if (!on && (live.has(feature.key) || state === 'failed')) {
      stopFeature(feature.key)
      patchLocal(feature.key, { state: 'off', error: null })
    }
  }
}

function adoptToggles(list) {
  const next = { ...$toggles.get() }

  for (const f of list || []) {
    next[f.key] = f.enabled !== false
  }

  $toggles.set(next)
  ctxRef.storage.set('toggles', next)
  applyToggles()
}

async function syncToggles() {
  try {
    const res = await ctxRef.rest('/features', { timeoutMs: 15_000 })

    $remote.set({ at: Date.now(), error: null, features: res?.features || [] })
    adoptToggles(res?.features)
  } catch (error) {
    // Backend not up / plugin API not mounted yet: keep the cached toggles running.
    $remote.set({ ...$remote.get(), error: errText(error) })
  }
}

async function setFeature(key, enabled) {
  $toggles.set({ ...$toggles.get(), [key]: enabled })
  applyToggles()

  try {
    const res = await ctxRef.rest('/features', { body: { [key]: enabled }, method: 'POST', timeoutMs: 15_000 })

    $remote.set({ at: Date.now(), error: null, features: res?.features || [] })
    adoptToggles(res?.features)
  } catch (error) {
    host.notify({ kind: 'error', message: `Could not save: ${errText(error)}`, title: 'T3 Code' })
    void syncToggles()
  }
}

// ─── settings: one T3 Code entry, features as sub-pages ─────────────────────

const settings = {
  add(feature, page) {
    pages.set(feature.key, { feature, page })
    queueSettings()

    return () => {
      if (pages.get(feature.key)?.page === page) {
        pages.delete(feature.key)
        queueSettings()
      }
    }
  }
}

function queueSettings() {
  if (settingsQueued) {
    return
  }

  settingsQueued = true
  queueMicrotask(() => {
    settingsQueued = false
    registerSettings()
  })
}

function registerSettings() {
  if (!ctxRef) {
    return
  }

  disposeSettings?.()
  const children = DESKTOP_FEATURES.filter(f => pages.has(f.key)).map(f => {
    const { page } = pages.get(f.key)

    return {
      id: f.key,
      title: f.label,
      render: () => h(FeatureBoundary, { label: f.label, loud: true, onError: e => reporter(f)(e, 'settings') }, h(Invoke, { fn: page.render }))
    }
  })

  disposeSettings =
    ctxRef.registerSettingsPage?.({
      children,
      icon: 'layers',
      id: 'settings',
      render: () => h(Overview, null),
      title: 'T3 Code'
    }) || null
}

function statusText(feature, on, local, remote) {
  if (!on) {
    return 'Off'
  }

  if (local?.state === 'failed') {
    return `Failed to start: ${local.error}`
  }

  const parts = remote?.status || {}
  const broken = Object.entries(parts).find(([, s]) => s.state === 'failed')

  if (broken) {
    return `${broken[0] === 'api' ? 'Backend' : 'Agent'} failed: ${broken[1].error}`
  }

  if (local?.lastError) {
    return `Running · last error: ${local.lastError}`
  }

  return local?.state === 'running' || !DESKTOP_FEATURES.some(f => f.key === feature.key) ? 'Running' : 'Starting…'
}

function Overview() {
  const toggles = useValue($toggles)
  const remote = useValue($remote)
  const local = useValue($local)
  const byKey = Object.fromEntries((remote.features || []).map(f => [f.key, f]))

  return h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
    h(
      'p',
      { style: { color: 'var(--ui-text-secondary)', fontSize: 12, margin: '0 0 8px' } },
      'Each feature runs on its own: switching one off, or one failing, leaves the others working. The same switches are in Capabilities ▸ Plugins ▸ T3 Code.'
    ),
    ...[...DESKTOP_FEATURES, ...AGENT_ONLY].map(feature => {
      const on = toggles[feature.key] !== false
      const desktop = DESKTOP_FEATURES.find(f => f.key === feature.key)
      const status = statusText(feature, on, local[feature.key], byKey[feature.key])

      return h(
        'div',
        { key: feature.key, style: { display: 'flex', flexDirection: 'column' } },
        h(ToggleRow, {
          checked: on,
          description: `${DESCRIPTIONS[feature.key]}  ·  ${status}`,
          label: feature.label,
          onChange: v => void setFeature(feature.key, v)
        }),
        desktop && on && (local[feature.key]?.state === 'failed' || local[feature.key]?.lastError)
          ? h(ListRow, {
              action: h(Button, { onClick: () => restartFeature(desktop), size: 'sm', variant: 'outline' }, 'Restart'),
              description: 'Stops and starts this feature only.',
              title: `Restart ${feature.label}`
            })
          : null
      )
    }),
    remote.error
      ? h(
          'p',
          { style: { color: 'var(--ui-text-tertiary)', fontSize: 12, margin: '8px 0 0' } },
          `Backend not reachable (${remote.error}); using the last known switches.`
        )
      : null
  )
}

// ─── registration ───────────────────────────────────────────────────────────

export default {
  id: PLUGIN_ID,
  name: 'T3 Code',
  description: 'Inbox, Usage, Orchestrator, Claude CLI, Muse Code and Antigravity from T3 Code — one plugin, each feature switchable and isolated.',
  register(ctx) {
    ctxRef = ctx
    live = new Map()
    pages = new Map()
    disposeSettings = null
    settingsQueued = false
    $toggles = atom({ inbox: true, usage: true, orchestrator: true, claude_cli: true, muse_code: true, antigravity: true, ...ctx.storage.get('toggles', {}) })
    $remote = atom({ at: 0, error: null, features: null })
    $local = atom({})

    ctx.onDispose(() => {
      for (const key of [...live.keys()]) {
        stopFeature(key)
      }
    })

    registerSettings()
    applyToggles()
    void syncToggles()
    ctx.setInterval(() => !document.hidden && void syncToggles(), TOGGLE_POLL_MS)
    ctx.addEventListener(document, 'visibilitychange', () => !document.hidden && void syncToggles())
    // Toggles are per profile: re-read them when the active profile changes.
    if (host.state?.profile?.subscribe) {
      ctx.onDispose(host.state.profile.subscribe(() => void syncToggles()))
    }
  }
}
