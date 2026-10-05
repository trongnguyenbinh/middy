// Bridge between the renderer (React) and the main process. contextIsolation stays on; the renderer only sees `window.midy`,
// and only the channels listed in preload/channels.js.
const { contextBridge, ipcRenderer } = require('electron')
const { INVOKE, ON } = require('./channels.js')

contextBridge.exposeInMainWorld('midy', {
  invoke: (channel, payload) => INVOKE.has(channel) ? ipcRenderer.invoke(channel, payload) : Promise.reject(new Error('IPC channel not allowed: ' + channel)),
  on: (channel, fn) => {
    if (!ON.has(channel)) throw new Error('IPC channel not allowed: ' + channel)
    const h = (_e, data) => fn(data)
    ipcRenderer.on(channel, h)
    return () => ipcRenderer.removeListener(channel, h)
  },
  // mic PCM (ArrayBuffer of s16le 16 kHz) -> main -> daemon stream 1
  sendMic: (buf) => ipcRenderer.send('mic-audio', buf),
  window: location.hash.replace('#', '') || 'toolbar',
})
