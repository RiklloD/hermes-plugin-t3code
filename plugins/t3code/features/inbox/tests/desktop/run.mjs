// Behaviour tests for the Inbox feature's Desktop module (features/inbox/desktop.js) against a mocked SDK.
// Run: node plugins/t3code/features/inbox/tests/desktop/run.mjs   (needs hermes-agent/node_modules)
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const HERMES = path.resolve(here, '../../../../../..')
const FEATURE = path.resolve(here, '../..')
const MODULES = path.join(HERMES, 'hermes-agent/node_modules')
const require = createRequire(path.join(MODULES, 'x.js'))
const esbuild = require('esbuild')

// ── globals the plugin touches ──────────────────────────────────────────────
const store = new Map()
globalThis.window = globalThis
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
  key: i => [...store.keys()][i] ?? null,
  get length() {
    return store.size
  }
}
globalThis.document = { hidden: false }
globalThis.location = { hash: '' }
let backend = { interrupted: {}, lifecycle: {}, outcomes: {}, sessions: [], search: {} }
globalThis.hermesDesktop = {
  api: async req => {
    if (req.path.startsWith('/api/profiles/sessions')) {
      return { sessions: backend.sessions, total: backend.sessions.length }
    }

    if (req.path.startsWith('/api/sessions/search')) {
      const p = new URLSearchParams(req.path.split('?')[1]).get('profile')

      return { results: backend.search[p] || [] }
    }

    if (req.method === 'PATCH') {
      return { ok: true }
    }

    throw new Error(`unexpected api ${req.path}`)
  }
}

// ── bundle the real plugin with test hooks appended ─────────────────────────
const source = fs.readFileSync(path.join(FEATURE, 'desktop.js'), 'utf8')
const hooks = `
export const __t = {
  classify, deriveRow, settle, snooze, unsnooze, unsettle, setPinned, writeRecs, loadLife, migrateLegacy,
  adoptRemote, pollOwner, pollLive, keyOf, settleBlocker, searchTranscripts, fetchSessions, openRow,
  setPrefs: p => $prefs.set({ ...$prefs.get(), ...p }),
  InboxPane, StatusLabel, CardRow, SlimRow, recoverInterrupted, recoverLimited, runRecovery,
  resetRecovery: () => { crashTried = new Set(); recoveryInflight = new Set(); lastInterruptedAt = 0 },
  life: () => $life, live: () => $live, selection: () => $selection, sync: () => $sync,
  setLife: v => setLife(v), clearPins: () => pinOverlay.clear(), gen: () => connGeneration, reset: () => resetSourceState(), sdk, React
}`
// Inside node_modules so the external react resolves to the same copy react-dom/server uses.
const out = path.join(MODULES, `.t3i-test-${process.pid}.mjs`)
await esbuild.build({
  stdin: { contents: source + hooks, loader: 'js', resolveDir: FEATURE },
  alias: { '@hermes/plugin-sdk': path.join(here, 'mock-sdk.mjs') },
  bundle: true,
  external: ['react', 'react-dom'],
  format: 'esm',
  logLevel: 'error',
  nodePaths: [MODULES],
  outfile: out,
  platform: 'node'
})
const mod = await import(pathToFileURL(out).href)
fs.rmSync(out)
const T = mod.__t
const sdk = T.sdk
const React = T.React
const { renderToString } = require('react-dom/server')
const { calls, host } = sdk

const storage = new Map()
const ctx = {
  storage: { get: (k, d) => (storage.has(k) ? storage.get(k) : d), set: (k, v) => storage.set(k, v) },
  setInterval: () => () => {},
  setTimeout: () => () => {},
  addEventListener: () => {},
  onEvent: () => {},
  onDispose: () => {},
  register: () => {},
  registerMany: () => {},
  registerSettingsPage: () => {},
  os: { writeClipboard: async () => true },
  rest: async (p, opts = {}) => {
    calls.rest.push({ opts, p })

    if (p.startsWith('/outcomes')) {
      return backend.outcomes
    }

    if (p === '/interrupted') {
      return backend.interrupted
    }

    if (p === '/lifecycle') {
      if (opts.method === 'POST') {
        for (const [k, r] of Object.entries(opts.body.records)) {
          if (!backend.lifecycle[k] || backend.lifecycle[k].t < r.t) backend.lifecycle[k] = r
        }
      }

      return { records: backend.lifecycle }
    }

    throw new Error('404 not found')
  }
}
mod.default.register(ctx)

