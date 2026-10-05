// Stands in for renderer/index.jsx in the screen tests: a fake window.midy bridge and the two hooks, no createRoot().
const calls = []
export const api = {
  calls,
  window: 'test',
  invoke: (channel, payload) => { calls.push([channel, payload]); return Promise.resolve(null) },
  on: () => () => {},
  sendMic: () => {},
}
export function useMeeting() { return globalThis.__meeting || { state: 'idle' } }
export function useSettings() {
  return [{ shortcut: 'Control+Alt+R', micInput: true, language: 'English', space: 'default', screenCapture: false, micDeviceId: 'default' }, () => {}]
}
