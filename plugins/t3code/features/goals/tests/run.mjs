import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import * as sdk from './mock-sdk.mjs'
const here = path.dirname(fileURLToPath(import.meta.url))
const root = process.env.HERMES_SOURCE_ROOT
if (!root) throw new Error('Set HERMES_SOURCE_ROOT to the Hermes source tree')
const require = createRequire(path.join(root, 'package.json'))
const React = require('react'), { renderToStaticMarkup } = require('react-dom/server')
const out = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), 'goal-ui-test-'))
try {
  await require('esbuild').build({ entryPoints: [path.join(here, '../desktop.js')], outfile: path.join(out, 'ui.mjs'),
    bundle: true, format: 'esm', platform: 'node', plugins: [{ name: 'test-dependencies', setup(build) {
      build.onResolve({ filter: /^@hermes\/plugin-sdk$/ }, () => ({ path: path.join(here, 'mock-sdk.mjs'), external: true }))
      build.onResolve({ filter: /^react$/ }, () => ({ path: require.resolve('react'), external: true }))
    } }] })
  const T = await import(pathToFileURL(path.join(out, 'ui.mjs')))
  const goal = { objective: 'Ship the real goal', status: 'active' }
  for (const model of ['plain-api', 'claude-cli', 'muse-code', 'antigravity']) {
    sdk.host.state.model.set(model)
    sdk.setQuery({ data: goal, isError: false })
    assert.match(renderToStaticMarkup(React.createElement(T.GoalStrip)), /Pursuing goal.*Ship the real goal/)
  }
  for (const [status, label] of [['paused', 'Goal paused'], ['waiting', 'Waiting on goal'], ['done', 'Goal complete']]) {
    assert.ok(renderToStaticMarkup(React.createElement(T.GoalCallout, { goal: { ...goal, status } })).includes(label))
  }
  assert.equal(renderToStaticMarkup(React.createElement(T.GoalCallout, { goal: null })), '')
  assert.equal(renderToStaticMarkup(React.createElement(T.GoalCallout, { goal: { ...goal, status: 'cleared' } })), '')
  let request
  globalThis.window = { hermesDesktop: { api: async req => { request = req; return { session_id: 'thread', goal } } } }
  assert.deepEqual(await T.fetchGoal({ connectionId: 'remote', profile: 'verpin' }, 'thread'), goal)
  assert.equal(request.connectionId, 'remote')
  assert.equal(request.profile, 'verpin')
  assert.match(request.path, /profile=verpin/)
  globalThis.window.hermesDesktop.api = async () => ({ session_id: 'foreign', goal })
  await assert.rejects(T.fetchGoal({ connectionId: 'remote', profile: 'verpin' }, 'thread'), /another thread/)
  const rows = [{ id: 1, role: 'user', content: '/goal Ship native', timestamp: 1 },
    { id: 2, role: 'assistant', content: 'Goal set: Ship native', timestamp: 2 }]
  globalThis.window.hermesDesktop.api = async req => {
    if (req.path.includes('/plugins/')) throw Object.assign(new Error('HTTP 404'), { status: 404 })
    return { session_id: 'native', messages: rows }
  }
  const native = await T.fetchGoal({ connectionId: 'local', profile: 'legacy' }, 'native', 'live')
  assert.equal(native.objective, 'Ship native')
  assert.equal(sdk.calls.at(-1).method, 'session.control.read')
  sdk.setControl({ goal: 'Hermes API goal', status: 'paused', created_at: 5 })
  assert.equal((await T.fetchGoal({ connectionId: 'local', profile: 'legacy' }, 'native', 'live')).status, 'paused')
  console.log('PASS: four harnesses; lifecycle/empty; exact owner routing; foreign-response rejection; running-backend compatibility')
} finally { fs.rmSync(out, { recursive: true, force: true }) }