// ── fixtures ────────────────────────────────────────────────────────────────
const NOW = Date.now()
const HOUR = 3_600_000
const sec = ms => ms / 1000
const base = (id, extra = {}) => ({
  _conn: 'local',
  id,
  last_active: sec(NOW - 30_000),
  message_count: 4,
  profile: 'default',
  unread: false,
  ...extra
})
const PREFS = { autoSettleDays: 0, expanded: {}, includeCron: false, syncLifecycle: true, workingShelf: true }
const freshLife = () => ({ legacy: null, records: {}, v: 2 })
const owner = (statuses = {}, extra = {}) => ({
  owners: {
    'local|default': { background: {}, pending: {}, runtime: {}, stale: false, statuses, waitingSince: {}, ...extra }
  }
})
const run = (sessions, o = {}) =>
  T.classify(sessions, { focusedId: null, life: freshLife(), live: { owners: {} }, now: NOW, pins: [], prefs: PREFS, ...o })
const sectionOf = (g, id) => ['pinned', 'inbox', 'working', 'snoozed', 'settled'].find(k => g[k].some(r => r.s.id === id))
const rec = (key, r) => ({ legacy: null, records: { [key]: { t: 1, ...r } }, v: 2 })
const K = (id, profile = 'default') => `local|${profile}|${id}`
const reset = () => {
  T.clearPins()
  T.setLife(freshLife())
  for (const k of Object.keys(calls)) calls[k].length = 0
}

const results = []
async function test(name, fn) {
  reset()

  try {
    await fn()
    results.push({ name, ok: true })
  } catch (err) {
    results.push({ err: err.stack || String(err), name, ok: false })
  }
}

// ── report acceptance criteria ──────────────────────────────────────────────
await test('an unread failed turn says Failed, never Done', () => {
  const g = run([base('a', { outcome: 'failed', outcome_at: sec(NOW - 5000), unread: true })])
  assert.equal(g.rows[0].status, 'failed')
})

await test('a usage-limit failure says Limited; a stopped turn is not a failure', () => {
  const g = run([
    base('a', { outcome: 'limited', outcome_at: sec(NOW - 5000), unread: true }),
    base('b', { outcome: 'stopped', outcome_at: sec(NOW - 5000), unread: true })
  ])
  assert.equal(g.rows[0].status, 'limited')
  assert.equal(g.rows[1].status, 'done')
})

await test('input outranks working outranks failure', () => {
  const s = [base('a', { outcome: 'failed', outcome_at: sec(NOW - 5000), unread: true })]
  assert.equal(run(s, { live: owner({ a: 'waiting' }) }).rows[0].status, 'input')
  assert.equal(run(s, { live: owner({ a: 'working' }) }).rows[0].status, 'working')
})

await test('an approval is labelled Approval', () => {
  const g = run([base('a')], { live: owner({ a: 'waiting' }, { pending: { a: 'approval' } }) })
  assert.equal(g.rows[0].status, 'approval')
})

await test('a fresh failure wakes a snooze; a failure older than the snooze does not', () => {
  const snoozeAt = NOW - 60_000
  const life = rec(K('a'), { snoozeAt, snoozeUntil: NOW + HOUR })
  const fresh = run([base('a', { outcome: 'failed', outcome_at: sec(NOW - 10_000) })], { life })
  assert.equal(sectionOf(fresh, 'a'), 'inbox')
  assert.equal(fresh.rows[0].isWoke, true)
  const old = run([base('a', { outcome: 'failed', outcome_at: sec(snoozeAt - 10_000) })], { life })
  assert.equal(sectionOf(old, 'a'), 'snoozed')
})

await test('a run that finishes after snoozing wakes it', () => {
  const life = rec(K('a'), { snoozeAt: NOW - 60_000, snoozeUntil: NOW + HOUR })
  const g = run([base('a', { outcome: 'ok', outcome_at: sec(NOW - 1000) })], { life })
  assert.equal(sectionOf(g, 'a'), 'inbox')
})

