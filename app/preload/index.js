// Bridge between the renderer (React) and the main process. contextIsolation stays on; the renderer only sees `window.midy`.
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('midy', {
  invoke: (channel, payload) => ipcRenderer.invoke(channel, payload),
  on: (channel, fn) => {
    const h = (_e, data) => fn(data)
    ipcRenderer.on(channel, h)
    return () => ipcRenderer.removeListener(channel, h)
  },
  // mic PCM (ArrayBuffer of s16le 16 kHz) -> main -> daemon stream 1
  sendMic: (buf) => ipcRenderer.send('mic-audio', buf),
  window: location.hash.replace('#', '') || 'toolbar',
})
