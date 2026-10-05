// UX 05/10, pure logic (tested in test/ux.test.js): the menu bar icon state and where the meeting overlay may reappear.

// nothing = idle, ● = listening, ❚❚ = paused, … = getting ready or still writing up a stopped meeting
function trayState(meeting, finishing, detected) {
  if (meeting.state === 'recording') return { title: meeting.paused ? '❚❚' : '●', tip: meeting.paused ? 'Middy: paused' : 'Middy: listening' }
  if (meeting.state === 'starting' || finishing > 0) return { title: '…', tip: meeting.state === 'starting' ? 'Middy: getting ready' : 'Middy: writing up the last meeting' }
  return { title: '', tip: detected ? 'Middy: ' + detected.appLabel + ' meeting detected' : 'Middy' }
}

// a remembered overlay position is used only if its top-left 40 px still lie on a connected display (an unplugged monitor)
function onSomeDisplay(pos, workAreas) {
  return !!pos && workAreas.some((a) => pos.x >= a.x && pos.y >= a.y && pos.x + 40 <= a.x + a.width && pos.y + 40 <= a.y + a.height)
}

module.exports = { trayState, onSomeDisplay }
