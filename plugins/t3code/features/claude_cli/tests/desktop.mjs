import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
const here = path.dirname(fileURLToPath(import.meta.url))
const MODULES = process.env.HERMES_NODE_MODULES || path.resolve(here, '../../../../../hermes-agent/node_modules')
const require = createRequire(path.join(MODULES, 'x.js'))
const React = require('react')
const { JSDOM } = require('jsdom')
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'https://hermes.test/' })
Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true })
const { createRoot } = require('react-dom/client')
const out = path.join(MODULES, `.claude-context-${process.pid}.mjs`)
try {
  await require('esbuild').build({
    stdin: { contents: `export { default } from ${JSON.stringify(path.resolve(here,'../desktop.js'))}; export * from ${JSON.stringify(path.join(here,'mock-sdk.mjs'))}`, resolveDir: here },
    alias: { '@hermes/plugin-sdk': path.join(here, 'mock-sdk.mjs') },
    bundle: true, external: ['react','nanostores'], format: 'esm', platform: 'node', nodePaths: [MODULES], outfile: out
  })
  const { default: feature, fixture, host } = await import(pathToFileURL(out).href)
  let contribution
  feature.register({ register: c => { contribution=c }, onEvent: () => () => {} })
  const root = createRoot(document.getElementById('root'))
  const render = () => React.act(async () => root.render(contribution.render()))
  const click = text => React.act(async () => {
    const button = [...document.querySelectorAll('button')].find(b => b.textContent.includes(text))
    assert(button, `missing ${text}`)
    button.click()
  })
  fixture.info = { model: 'claude-sonnet-5-5', provider: 'claude-cli' }
  await render()
  assert(document.body.textContent.includes('Context 1M'))
  await click('200K ·')
  assert.deepEqual(fixture.calls[0], {
    route: { connectionId:'local', profile:'owner-a', targetProfile:'owner-a', mode:'local' },
    method:'config.set', params: { session_id:'runtime-a',key:'model',value:'claude-sonnet-5-5[200k] --provider claude-cli --session' }
  })
  fixture.info = { model:'claude-sonnet-5-5[200k]',provider:'claude-cli' }
  await render()
  assert(document.body.textContent.includes('Context 200K'))
  fixture.result = { confirm_required:true, confirm_message:'Context will grow' }
  await click('1M ·')
  assert(fixture.confirmed)
  assert.equal(fixture.calls.at(-1).params.value,'claude-sonnet-5-5[1m] --provider claude-cli --session')
  assert.equal(fixture.calls.at(-1).params.confirm_expensive_model,true)
  console.log('PASS context roundtrip: owner-routed session-only writes, native cap, restore, shared confirmation')
  await React.act(async () => host.state.busy.set(true))
  assert([...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Context')).disabled)
  const count=fixture.calls.length
  await click('1M ·')
  assert.equal(fixture.calls.length,count)
  fixture.info={model:'gpt-example',provider:'openai-codex'}
  await render()
  assert.equal(document.body.textContent,'')
  await React.act(async () => root.unmount())
  console.log('PASS selector isolation: hidden outside Claude; no writes while the session is busy')
} finally { fs.rmSync(out,{force:true}); dom.window.close() }