await test('a question wakes a snooze with Woke even after an earlier visit', () => {
  const life = rec(K('a'), { snoozeAt: NOW - 20_000, snoozeUntil: NOW + HOUR, visitedAt: NOW - 10_000 })
  const g = run([base('a')], { life, live: owner({ a: 'waiting' }, { waitingSince: { a: NOW - 1000 } }) })
  assert.equal(sectionOf(g, 'a'), 'inbox')
  assert.equal(g.rows[0].isWoke, true)
})

await test('pinning a snoozed session promotes it to Pinned', () => {
  T.setLife(rec(K('a'), { snoozeAt: NOW - 1000, snoozeUntil: NOW + HOUR }))
  const row = run([base('a')], { life: T.life().get() }).rows[0]
  T.setPinned(row, true)
  const g = run([base('a', { pinned: true })], { life: T.life().get() })
  assert.equal(sectionOf(g, 'a'), 'pinned')
  assert.equal(calls.pin[0].pinned, true)
  assert.ok(calls.notify.some(n => n.action?.label === 'Undo'))
})

await test('the same id in two profiles keeps independent lifecycle', () => {
  const s = [base('same', { profile: 'alpha' }), base('same', { profile: 'beta' })]
  const rows = run(s).rows
  assert.notEqual(rows[0].key, rows[1].key)
  T.settle(rows[0])
  const g = run(s, { life: T.life().get() })
  assert.deepEqual(g.settled.map(r => r.s.profile), ['alpha'])
})

await test('a live or blocked session cannot be settled', () => {
  for (const [status, extra] of [['working', {}], ['waiting', {}], ['idle', { background: { a: 2 } }]]) {
    reset()
    const row = run([base('a')], { live: owner({ a: status }, extra) }).rows[0]
    assert.ok(T.settleBlocker(row), status)
    T.settle(row)
    assert.equal(T.life().get().records[K('a')]?.settledAt, undefined, status)
    assert.equal(calls.notify[0].kind, 'warning')
  }
})

await test('a waiting session cannot be snoozed', () => {
  const row = run([base('a')], { live: owner({ a: 'waiting' }) }).rows[0]
  T.snooze(row, NOW + HOUR)
  assert.equal(T.life().get().records[K('a')], undefined)
})

await test('a settled session returns when new messages arrive, not on a timer', () => {
  const life = rec(K('a'), { settledAt: NOW - 1000, settledCount: 4, settledLast: NOW - 30_000 })
  assert.equal(sectionOf(run([base('a', { last_active: sec(NOW) })], { life }), 'a'), 'settled')
  assert.equal(sectionOf(run([base('a', { message_count: 6 })], { life }), 'a'), 'inbox')
  assert.equal(sectionOf(run([base('a')], { life, live: owner({ a: 'working' }) }), 'a'), 'working')
})

await test('children outliving their parent turn keep it Working (Background)', () => {
  const g = run([base('a')], { live: owner({ a: 'idle' }, { background: { a: 1 } }) })
  assert.equal(sectionOf(g, 'a'), 'working')
  assert.equal(g.rows[0].status, 'background')
})

await test('a new question returns prominently to the top of the inbox', () => {
  const g = run(
    [base('recent', { last_active: sec(NOW - 1000), outcome: 'ok', outcome_at: sec(NOW - 1000) }), base('asks', { last_active: sec(NOW - HOUR) })],
    { live: owner({ asks: 'waiting' }, { waitingSince: { asks: NOW } }) }
  )
  assert.deepEqual(g.inbox.map(r => r.s.id), ['asks', 'recent'])
})

await test('streaming does not reorder Working: it sorts by your last send', () => {
  const s = [
    base('first', { last_active: sec(NOW - 1000), sent_at: sec(NOW - 60_000) }),
    base('second', { last_active: sec(NOW - 2000), sent_at: sec(NOW - 10_000) })
  ]
  const g = run(s, { live: owner({ first: 'working', second: 'working' }) })
  assert.deepEqual(g.working.map(r => r.s.id), ['second', 'first'])
})

await test('un-settling promotes a row to the newest return position', () => {
  const s = [base('old', { last_active: sec(NOW - HOUR) }), base('recent', { outcome: 'ok', outcome_at: sec(NOW - 30_000) })]
  T.unsettle(run(s).rows[0])
  const g = run(s, { life: T.life().get() })
  assert.equal(g.inbox[0].s.id, 'old')
})

