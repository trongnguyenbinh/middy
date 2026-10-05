// Every screen of the renderer renders (server-side, no DOM) with a fake bridge: catches a broken import, a JSX error or a
// render that throws on the initial state. Effects do not run here; their logic lives in renderer/lib (renderer_lib.test.mjs).
import test from 'node:test'
import assert from 'node:assert'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import * as esbuild from 'esbuild'

const APP = path.join(path.dirname(fileURLToPath(import.meta.url)), '..')
const SCREENS = { Toolbar: 'Toolbar', Overlay: 'Overlay', Preview: 'Preview', Settings: 'Settings', Library: 'Library', QuitWarning: 'QuitWarning' }

async function load() {
  const entry = Object.keys(SCREENS).map((k) => `export { ${k} } from './renderer/screens/${SCREENS[k]}.jsx'`).join('\n') +
    "\nexport { renderToStaticMarkup } from 'react-dom/server'\nexport { createElement } from 'react'\nexport { api } from './renderer/index.jsx'\n"
  const stub = { name: 'stub-index', setup(b) { b.onResolve({ filter: /\/index\.jsx$/ }, () => ({ path: path.join(APP, 'test', 'fixtures', 'index_stub.jsx') })) } }
  const out = path.join(APP, 'node_modules', '.cache', 'midy-test', 'screens.mjs')   // inside the app: npm packages stay external and resolve
  await esbuild.build({ stdin: { contents: entry, resolveDir: APP, loader: 'jsx' }, bundle: true, packages: 'external', format: 'esm', platform: 'node', jsx: 'automatic',
    loader: { '.wasm': 'binary' }, outfile: out, plugins: [stub], logLevel: 'silent', define: { 'process.env.NODE_ENV': '"production"' } })
  return import(pathToFileURL(out).href)
}

const mod = await load()
const render = (name) => mod.renderToStaticMarkup(mod.createElement(mod[name]))

for (const name of Object.keys(SCREENS)) {
  test(`${name} renders`, () => { assert.ok(render(name).length > 20) })
}

test('QuitWarning text and buttons', () => {
  const h = render('QuitWarning')
  assert.ok(h.includes('Unsaved meeting notes') && h.includes('Quit anyway') && h.includes('Go back'))
})

test('render does not call the bridge (calls happen in effects only)', () => {
  assert.deepStrictEqual(mod.api.calls, [])
})
