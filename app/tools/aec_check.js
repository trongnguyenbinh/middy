// Quick check: can the AEC3 worklet bundle initialise inside Electron's AudioWorkletGlobalScope? Prints the status and quits.
const { app, BrowserWindow } = require('electron')
const path = require('path')
app.commandLine.appendSwitch('no-sandbox')
app.whenReady().then(() => {
  const w = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, sandbox: false } })
  w.webContents.on('console-message', (_e, _l, msg) => { console.log('PAGE', msg); if (/AEC-RESULT/.test(msg)) setTimeout(() => app.quit(), 200) })
  w.loadFile(path.join(__dirname, '..', 'dist', 'aec_check.html'))
  setTimeout(() => { console.log('TIMEOUT'); app.quit() }, 15000)
})
