// T3 Code Desktop isolation tests: the real shell + the real feature modules, with failures
// injected into one feature at a time, against a host that behaves like Hermes's plugin context.
// Run: node plugins/t3code/tests/desktop/isolation.mjs   (needs hermes-agent/node_modules)
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(here, '../..')
const MODULES = path.resolve(ROOT, '../../hermes-agent/node_modules')
const require = createRequire(path.join(MODULES, 'x.js'))
const esbuild = require('esbuild')
const { JSDOM } = require('jsdom')

// ── a DOM, storage and bridge like the renderer's ───────────────────────────
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://hermes.test/', pretendToBeVisual: true })
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  MutationObserver: dom.window.MutationObserver,
  IS_REACT_ACT_ENVIRONMENT: true
})
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: dom.window.localStorage })
window.hermesDesktop = { api: async req => Promise.reject(new Error(`unexpected bridge call ${req.path}`)) }
// Pre-merge storage of the old t3-inbox plugin, to be carried over once.
localStorage.setItem('hermes.plugin.t3-inbox.prefs', JSON.stringify({ workingShelf: false, revealed: true }))

// ── bundle the shell; each feature import goes through a failure-injection wrapper ──
const FEATURE_RE = /features[\\/](inbox|usage|orchestrator|claude_cli)[\\/]desktop\.js$/
const inject = {
  name: 'inject',
  setup(build) {
    build.onResolve({ filter: FEATURE_RE }, args => {
      if (args.namespace === 'inject') {
        return undefined
      }

      return { namespace: 'inject', path: path.resolve(args.resolveDir, args.path) }
    })
    build.onLoad({ filter: /.*/, namespace: 'inject' }, args => {
      const key = FEATURE_RE.exec(args.path)[1]

      return {
        resolveDir: path.dirname(args.path),
        contents: `
import real from ${JSON.stringify(args.path)}
const key = ${JSON.stringify(key)}
export default {
  ...real,
  register(ctx) {
    const mode = globalThis.__t3fail?.[key]
    if (mode === 'register') {
      ctx.register({ id: 'half-done', area: 'palette', data: { id: key + '.half', run() {} } })
      throw new Error(key + ' register boom')
    }
    if (mode === 'async') {
      ctx.register({ id: 'half-done', area: 'palette', data: { id: key + '.half', run() {} } })
      return Promise.reject(new Error(key + ' async boom'))
    }
    const out = real.register(ctx)
    if (mode === 'render') ctx.register({ id: 'bomb', area: 'panes', render: () => { throw new Error(key + ' render boom') } })
    if (mode === 'badge') ctx.register({ id: 'bomb', area: 'sessionRow.trailing', data: { render: () => { throw new Error('badge boom') } } })
    if (mode === 'timer') ctx.setInterval(() => { throw new Error(key + ' timer boom') }, 1000)
    if (mode === 'event') ctx.onEvent('message.complete', () => { throw new Error(key + ' event boom') })
    if (mode === 'command') ctx.register({ id: 'bomb', area: 'palette', data: { id: key + '.bomb', run: () => { throw new Error('command boom') } } })
    return out
  }
}`
      }
    })
  }
}

const out = path.join(MODULES, `.t3code-isolation-${process.pid}.mjs`)
await esbuild.build({
  entryPoints: [path.join(ROOT, 'shared/desktop/shell.js')],
  alias: { '@hermes/plugin-sdk': path.join(here, 'mock-sdk.mjs') },
  bundle: true,
  external: ['react', 'react-dom', 'nanostores'],
  format: 'esm',
  logLevel: 'error',
  nodePaths: [MODULES],
  outfile: out,
  platform: 'node',
  plugins: [inject]
})
const shell = (await import(pathToFileURL(out).href)).default
fs.rmSync(out)
const React = require('react')
const { act } = React
const { createRoot } = require('react-dom/client')

