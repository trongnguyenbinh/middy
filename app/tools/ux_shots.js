// README screenshots of the meeting UI, rendered HEADLESS (no window on screen, no Electron, no daemon): the real renderer bundle
// (dist/) with a fake window.midy bridge and sample data, in headless Chromium through playwright-core.
//   npm run build && npm i --no-save playwright-core && npx playwright install chromium-headless-shell
//   node tools/ux_shots.js [out dir, default ../docs/ux]        (PLAYWRIGHT=<path to playwright-core>, CHROME=<headless binary>)
// The menu bar and the notification are native macOS UI: they are drawn here as HTML mock-ups (marked as such in the README).
const fs = require('fs')
const os = require('os')
const path = require('path')
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright-core')

const APP = path.join(__dirname, '..')
const DIST = path.join(APP, 'dist')
const OUT = path.resolve(process.argv[2] || path.join(APP, '..', 'docs', 'ux'))
const url = (f) => 'file://' + f

const now = Date.now()
const TRANSCRIPT = [
  { id: 'b1', speaker: 'Speaker 1', source: 'system', state: 'refined', text: 'Chào mọi người, hôm nay mình chốt kế hoạch ra mắt bản thử nghiệm.' },
  { id: 'b2', speaker: 'You', source: 'mic', state: 'refined', text: 'Bản cài đặt đã xong, còn phần hướng dẫn mở app lần đầu.' },
  { id: 'b3', speaker: 'Speaker 2', source: 'system', state: 'refined', text: 'Vậy thứ Sáu gửi bản rc cho nhóm thử, thứ Hai tổng hợp góp ý nhé.' },
  { id: 'b4', speaker: 'Speaker 1', source: 'system', state: 'refined', text: 'Đồng ý. Ai phụ trách phần README?' },
]
const SETTINGS = { shortcut: 'Control+Alt+R', micInput: true, language: 'Vietnamese', space: 'default', screenCapture: false, micDeviceId: 'default',
  summarizer: 'claude', widget: false, overlay: true, overlayOpacity: 0.3 }
const MEETING = { state: 'recording', id: 7, title: 'Kế hoạch bản thử nghiệm', startedAt: now - 760e3, readyAt: now - 754e3, language: 'Vietnamese', space: 'default', stats: {} }

// fake bridge: answers the renderer's invoke() calls from the sample data above
const bridge = (win) => `
  window.midy = {
    window: ${JSON.stringify(win)},
    invoke: (ch, p) => Promise.resolve(({
      'meeting:state': ${JSON.stringify(MEETING)}, 'settings:get': ${JSON.stringify(SETTINGS)},
      'daemon': p && p.cmd === 'snapshot' ? { ok: true, snapshot: { windowOffset: 0, window: ${JSON.stringify(TRANSCRIPT)} } } : { ok: true },
      'shortcut:get': { accelerator: 'Control+Alt+R', label: '⌃⌥R', ok: true }, 'net:status': { done: true, blocked: true, tcp: 'blocked:EPERM', dns: 'blocked:ENOTFOUND', sandbox: true, blockedRequests: 0 },
      'ax:status': { trusted: false }, 'mic-state:get': null, 'reminder:get': null,
    })[ch] ?? null),
    on: () => () => {}, sendMic: () => {},
  }`

// a desktop behind the see-through overlay: a light document or a dark editor
const DESKTOP = {
  light: '<div class="shot-bg light"><h1>Release checklist</h1>' + '<p>Ship the installer, write the first-run notes, collect feedback from the test group, fix what blocks the next release candidate. </p>'.repeat(8) + '</div>',
  plain: '<div class="shot-bg plain"></div>',            // stands in for the native frosted (vibrancy) background of the window
  dark: '<div class="shot-bg dark"><pre>' + 'const meeting = await middy.start({ language: "vi" })\nfor await (const line of meeting.transcript) render(line)\n'.repeat(14) + '</pre></div>',
}
const BG_CSS = `.shot-bg{position:fixed;inset:0;z-index:-1;padding:24px 28px;overflow:hidden;font:15px/1.6 -apple-system,system-ui,sans-serif}
  .shot-bg.light{background:#f6f6f3;color:#222}.shot-bg.dark{background:#1e1f24;color:#9ad1a8}.shot-bg.plain{background:#2f3137}.shot-bg pre{margin:0;font:13px/1.7 Menlo,monospace}
  .shot-bg h1{margin:0 0 12px;font-size:22px}`

