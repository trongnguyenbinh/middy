// Quit warning: "Unsaved meeting notes" -> "Quit anyway" / "Go back"
import React from 'react'
import { api } from '../index.jsx'

export function QuitWarning() {
  return (
    <div className="popup">
      <div className="pp-head"><span className="logo-m small">M</span><span>Unsaved meeting notes</span></div>
      <p className="muted">Please save your notes before quitting or they may be lost.</p>
      <div className="row right"><button className="btn-ghost" onClick={() => api.invoke('quit')}>Quit anyway</button><button className="btn-accent" onClick={() => api.invoke('window:close', 'quit-warning')}>Go back</button></div>
    </div>
  )
}