// ── a host context with Hermes's semantics (registry, tracked disposers, scoped storage) ──
function makeHost(toggles) {
  const contribs = new Map()
  const hostDisposers = []
  const intervals = new Set()
  const listeners = new Map()
  const storage = new Map()
  const backend = { toggles: { inbox: true, usage: true, orchestrator: true, claude_cli: true, ...toggles } }
  const track = d => (hostDisposers.push(d), d)
  const features = () => Object.entries(backend.toggles).map(([key, enabled]) => ({ key, label: key, enabled, status: {} }))

  const ctx = {
    source: 'plugin:t3code',
    register: c => {
      const id = `t3code:${c.id}`
      contribs.set(id, { ...c, id })

      return track(() => contribs.delete(id))
    },
    registerMany: cs => {
      const ds = cs.map(c => ctx.register(c))

      return track(() => ds.forEach(d => d()))
    },
    registerSettingsPage: page => {
      const id = `t3code:settings:${page.id}`
      contribs.set(id, { ...page, area: 'settings.plugins', id })

      return track(() => contribs.delete(id))
    },
    onDispose: fn => void track(fn),
    onEvent: (type, l) => {
      const set = listeners.get(type) || new Set()
      set.add(l)
      listeners.set(type, set)

      return track(() => set.delete(l))
    },
    setTimeout: () => track(() => {}),
    setInterval: fn => {
      intervals.add(fn)

      return track(() => intervals.delete(fn))
    },
    addEventListener: () => track(() => {}),
    rest: async (p, opts = {}) => {
      if (p === '/features') {
        if (opts.method === 'POST') Object.assign(backend.toggles, opts.body)

        return { features: features() }
      }

      if (p.startsWith('/inbox/lifecycle')) return { records: {} }
      if (p.startsWith('/inbox/outcomes')) return {}
      if (p.startsWith('/inbox/interrupted')) return {}
      if (p.startsWith('/orchestrator/') || p.startsWith('/usage/')) return {}
      throw new Error(`404 ${p}`)
    },
    socket: () => track(() => {}),
    os: {},
    pet: { say: () => () => {}, clear: () => {} },
    i18n: {},
    storage: {
      get: (k, d) => (storage.has(`hermes.plugin.t3code.${k}`) ? storage.get(`hermes.plugin.t3code.${k}`) : d),
      set: (k, v) => storage.set(`hermes.plugin.t3code.${k}`, v),
      remove: k => storage.delete(`hermes.plugin.t3code.${k}`)
    }
  }

  return {
    backend,
    contribs,
    ctx,
    ids: () => [...contribs.keys()].sort(),
    has: prefix => [...contribs.keys()].some(id => id.startsWith(`t3code:${prefix}-`)),
    fireIntervals: () => [...intervals].forEach(fn => fn()),
    emit: (type, ev) => [...(listeners.get(type) || [])].forEach(l => l(ev)),
    settings: () => contribs.get('t3code:settings:settings'),
    storage,
    unload: () => hostDisposers.splice(0).reverse().forEach(d => d())
  }
}

const flush = () => act(async () => {
  for (let i = 0; i < 5; i += 1) await new Promise(r => setTimeout(r, 0))
})

async function render(node) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const root = createRoot(el)
  await act(async () => root.render(node))

  return { el, root, text: () => el.textContent }
}

function boot(fail = {}, toggles = {}) {
  globalThis.__t3fail = fail
  const host = makeHost(toggles)
  shell.register(host.ctx)

  return host
}

const errors = []
const realError = console.error
console.error = (...args) => errors.push(args.map(String).join(' '))

let passed = 0
const results = []

async function test(name, fn) {
  try {
    await fn()
    passed += 1
    results.push(`ok   ${name}`)
  } catch (err) {
    results.push(`FAIL ${name}\n     ${err?.stack || err}`)
  }
}

// ── tests ───────────────────────────────────────────────────────────────────
await test('all features start, each in its own id namespace, with one T3 Code settings entry', async () => {
  const host = boot()
  await flush()
  assert.ok(host.has('inbox'), 'inbox contributions')
  assert.ok(host.has('usage'), 'usage contributions')
  assert.ok(host.has('orchestrator'), 'orchestrator contributions')
  assert.ok(host.has('claude_cli'), 'Claude context contribution')
  assert.ok(host.contribs.has('t3code:inbox-pane'), 'inbox pane keeps the id PANE_ID expects')
  const page = host.settings()
  assert.equal(page.title, 'T3 Code')
  assert.deepEqual(page.children.map(c => c.id), ['inbox', 'orchestrator'])
  host.unload()
  assert.deepEqual(host.ids(), [], 'unload removes everything')
})

await test('old plugin storage is carried over once into the feature namespace', async () => {
  const host = boot()
  await flush()
  assert.equal(host.storage.get('hermes.plugin.t3code.inbox.prefs').workingShelf, false)
  assert.equal(host.storage.get('hermes.plugin.t3code.inbox.__migrated'), true)
  host.unload()
})

await test('switching one feature off stops only that feature; on starts it again (live)', async () => {
  const host = boot()
  await flush()
  const inboxIds = host.ids().filter(id => id.startsWith('t3code:inbox-'))
  host.backend.toggles.usage = false
  host.fireIntervals() // includes the toggle poll
  await flush()
  assert.ok(!host.has('usage'), 'usage gone')
  assert.deepEqual(host.ids().filter(id => id.startsWith('t3code:inbox-')), inboxIds, 'inbox untouched')
  assert.ok(host.has('orchestrator'))
  host.backend.toggles.usage = true
  host.fireIntervals()
  await flush()
  assert.ok(host.has('usage'), 'usage back')
  host.unload()
})

await test('features switched off in config never start, and start once switched on', async () => {
  const host = boot({}, { orchestrator: false })
  await flush()
  assert.ok(!host.has('orchestrator'))
  assert.ok(host.has('inbox') && host.has('usage'))
  assert.deepEqual(host.settings().children.map(c => c.id), ['inbox'])
  host.backend.toggles.orchestrator = true
  host.fireIntervals()
  await flush()
  assert.ok(host.has('orchestrator'))
  host.unload()
})