function page(win, desktop) {
  const html = `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="${url(path.join(DIST, 'styles.css'))}"><style>${BG_CSS}</style>
    <script>${bridge(win)}</script></head><body>${desktop ? DESKTOP[desktop] : ''}<div id="root"></div><script src="${url(path.join(DIST, 'renderer.js'))}"></script></body></html>`
  const f = path.join(os.tmpdir(), 'middy-shot-' + win + '-' + (desktop || 'none') + '.html'); fs.writeFileSync(f, html); return url(f)
}

// native UI, drawn as HTML (labelled "mock-up" in the README)
const ICON = 'data:image/png;base64,' + fs.readFileSync(path.join(APP, 'renderer', 'icons', 'trayTemplate@2x.png')).toString('base64')
const MENUBAR = `<!doctype html><html><head><meta charset="utf-8"><style>
  body{margin:0;font:13px -apple-system,system-ui,sans-serif;background:#2b2d33;color:#fff;padding:14px}
  .bar{display:flex;align-items:center;gap:14px;height:26px;padding:0 12px;border-radius:7px;background:rgba(40,40,46,.92);margin:8px 0;width:330px}
  .bar img{height:16px;filter:invert(1)}.item{display:flex;align-items:center;gap:4px}.t{font-size:12px}.cap{color:#a2a6ad;font-size:12px;margin-left:auto}
  .menu{margin-top:12px;width:250px;background:rgba(50,52,58,.97);border-radius:9px;padding:5px;box-shadow:0 8px 24px rgba(0,0,0,.4)}
  .menu div{padding:4px 10px;border-radius:5px}.menu .sep{height:1px;background:rgba(255,255,255,.12);padding:0;margin:5px 4px}.menu .dis{color:#8a8e95}
  .menu .hi{background:#3a6fe0}.rec{color:#ff5f57}</style></head><body>
  <div class="bar"><span class="item"><img src="${ICON}"></span><span class="cap">idle</span></div>
  <div class="bar"><span class="item"><img src="${ICON}"><span class="t">●</span></span><span class="cap">listening</span></div>
  <div class="bar"><span class="item"><img src="${ICON}"><span class="t">…</span></span><span class="cap">getting ready / writing up</span></div>
  <div class="menu"><div class="hi">Stop meeting</div><div>Show overlay</div><div class="sep"></div><div>Open Middy</div><div>Settings</div>
  <div>Floating button</div><div class="dis">Middy v0.1.0 · overlay hidden</div><div class="sep"></div><div>Quit</div></div></body></html>`
const NOTIFICATION = `<!doctype html><html><head><meta charset="utf-8"><style>
  body{margin:0;padding:16px;background:linear-gradient(135deg,#5b6b8c,#2e3546);font:13px -apple-system,system-ui,sans-serif}
  .n{width:344px;display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:16px;background:rgba(236,236,240,.86);color:#111;box-shadow:0 6px 20px rgba(0,0,0,.25)}
  .ic{width:34px;height:34px;border-radius:8px;background:#1f2126;display:flex;align-items:center;justify-content:center}.ic img{height:20px;filter:invert(1)}
  b{display:block;margin-bottom:2px}.m{color:#444}.ago{margin-left:auto;color:#666;font-size:11px}</style></head><body>
  <div class="n"><div class="ic"><img src="${ICON}"></div><div><b>Zoom meeting detected</b><span class="m">Click to start recording with Middy.</span></div><span class="ago">now</span></div></body></html>`

async function main() {
  if (!fs.existsSync(path.join(DIST, 'renderer.js'))) throw new Error('run `npm run build` first')
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME || undefined })
  const ctx = await browser.newContext({ deviceScaleFactor: 2 })
  const shoot = async (name, src, size, prep) => {
    const p = await ctx.newPage(); await p.setViewportSize(size)
    await p.goto(src); await p.waitForTimeout(800)
    if (prep) await prep(p)
    await p.screenshot({ path: path.join(OUT, name + '.png') }); await p.close(); console.log('shot', name)
  }
  const ov = { width: 400, height: 560 }
  await shoot('overlay-light', page('meeting-overlay', 'light'), ov)
  await shoot('overlay-dark', page('meeting-overlay', 'dark'), ov)
  await shoot('overlay-hover', page('meeting-overlay', 'light'), ov, (p) => p.hover('.overlay'))
  await shoot('settings', page('settings', 'plain'), { width: 480, height: 640 }, (p) => p.evaluate(() => {
    const l = [...document.querySelectorAll('.label')].find((e) => e.textContent === 'MEETING OVERLAY'); l.scrollIntoView() }))
  await shoot('menubar', 'data:text/html,' + encodeURIComponent(MENUBAR), { width: 360, height: 360 })
  await shoot('notification', 'data:text/html,' + encodeURIComponent(NOTIFICATION), { width: 376, height: 96 })
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