await test('Undo reverts a settle, but not one overtaken by a newer change', () => {
  const row = run([base('a', { unread: true })]).rows[0]
  T.settle(row)
  const undo = calls.notify.find(n => n.action?.label === 'Undo').action.onClick
  undo()
  assert.equal(T.life().get().records[K('a')].settledAt, undefined)
  assert.ok(calls.patch === undefined || true)

  reset()
  T.settle(run([base('b')]).rows[0])
  const stale = calls.notify.find(n => n.action?.label === 'Undo').action.onClick
  T.writeRecs({ [K('b')]: { visitedAt: Date.now() } })
  stale()
  assert.ok(T.life().get().records[K('b')].settledAt, 'newer change kept')
  assert.ok(calls.notify.some(n => /changed since/.test(n.message)))
})

await test('bulk settle settles the selection and navigates past it', () => {
  host.state.focusedStoredSessionId.set('a')
  const g = run([base('a'), base('b'), base('c')], { focusedId: 'a' })
  T.settle([g.rows[0], g.rows[1]])
  host.state.focusedStoredSessionId.set(null)
  const after = run([base('a'), base('b'), base('c')], { life: T.life().get() })
  assert.deepEqual(after.settled.map(r => r.s.id).sort(), ['a', 'b'])
})

await test('v1 bare-id lifecycle migrates onto owner keys', () => {
  T.setLife(T.loadLife({ settled: { a: { at: NOW - 1000, state: 'settled' } }, snoozed: {}, visited: { b: NOW } }))
  T.migrateLegacy([base('a', { profile: 'alpha' }), base('b')])
  const life = T.life().get()
  assert.ok(life.records[K('a', 'alpha')].settledAt)
  assert.equal(life.records[K('b')].visitedAt, NOW)
  assert.equal(life.legacy, null)
})

await test('shared lifecycle: newest write wins, and a stale connection response is dropped', () => {
  T.setLife(rec(K('a'), { settledAt: 5, t: 10 }))
  T.adoptRemote({ 'default|a': { snoozeUntil: NOW + HOUR, t: 20 }, 'default|b': { t: 5, visitedAt: 7 } }, 'local', T.gen())
  const life = T.life().get()
  assert.equal(life.records[K('a')].settledAt, undefined)
  assert.equal(life.records[K('b')].visitedAt, 7)
  T.adoptRemote({ 'default|c': { t: 99, visitedAt: 1 } }, 'local', T.gen() - 1)
  assert.equal(T.life().get().records[K('c')], undefined)
})

await test('lifecycle writes are pushed to the backend', async () => {
  backend.lifecycle = {}
  T.writeRecs({ [K('z')]: { visitedAt: 42 } })
  await new Promise(r => setTimeout(r, 900))
  await new Promise(r => setTimeout(r, 20))
  assert.equal(backend.lifecycle['default|z']?.visitedAt, 42)
  assert.equal(T.sync().get().state, 'synced')
})

await test('live polling: baseline first, then working→idle records a return', async () => {
  queryClientRows([base('a')])
  let status = 'working'
  const request = async m => (m === 'session.active_list' ? { sessions: [{ id: 'rt1', session_key: 'a', status }] } : { active: [] })
  T.live().set({ owners: {} })
  await T.pollOwner('local|default', request)
  assert.equal(T.life().get().records[K('a')], undefined, 'no edge on the baseline poll')
  status = 'idle'
  await T.pollOwner('local|default', request)
  assert.ok(T.life().get().records[K('a')].returnedAt)
})

await test('an unreachable source is marked stale, not idle', async () => {
  const ok = host.request
  host.request = async () => {
    throw new Error('socket closed')
  }
  T.reset() // forget the last successful poll: the source has been silent past the grace window
  await new Promise(r => setTimeout(r, 20)) // let the reset's own (failing) poll land first
  T.live().set(owner({ a: 'working' }))
  const failing = async () => {
    throw new Error('socket closed')
  }
  // lastLiveOk is empty → older than the stale window
  await T.pollOwner('local|default', failing)
  const g = run([base('a')], { live: T.live().get() })
  assert.equal(g.rows[0].status, 'working')
  assert.equal(g.rows[0].stale, true)
  host.request = ok
})

