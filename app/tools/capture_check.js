// Sample accounting of the renderer mic path with the fake device:  electron tools/capture_check.js <wav48k> [seconds]
const { app, BrowserWindow } = require('electron')
const path = require('path')
const wav = process.argv[2], T = Number(process.argv[3] || 20), LEG = process.argv[4] || 'ctx16'
app.commandLine.appendSwitch('use-fake-device-for-media-stream')
app.commandLine.appendSwitch('use-file-for-fake-audio-capture', wav + '%noloop')
app.commandLine.appendSwitch('no-sandbox')
app.whenReady().then(() => {
  if (process.argv.includes('--dock-hide')) app.dock.hide()          // like the app (accessory app => App Nap candidate)
  const MODE = process.argv[5] || 'plain'          // plain | panel-hidden | panel-visible
  const panel = MODE.startsWith('panel')
  const w = new BrowserWindow({ show: false, width: 88, height: 88, frame: !panel, transparent: panel, alwaysOnTop: panel, type: panel ? 'panel' : undefined, vibrancy: panel ? 'fullscreen-ui' : undefined,
    webPreferences: { contextIsolation: true, sandbox: false, backgroundThrottling: false } })
  if (panel) { w.setAlwaysOnTop(true, 'screen-saver'); w.once('ready-to-show', () => { w.show(); if (MODE === 'panel-hidden') setTimeout(() => w.hide(), 1500) }) }
  w.webContents.on('console-message', (_e, _l, msg) => { if (/^CC/.test(msg)) console.log(msg); if (/CC-RESULT/.test(msg)) setTimeout(() => app.quit(), 200) })
  w.webContents.session.setPermissionRequestHandler((_w, _p, cb) => cb(true))
  w.loadFile(path.join(__dirname, '..', 'dist', 'capture_check.html'), { query: { t: String(T), leg: LEG } })
  setTimeout(() => { console.log('TIMEOUT'); app.quit() }, (T + 15) * 1000)
})
