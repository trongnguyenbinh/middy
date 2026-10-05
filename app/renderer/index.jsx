import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Toolbar } from './screens/Toolbar.jsx'
import { Overlay } from './screens/Overlay.jsx'
import { Preview } from './screens/Preview.jsx'
import { Settings } from './screens/Settings.jsx'
import { Library } from './screens/Library.jsx'
import { QuitWarning } from './screens/QuitWarning.jsx'
import { MeetingDetected } from './screens/MeetingDetected.jsx'

export const api = window.midy

export function useMeeting() {
  const [m, setM] = useState({ state: 'idle' })
  useEffect(() => { api.invoke('meeting:state').then(setM); return api.on('meeting', setM) }, [])
  return m
}

export function useSettings() {
  const [s, setS] = useState(null)
  useEffect(() => { api.invoke('settings:get').then(setS); return api.on('settings', setS) }, [])
  return [s, (patch) => api.invoke('settings:set', patch).then(setS)]
}

const SCREENS = { toolbar: Toolbar, 'meeting-overlay': Overlay, 'preview-window': Preview, settings: Settings, library: Library, 'quit-warning': QuitWarning, 'meeting-detected': MeetingDetected }

function App() {
  const name = api.window
  const Screen = SCREENS[name] || (() => <div className="pad">Unknown window {name}</div>)
  useEffect(() => { document.body.dataset.win = name }, [name])
  return <Screen />
}

createRoot(document.getElementById('root')).render(<App />)