for (const key of ['inbox', 'usage', 'orchestrator', 'claude_cli']) {
  const others = ['inbox', 'usage', 'orchestrator', 'claude_cli'].filter(k => k !== key)

  await test(`${key} throwing in register(): rolled back and marked failed; ${others.join(' + ')} keep running`, async () => {
    const host = boot({ [key]: 'register' })
    await flush()
    assert.ok(!host.has(key), `${key} partial registrations rolled back: ${host.ids().join(', ')}`)
    others.forEach(k => assert.ok(host.has(k), `${k} still running`))
    const view = await render(React.createElement(host.settings().render))
    assert.match(view.text(), new RegExp(`Failed to start: ${key} register boom`))
    host.unload()
  })
}

await test('async register() rejection is contained the same way', async () => {
  const host = boot({ usage: 'async' })
  await flush()
  assert.ok(!host.has('usage'))
  assert.ok(host.has('inbox') && host.has('orchestrator'))
  host.unload()
})

await test('a crashing panel shows an error with Retry in that panel only; other panels render', async () => {
  const host = boot({ inbox: 'render' })
  await flush()
  const bomb = await render(React.createElement(host.contribs.get('t3code:inbox-bomb').render))
  assert.match(bomb.text(), /T3 Code · Inbox hit an error/)
  assert.match(bomb.text(), /inbox render boom/)
  assert.match(bomb.text(), /Retry/)
  const strip = await render(React.createElement(host.contribs.get('t3code:orchestrator-context-strip').render))
  assert.doesNotMatch(strip.text(), /hit an error/)
  const usagePage = await render(React.createElement(host.contribs.get('t3code:usage-page').render))
  assert.doesNotMatch(usagePage.text(), /hit an error/)
  host.unload()
})

await test('a crashing small slot (row badge) disappears quietly instead of breaking the row', async () => {
  const host = boot({ orchestrator: 'badge' })
  await flush()
  const badge = await render(React.createElement(host.contribs.get('t3code:orchestrator-bomb').data.render, { sessionId: 's' }))
  assert.equal(badge.text(), '')
  host.unload()
})

await test('throwing timers, gateway events and palette commands are recorded, not propagated', async () => {
  const host = boot({ usage: 'timer', orchestrator: 'event', inbox: 'command' })
  await flush()
  assert.doesNotThrow(() => host.fireIntervals())
  assert.doesNotThrow(() => host.emit('message.complete', { type: 'message.complete' }))
  assert.doesNotThrow(() => host.contribs.get('t3code:inbox-bomb').data.run())
  await flush()
  const view = await render(React.createElement(host.settings().render))
  assert.match(view.text(), /last error: interval: usage timer boom/)
  assert.match(view.text(), /last error: event message\.complete: orchestrator event boom/)
  assert.match(view.text(), /last error: data\.run: command boom/)
  assert.ok(host.has('inbox') && host.has('usage') && host.has('orchestrator'), 'all still running')
  host.unload()
})

await test('Restart on the T3 Code page brings a failed feature back once it is fixed', async () => {
  const host = boot({ usage: 'register' })
  await flush()
  assert.ok(!host.has('usage'))
  globalThis.__t3fail = {}
  const view = await render(React.createElement(host.settings().render))
  const restart = [...view.el.querySelectorAll('button')].find(b => b.textContent === 'Restart')
  assert.ok(restart, 'Restart button shown for the failed feature')
  await act(async () => restart.click())
  await flush()
  assert.ok(host.has('usage'), 'usage running after restart')
  host.unload()
})

await test('the overview switches write the toggle and apply it at once', async () => {
  const host = boot()
  await flush()
  const view = await render(React.createElement(host.settings().render))
  const box = view.el.querySelector('[data-toggle="Inbox"] input')
  await act(async () => box.click())
  await flush()
  assert.equal(host.backend.toggles.inbox, false, 'saved through POST /features')
  assert.ok(!host.has('inbox'))
  assert.ok(host.has('usage') && host.has('orchestrator'))
  host.unload()
})

await test('an unreachable backend keeps the last known switches', async () => {
  const host = boot({}, { usage: false })
  await flush()
  host.unload()
  // Next launch: the backend is down; the cached toggles (usage off) still apply.
  const again = makeHost()
  again.storage.clear()
  for (const [k, v] of host.storage) again.storage.set(k, v)
  again.ctx.rest = async () => {
    throw new Error('backend down')
  }
  globalThis.__t3fail = {}
  shell.register(again.ctx)
  await flush()
  assert.ok(!again.has('usage'))
  assert.ok(again.has('inbox') && again.has('orchestrator'))
  const view = await render(React.createElement(again.settings().render))
  assert.match(view.text(), /Backend not reachable \(backend down\)/)
  again.unload()
})

console.error = realError
console.log(results.join('\n'))
console.log(`\n${passed}/${results.length} passed`)
process.exit(passed === results.length ? 0 : 1)