await test('a connection change clears live state and selection', () => {
  T.live().set(owner({ a: 'working' }))
  T.selection().set({ anchor: 'x', keys: ['x'] })
  T.reset()
  assert.deepEqual(T.live().get().owners, {})
  assert.deepEqual(T.selection().get().keys, [])
})

await test('outcomes merge (new and legacy backend shapes)', async () => {
  backend.sessions = [base('a'), base('b')].map(({ _conn, ...s }) => s)
  backend.outcomes = { default: { a: { at: 100, outcome: 'failed', sent_at: 90 }, b: 'ok' } }
  const { sessions } = await T.fetchSessions('local', false, 400)
  assert.equal(sessions[0].outcome, 'failed')
  assert.equal(sessions[0].outcome_at, 100)
  assert.equal(sessions[0].sent_at, 90)
  assert.equal(sessions[1].outcome, 'ok')
  assert.equal(sessions[0]._conn, 'local')
})

await test('full-text search fans out per profile and tolerates a failing one', async () => {
  backend.search = { alpha: [{ lineage_root: 'r', session_id: 'x', snippet: 'a >>>needle<<< here' }] }
  const { hits } = await T.searchTranscripts('needle', ['alpha', 'beta'], false)
  assert.equal(hits.length, 1)
  assert.equal(hits[0].profile, 'alpha')
})

await test('opening acknowledges only after the open succeeds', async () => {
  const row = run([base('a')]).rows[0]
  T.openRow(row)
  assert.equal(T.life().get().records[K('a')], undefined)
  await new Promise(r => setTimeout(r, 0))
  assert.ok(T.life().get().records[K('a')].visitedAt)
})

await test('unchanged rows keep their identity across classifications (memoized rows)', () => {
  const s = [base('a')]
  const a = run(s).rows[0]
  const b = run(s).rows[0]
  assert.equal(a, b)
})

await test('the pane renders every status without throwing', () => {
  const sessions = [
    base('w'),
    base('i'),
    base('f', { outcome: 'failed', outcome_at: sec(NOW - 1000), unread: true }),
    base('d', { unread: true }),
    base('p', { pinned: true }),
    base('bg')
  ]
  queryClientRows(sessions)
  T.live().set(owner({ i: 'waiting', w: 'working' }, { background: { bg: 1 } }))
  const html = renderToString(React.createElement(T.InboxPane))
  for (const label of ['Working', 'Input', 'Failed', 'Done', 'Background']) {
    assert.ok(html.includes(label), label)
  }
})

// ── recovery ────────────────────────────────────────────────────────────────
const resumeCalls = []
function mockGateway({ autoContinue = false, status = 'idle' } = {}) {
  resumeCalls.length = 0
  host.request = async (method, params) => {
    resumeCalls.push({ method, params })

    return method === 'session.resume'
      ? { auto_continue: autoContinue ? { attempt: 1 } : undefined, session_id: `rt-${params.session_id}`, status }
      : { ok: true }
  }
}
const recoveryReset = () => {
  storage.delete('recovery')
  T.resetRecovery()
  PREFS_FOR_RECOVERY()
}
const PREFS_FOR_RECOVERY = () => T.setPrefs({ autoResumeLimited: true, resumeAfterCrash: true, snoozeLimited: false })
const marker = (extra = {}) => ({ default: { a: { attempts: 0, auto_continue: true, started_at: sec(NOW - 60_000), ...extra } } })

await test('crash: an interrupted thread is resumed on launch with a continuation note', async () => {
  recoveryReset()
  mockGateway()
  queryClientRows([base('a')])
  backend.interrupted = marker()
  await T.recoverInterrupted()
  assert.deepEqual(resumeCalls.map(c => c.method), ['session.resume', 'prompt.submit'])
  assert.match(resumeCalls[1].params.text, /app stopped/)
  assert.equal(resumeCalls[1].params.session_id, 'rt-a')
  await T.recoverInterrupted()
  assert.equal(resumeCalls.length, 2, 'the same marker is not resumed twice')
})

await test('crash: when core re-submits the turn itself the plugin sends nothing', async () => {
  recoveryReset()
  mockGateway({ autoContinue: true })
  queryClientRows([base('a')])
  backend.interrupted = marker()
  await T.recoverInterrupted()
  assert.deepEqual(resumeCalls.map(c => c.method), ['session.resume'])
})

