/**
 * T3 Code · feature isolation for the Desktop half.
 *
 * Hermes loads ONE plugin (t3code). Each feature registers through its own scoped context, so:
 *   - everything it registers (contributions, timers, listeners, events, settings pages) is
 *     tracked per feature and can be torn down or restarted without touching the others;
 *   - every render it contributes sits inside an error boundary (a crash shows "Retry" in that
 *     panel only — or nothing, for small slots like row badges) and every callback it hands the
 *     host (timers, listeners, gateway events, palette commands) is wrapped, so a throw is
 *     recorded against that feature instead of escaping;
 *   - its ids, storage keys and REST paths are namespaced (`<feature>-<id>`, `<feature>.<key>`,
 *     `/api/plugins/t3code/<feature>/…`).
 *
 * What this cannot isolate: a feature that blocks the renderer thread (an infinite loop) or
 * corrupts shared globals. That needs process isolation, which Desktop plugins do not have.
 */
import React from 'react'

const h = React.createElement

// Slots where a crashed feature should show itself; everywhere else it just disappears.
const LOUD_AREAS = new Set(['panes', 'routes', 'settings.plugins'])

function errText(error) {
  return error instanceof Error ? error.message : String(error)
}

/** Calls `fn` while rendering, so whatever it throws lands in the boundary above it. */
export function Invoke({ fn }) {
  const out = fn()

  return out === undefined ? null : out
}

export class FeatureBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error) {
    this.props.onError?.(error)
  }

  render() {
    const { error } = this.state

    if (!error) {
      return this.props.children
    }

    if (!this.props.loud) {
      return null
    }

    return h(
      'div',
      { style: { display: 'flex', flexDirection: 'column', gap: 8, padding: 16, fontSize: 12 } },
      h('div', { style: { color: 'var(--ui-red, #e5484d)', fontWeight: 600 } }, `T3 Code · ${this.props.label} hit an error`),
      h('div', { style: { color: 'var(--ui-text-secondary)', whiteSpace: 'pre-wrap' } }, errText(error)),
      h('div', { style: { color: 'var(--ui-text-tertiary)' } }, 'The other T3 Code features are unaffected.'),
      h(
        'button',
        {
          onClick: () => this.setState({ error: null }),
          style: {
            alignSelf: 'flex-start',
            background: 'transparent',
            border: '1px solid var(--ui-border, currentColor)',
            borderRadius: 6,
            color: 'inherit',
            cursor: 'pointer',
            fontSize: 12,
            padding: '4px 10px'
          },
          type: 'button'
        },
        'Retry'
      )
    )
  }
}

/** A render function whose output (and anything it renders) is caught per feature. */
export function guardRender(feature, fn, report, loud) {
  return (...args) =>
    h(FeatureBoundary, { label: feature.label, loud, onError: e => report(e, 'render') }, h(Invoke, { fn: () => fn(...args) }))
}

/** A callback the host will call later: a throw (or rejection) is reported, never propagated. */
export function guardFn(fn, report, where) {
  if (typeof fn !== 'function') {
    return fn
  }

  return function guarded(...args) {
    try {
      const out = fn.apply(this, args)

      if (out && typeof out.then === 'function') {
        out.then(undefined, e => report(e, where))
      }

      return out
    } catch (e) {
      report(e, where)

      return undefined
    }
  }
}

function guardData(data, report) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return data
  }

  const out = { ...data }

  for (const [k, v] of Object.entries(data)) {
    if (typeof v === 'function') {
      // data.render (row badges, etc.) renders React; everything else is a plain callback.
      out[k] = k === 'render' ? null : guardFn(v, report, `data.${k}`)
    }
  }

  return out
}

/** Copy a pre-merge plugin's storage (`hermes.plugin.<legacy>.<key>`) into this feature once. */
export function migrateLegacyStorage(ctx, feature) {
  const marker = `${feature.key}.__migrated`

  if (!feature.legacy || ctx.storage.get(marker, false)) {
    return
  }

  try {
    const prefix = `hermes.plugin.${feature.legacy}.`
    const ls = window.localStorage

    for (let i = 0; i < ls.length; i += 1) {
      const full = ls.key(i)

      if (!full || !full.startsWith(prefix)) {
        continue
      }

      const key = `${feature.key}.${full.slice(prefix.length)}`
      const missing = {}

      if (ctx.storage.get(key, missing) === missing) {
        try {
          ctx.storage.set(key, JSON.parse(ls.getItem(full)))
        } catch {
          // Unparseable legacy value: skip it, the feature falls back to its default.
        }
      }
    }
  } catch {
    // No localStorage (tests, restricted contexts): nothing to migrate.
  }

  ctx.storage.set(marker, true)
}

/**
 * The context one feature's register() receives. `entry.disposers` collects every teardown so
 * the shell can stop this feature alone; `report(error, where)` records failures against it;
 * `settings` collects its settings page under the single T3 Code entry.
 */
export function featureContext(ctx, feature, entry, { report, settings }) {
  const track = dispose => {
    let done = false

    const once = () => {
      if (done) {
        return
      }

      done = true
      entry.disposers.delete(once)
      dispose()
    }

    entry.disposers.add(once)

    return once
  }

  const scope = c => {
    const loud = LOUD_AREAS.has(c.area)
    const data = guardData(c.data, report)

    if (c.data && typeof c.data.render === 'function') {
      data.render = guardRender(feature, c.data.render, report, loud)
    }

    return {
      ...c,
      id: `${feature.key}-${c.id}`,
      ...(typeof c.render === 'function' ? { render: guardRender(feature, c.render, report, loud) } : {}),
      ...(c.data ? { data } : {})
    }
  }

  const storageKey = key => `${feature.key}.${key}`

  return {
    source: ctx.source,
    feature: feature.key,
    register: c => track(ctx.register(scope(c))),
    registerMany: cs => track(ctx.registerMany(cs.map(scope))),
    registerSettingsPage: page => track(settings.add(feature, page)),
    onDispose: fn => void track(guardFn(fn, report, 'dispose')),
    onEvent: (type, listener) => track(ctx.onEvent(type, guardFn(listener, report, `event ${type}`))),
    setTimeout: (fn, ms) => {
      let cancel = null

      // A fired timeout drops out of the feature's list (debounces would otherwise pile up).
      const fire = guardFn(() => {
        cancel && entry.disposers.delete(cancel)
        fn()
      }, report, 'timeout')

      cancel = track(ctx.setTimeout(fire, ms))

      return cancel
    },
    setInterval: (fn, ms) => track(ctx.setInterval(guardFn(fn, report, 'interval'), ms)),
    addEventListener: (target, type, listener, options) =>
      track(
        ctx.addEventListener(
          target,
          type,
          typeof listener === 'function' ? guardFn(listener, report, `listener ${type}`) : listener,
          options
        )
      ),
    rest: (path, opts) => ctx.rest(`/${feature.key}${path}`, opts),
    socket: (path, onMessage) => track(ctx.socket(`/${feature.key}${path}`, guardFn(onMessage, report, 'socket'))),
    os: ctx.os,
    pet: ctx.pet,
    runAction: (...args) => ctx.runAction?.(...args),
    listActions: (...args) => ctx.listActions?.(...args) ?? [],
    i18n: ctx.i18n,
    storage: {
      get: (key, fallback) => ctx.storage.get(storageKey(key), fallback),
      set: (key, value) => ctx.storage.set(storageKey(key), value),
      remove: key => ctx.storage.remove(storageKey(key))
    }
  }
}
