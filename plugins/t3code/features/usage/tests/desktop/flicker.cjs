const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const hostRoot = process.env.HERMES_SOURCE_ROOT || path.resolve(__dirname, '../../../../../../hermes-agent');
const desktopFile = process.env.T3_USAGE_DESKTOP_FILE || path.resolve(__dirname, '../../desktop.js');
const requireHost = createRequire(path.join(hostRoot, 'package.json'));
const { JSDOM } = requireHost('jsdom');
const esbuild = requireHost('esbuild');
const React = requireHost('react');
const { createRoot } = requireHost('react-dom/client');

async function main() {
  const dom = new JSDOM('<!doctype html><html><body><div id="mount"></div></body></html>', { url: 'https://hermes.test/#/chat', pretendToBeVisual: true });
  Object.assign(global, { window: dom.window, document: dom.window.document, MutationObserver: dom.window.MutationObserver, IS_REACT_ACT_ENVIRONMENT: true });
  const source = fs.readFileSync(desktopFile, 'utf8');
  const mock = `
    import React from 'react';
    export const atom = value => ({get:()=>value,set:v=>{value=v}});
    export const useValue = atom => atom.get();
    export const useQuery = () => ({data:null,isLoading:true,isFetching:false});
    export const queryClient = {};
    export const host = {navigate(){}};
    export const cn = (...v) => v.filter(Boolean).join(' ');
    export const Button = ({children,...props}) => React.createElement('button',props,children);
    export const Tip = ({children}) => children;
    export const ROUTES_AREA='routes', PALETTE_AREA='palette';
    export const STATUSBAR_AREAS={right:'statusBar.right'};
  `;
  const built = await esbuild.build({
    stdin: { contents: source + '\nexport {UsageGraft};', resolveDir: hostRoot },
    bundle: true, platform: 'node', format: 'cjs', write: false, external:['react'],
    plugins:[{name:'test-sdk',setup(build){
      build.onResolve({filter:/^@hermes\/plugin-sdk$/},()=>({path:'sdk',namespace:'mock'}));
      build.onLoad({filter:/.*/,namespace:'mock'},()=>({contents:mock,resolveDir:hostRoot}));
    }}]
  });
  const exported = {exports:{}};
  new Function('require','module','exports',built.outputFiles[0].text)(requireHost,exported,exported.exports);
  exported.exports.default.register({storage:{get:()=>({metric:'tokens',days:30})},setInterval(){},registerMany(){}});
  const root = createRoot(document.querySelector('#mount'));
  try {
    await React.act(async()=>{root.render(React.createElement(exported.exports.UsageGraft));});
    let stockHiddenAtFirstPaint = false;
    await React.act(async()=>{
      window.history.pushState({},'', '#/command-center?section=usage');
      document.body.insertAdjacentHTML('beforeend','<div id="overlay"><aside data-tour="overlay-nav"></aside><main><header><div><button aria-pressed="true">30D</button></div></header><div id="content"><div id="stock">OLD USAGE</div></div></main></div>');
      await new Promise(resolve=>window.requestAnimationFrame(()=>{
        stockHiddenAtFirstPaint=window.getComputedStyle(document.querySelector('#stock')).display==='none';
        resolve();
      }));
    });
    assert.equal(stockHiddenAtFirstPaint,true,'Stock usage must be hidden before the first animation frame, not after a polling delay');
    assert.equal(document.querySelector('#content > [data-t3u-host]')!==null,true,'Plugin must mount in the Usage tab');
    await React.act(async()=>{
      window.history.pushState({},'', '#/command-center?section=system');
      document.querySelector('#content').appendChild(document.createElement('div'));
      await new Promise(resolve=>setTimeout(resolve,20));
    });
    assert.equal(document.querySelector('#content').hasAttribute('data-t3u-active'),false,'Switching away restores normal content');
    await React.act(async()=>{
      window.history.pushState({},'', '#/command-center?section=usage');
      document.querySelector('#content').appendChild(document.createElement('div'));
      await new Promise(resolve=>setTimeout(resolve,20));
    });
    assert.equal(document.querySelectorAll('#content > [data-t3u-host]').length,1,'Reopening creates no duplicate panel');
    console.log('PASS: old panel hidden before first paint; tab navigation restores content; no duplicates');
  } finally {
    await React.act(async()=>root.unmount());
    dom.window.close();
  }
}
main().catch(error=>{console.error(error.message);process.exitCode=1});