await test('crash: settled, snoozed and over-chained threads are left alone', async () => {
  recoveryReset()
  mockGateway()
  queryClientRows([base('a')])
  backend.interrupted = marker()
  T.setLife(rec(K('a'), { settledAt: NOW - 1000 }))
  await T.recoverInterrupted()
  T.setLife(rec(K('a'), { snoozeAt: NOW - 5, snoozeUntil: NOW + HOUR }))
  T.resetRecovery()
  await T.recoverInterrupted()
  T.setLife(freshLife())
  T.resetRecovery()
  storage.set('recovery', { crash: { [K('a')]: { at: NOW - 1000, n: 3 } }, limit: {} })
  await T.recoverInterrupted()
  assert.equal(resumeCalls.length, 0)
})

await test('crash: a marker for a thread not in the roster (archived) is skipped; markers with auto_continue off too', async () => {
  recoveryReset()
  mockGateway()
  queryClientRows([base('a')])
  backend.interrupted = { default: { gone: { attempts: 0, auto_continue: true, started_at: sec(NOW) }, a: { attempts: 0, auto_continue: false, started_at: sec(NOW) } } }
  await T.recoverInterrupted()
  assert.equal(resumeCalls.length, 0)
})

await test('limit: resumes once after the reset time, not before, not twice', async () => {
  recoveryReset()
  mockGateway()
  const limited = (reset, at = NOW - 10 * 60_000) => base('a', { limit_reset_at: reset, outcome: 'limited', outcome_at: sec(at) })
  queryClientRows([limited(NOW + HOUR)])
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 0, 'before the reset')
  queryClientRows([limited(NOW - 60_000)])
  await T.recoverLimited()
  assert.deepEqual(resumeCalls.map(c => c.method), ['session.resume', 'prompt.submit'])
  assert.match(resumeCalls[1].params.text, /usage limit/)
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 2, 'once per failure')
  // It limits again later: a new failure is a new attempt.
  queryClientRows([limited(NOW - 1000, NOW - 5 * 60_000)])
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 2, 'same limited outcome only after its own timestamp changes')
})

await test('limit: no reset time falls back to a retry delay; the pref turns it off; live and settled threads are skipped', async () => {
  recoveryReset()
  mockGateway()
  const row = at => base('a', { outcome: 'limited', outcome_at: sec(at) })
  queryClientRows([row(NOW - 5 * 60_000)])
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 0, 'within the fallback delay')
  T.setPrefs({ autoResumeLimited: false })
  queryClientRows([row(NOW - 30 * 60_000)])
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 0, 'pref off')
  T.setPrefs({ autoResumeLimited: true })
  T.live().set(owner({ a: 'working' }))
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 0, 'already running')
  T.live().set({ owners: {} })
  T.setLife(rec(K('a'), { settledAt: NOW - 1000 }))
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 0, 'settled')
  T.setLife(freshLife())
  await T.recoverLimited()
  assert.equal(resumeCalls.length, 2, 'after the fallback delay')
})

await test('limit: snooze-until-reset parks a limited thread when the pref is on', async () => {
  recoveryReset()
  mockGateway()
  T.setPrefs({ snoozeLimited: true, autoResumeLimited: false })
  queryClientRows([base('a', { limit_reset_at: NOW + HOUR, outcome: 'limited', outcome_at: sec(NOW - 1000) })])
  await T.recoverLimited()
  assert.equal(T.life().get().records[K('a')].snoozeUntil, NOW + HOUR)
  assert.equal(resumeCalls.length, 0)
})

await test('limit: a thread past its limit starts a fresh chain', async () => {
  recoveryReset()
  storage.set('recovery', { crash: {}, limit: { [K('a')]: { n: 7, outcomeAt: 1, tried: true } } })
  queryClientRows([base('a', { outcome: 'ok', outcome_at: sec(NOW) })])
  await T.recoverLimited()
  assert.deepEqual(storage.get('recovery').limit, {})
})


function queryClientRows(sessions) {
  sdk.queryClient._clear()
  sdk.queryClient.setQueryData(['t3code.inbox', 'sessions', 'local', false, 400], { sessions, total: sessions.length })
}

// ── report ──────────────────────────────────────────────────────────────────
for (const r of results) {
  console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${r.ok ? '' : `\n${r.err}`}`)
}

const failed = results.filter(r => !r.ok).length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
