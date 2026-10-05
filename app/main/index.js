// Middy main process: windows, tray, daemon client, audio plumbing, network guard.
// Flags (blind regression runs only):  --fake-mic <wav 16k>  --fake-system <wav 16k>  --shots <dir>  --auto-start [--duration s]
const { app, BrowserWindow, Tray, Menu, Notification, ipcMain, session, screen, dialog, nativeImage, shell, systemPreferences, powerSaveBlocker, globalShortcut } = require('electron')
const { DEFAULT_SHORTCUT, accelFromKey, createShortcut } = require('./shortcut.js')
const path = require('path')
const fs = require('fs')
const { execFile } = require('child_process')
const { Daemon, ROOT } = require('./daemon')
const { trayState, onSomeDisplay } = require('./ux')
const { SystemAudio } = require('./audiotee')
const { createDetector, runProbe, parse: parseProbe, micInputEffective, labelFor } = require('./meeting_detector')
const micGate = { appMicOn: null }                      // Lỗi 9b: meeting app unmuted? from the read-only Accessibility helper (main/mic_state.js)
const { createMicStateWatcher } = require('./mic_state')
const micInputOn = () => micInputEffective({ appMicOn: micGate.appMicOn, button: settings.micInput })

const argv = process.argv.slice(1)
const flag = (k) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : null }
// --no-show = a test run that must stay INVISIBLE on the user's Mac (rule 28/09 after the 16:50 test icon was taken for the real app):
// no Dock icon (accessory policy set before the app finishes launching), no tray, every window off-screen and never focused.
const NO_SHOW = argv.includes('--no-show')
if (NO_SHOW && process.platform === 'darwin') app.dock?.hide()
const FAKE_MIC = flag('--fake-mic'), FAKE_SYSTEM = flag('--fake-system'), SHOTS = flag('--shots')
const AUTO_START = argv.includes('--auto-start'), DURATION = Number(flag('--duration') || 0), FORCE_AEC = argv.includes('--force-aec'), FORCE_HP = argv.includes('--force-headphone'), LANG = flag('--language')

const ROOT_WS = process.env.MIDY_ROOT || (app.isPackaged ? path.join(require('os').homedir(), 'middy') : path.resolve(__dirname, '..', '..'))
// Kernel-level network block for the WHOLE process tree, however the app was opened (Finder, Dock, Spotlight, `open`, `electron .`):
// re-exec ourselves under sandbox-exec with proto/nonet.sb (same pattern as midyd.py). --no-wrap is for diagnostics only.
if (process.env.MIDY_SANDBOX !== '1' && !argv.includes('--no-wrap') && fs.existsSync('/usr/bin/sandbox-exec')) {
  const { spawn } = require('child_process')
  // the sandbox matches resolved paths: a symlinked root (~/middy -> elsewhere, /tmp -> /private/tmp) must be passed resolved
  const realRun = () => { try { return fs.realpathSync(path.join(ROOT_WS, 'run')) } catch { return path.join(ROOT_WS, 'run') } }
  const early = (m) => { try { fs.appendFileSync(path.join(ROOT_WS, 'run', 'midy_app.log'), new Date().toISOString() + ' ' + m + '\n') } catch {} }
  try {
    // a GUI launch (Finder/Dock/open) has no usable stdio: inheriting closed fds throws and would kill the app silently
    // the child's own stdout/stderr go to run/midy_child.log (a GUI launch has no usable stdio; inheriting closed fds would throw)
    const outFd = fs.openSync(path.join(ROOT_WS, 'run', 'midy_child.log'), 'a')
    const child = spawn('/usr/bin/sandbox-exec', ['-f', path.join(ROOT_WS, 'proto', 'nonet.sb'), '-D', 'RUN=' + realRun(), process.execPath, ...process.argv.slice(1)],
      { env: { ...process.env, MIDY_SANDBOX: '1' }, detached: true, stdio: ['ignore', outFd, outFd] })
    child.unref()
    early('re-exec under sandbox-exec: child pid ' + child.pid + ' (parent ' + process.pid + ', packaged ' + app.isPackaged + ')')
    app.exit(0)
  } catch (e) { early('re-exec FAILED: ' + e + ' — continuing WITHOUT kernel sandbox (self-test will refuse Record)') }
}
if (FAKE_MIC) {                                              // Chromium fake capture device: the mic "hears" a wav, no TCC prompt
  app.commandLine.appendSwitch('use-fake-device-for-media-stream')
  app.commandLine.appendSwitch('use-file-for-fake-audio-capture', FAKE_MIC + '%noloop')
  app.commandLine.appendSwitch('no-sandbox')                // the audio service sandbox cannot read the wav (test runs only)
}
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling')
// Inside the kernel sandbox-exec profile Chromium's own seatbelt (GPU / network / utility helpers) cannot be applied a second
// time: the helpers exit 5 and the app dies (measured 27/09). The outer profile covers the whole process tree, so Chromium's
// per-process seatbelt is switched off there; contextIsolation, no nodeIntegration, CSP and webRequest stay.
if (process.env.MIDY_SANDBOX === '1') app.commandLine.appendSwitch('no-sandbox')

const DIST = path.join(__dirname, '..', 'dist')
const net_selftest = { done: false, tcp: null, dns: null, blocked: false, sandbox: process.env.MIDY_SANDBOX === '1' }
const LOG = path.join(ROOT, 'run', 'midy_app.log')
const log = (m) => { const line = new Date().toISOString() + ' ' + m + '\n'; try { fs.appendFileSync(LOG, line) } catch {} }
const SETTINGS_PATH = () => path.join(app.getPath('userData'), 'settings.json')
const DEFAULT_SETTINGS = { shortcut: DEFAULT_SHORTCUT /* Việc 16 */, micInput: true /* Lỗi 9: Middy's mic-input button, last choice kept */, language: 'English', space: 'default', screenCapture: false, micDeviceId: 'default', summarizer: 'claude' /* 05/10: 'claude' (Claude Code via MCP, no Gemma; the release default) | 'local' (Gemma after Stop) */,
  // UX 05/10 ("icon lù lù"): no floating button by default (the menu bar icon is the entry); the meeting overlay opens with each
  // meeting (overlay), see-through until hovered (overlayOpacity = background opacity), at the position it was dragged to (overlayPos)
  widget: false, overlay: true, overlayOpacity: 0.3, overlayPos: null }
let settings = { ...DEFAULT_SETTINGS }
try { settings = { ...settings, ...JSON.parse(fs.readFileSync(SETTINGS_PATH(), 'utf8')) } } catch {}
const DROPPED_SETTINGS = ['autoTranslate']                 // Lỗi 10: auto-translate removed ("không làm dịch"); purge the stored key
const saveSettings = () => { try { fs.mkdirSync(app.getPath('userData'), { recursive: true }); fs.writeFileSync(SETTINGS_PATH(), JSON.stringify(settings, null, 1)) } catch (e) { log('settings save failed ' + e) } }
if (DROPPED_SETTINGS.some((k) => k in settings)) { for (const k of DROPPED_SETTINGS) delete settings[k]; saveSettings() }

// ---- windows (frameless, transparent, always on top at screen-saver level, not resizable, 11 px radius) ------------
const SIZES = {                     // [w, h]
  toolbar: [88, 88], 'toolbar-expanded': [88, 254], 'meeting-overlay': [400, 560],
  'preview-window': [460, 624], settings: [480, 520], 'quit-warning': [320, 150], 'space-suggestion': [320, 180],
  library: [1280, 800],
}
const wins = {}
const net_blocked = { count: 0, last: null }

function makeWindow(name, opts = {}) {
  const [w, h] = SIZES[name]
  const framed = name === 'library'
  const bw = new BrowserWindow({
    width: w, height: h, show: false, frame: framed, transparent: !framed, resizable: false, minimizable: framed, maximizable: false,
    fullscreenable: false, hasShadow: framed, alwaysOnTop: !framed, skipTaskbar: !framed, title: 'Middy',
    type: framed ? undefined : 'panel', vibrancy: framed ? undefined : 'fullscreen-ui', visualEffectState: framed ? undefined : 'active' , roundedCorners: true,
    backgroundColor: framed ? '#1f2126' : '#00000000',
    webPreferences: { preload: path.join(__dirname, '..', 'preload', 'index.js'), contextIsolation: true, nodeIntegration: false, sandbox: false /* Chromium's own seatbelt cannot nest inside the kernel sandbox-exec profile: GPU/network helpers exit 5 (measured 27/09) */,
                      backgroundThrottling: false, spellcheck: false },
    ...opts,
  })
  // skipTransformProcessType: without it Electron flips the PROCESS to UIElementApplication on every call
  // (electron.d.ts VisibleOnAllWorkspacesOptions) => no Dock icon even though Info.plist has no LSUIElement (Lỗi 6, measured 28/09 lsappinfo)
  if (!framed) { bw.setAlwaysOnTop(true, 'screen-saver'); bw.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true }) }
  bw.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))                     // no popups (rnd M2 item 6)
  bw.webContents.on('will-navigate', (e) => e.preventDefault())
  bw.loadFile(path.join(DIST, 'index.html'), { hash: name })
  bw.on('closed', () => { delete wins[name]; log('window closed ' + name); if (['meeting-overlay', 'preview-window'].includes(name) && !['starting', 'recording', 'finishing'].includes(meeting.state)) showToolbar('closed ' + name) })
  if (name === 'meeting-overlay') {                                        // dragged by its top bar: remembered for the next meeting
    let t = null
    bw.on('moved', () => { clearTimeout(t); t = setTimeout(() => { if (bw.isDestroyed() || NO_SHOW) return; const [x, y] = bw.getPosition(); settings.overlayPos = { x, y }; saveSettings() }, 400) })
  }
  wins[name] = bw
  return bw
}

function place(name) {                                       // positions relative to the toolbar / screen
  const bw = wins[name]; if (!bw) return
  const area = screen.getPrimaryDisplay().workArea
  const tb = wins.toolbar ? wins.toolbar.getBounds() : { x: area.x + area.width - 88 - 16, y: area.y + area.height - 88 - 16, width: 88, height: 88 }
  const [w, h] = bw.getSize()
  let x, y
  const pos = name === 'meeting-overlay' && settings.overlayPos
  if (onSomeDisplay(pos, screen.getAllDisplays().map((d) => d.workArea))) { x = pos.x; y = pos.y }
  else if (name === 'toolbar') { x = area.x + area.width - w - 16; y = area.y + area.height - h - 16 }
  else if (['meeting-overlay', 'preview-window', 'settings'].includes(name)) { x = tb.x - w - 12; y = Math.max(area.y, Math.min(tb.y + tb.height - h, area.y + area.height - h)) }
  else if (['quit-warning', 'space-suggestion'].includes(name)) { x = area.x + area.width - w - 16; y = area.y + 16 }
  else { x = area.x + Math.round((area.width - w) / 2); y = area.y + Math.round((area.height - h) / 2) }
  if (NO_SHOW) { x = -3000; y = -3000 }                // test runs while the user uses the Mac: render off-screen
  bw.setPosition(Math.round(x), Math.round(y))
}

function open(name) {
  if (!wins[name]) makeWindow(name)
  const bw = wins[name]
  const reveal = () => { place(name); if (NO_SHOW) bw.showInactive(); else bw.show() }   // --no-show: never steal focus
  if (bw.webContents.isLoading()) bw.once('ready-to-show', reveal); else reveal()
  return bw
}
const close = (name) => { if (wins[name]) wins[name].close() }
// The toolbar is hidden while the overlay is up; it MUST come back whenever the meeting UI goes away, otherwise the app looks
// quit (only the tray icon is left) — bug 1, 27/09 22:52.
// UX 05/10: only when the floating button is wanted (Settings); otherwise the toolbar window stays hidden: it still runs the mic capture.
function showToolbar(why) { const t = wins.toolbar; if (!t || t.isDestroyed() || !settings.widget) return; if (!t.isVisible()) { place('toolbar'); t.show(); log('toolbar shown (' + why + ')') } }
const nameOf = (wc) => Object.keys(wins).find((k) => wins[k].webContents === wc)
const broadcast = (channel, data) => { for (const bw of Object.values(wins)) if (!bw.isDestroyed()) bw.webContents.send(channel, data) }

// ---- meeting state --------------------------------------------------------------------------------------------------------
const daemon = new Daemon(log)
let sysAudio = null
const meeting = { state: 'idle', id: null, title: 'Untitled Note', startedAt: 0, language: null, unsubscribe: null, stats: { mic_chunks: 0, sys_chunks: 0 } }

let meetingSeq = 0
async function startMeeting(o = {}) {
  if (meeting.state !== 'idle') return { ok: false, error: 'a meeting is already running' }
  if (!net_selftest.blocked && !argv.includes('--allow-open-network')) return { ok: false, error: 'network self-test not passed (sandbox missing)' }
  await daemon.start()
  meeting.language = o.language || settings.language
  const r = await daemon.request({ cmd: 'start', name: o.name || ('meeting ' + new Date().toISOString().slice(0, 16)), live: 'ui', language: meeting.language,
                                   space: o.space || settings.space, chunk_min: 10 /* rolling-diarization window (min), not "part notes" */, no_slides: !settings.screenCapture, no_llm: argv.includes('--no-llm-test'), summarizer: settings.summarizer })
  if (!r.ok) return r
  meeting.state = 'starting'; meeting.startedAt = Date.now(); meeting.readyAt = 0; meeting.title = 'Untitled Note'; meeting.prebuf = { 0: [], 1: [] }; meeting.paused = false; meeting.stats = { mic_chunks: 0, sys_chunks: 0 }
  const token = meeting.token = ++meetingSeq               // Lỗi 14: events are routed by meeting, see onEvent
  meeting.unsubscribe = await daemon.subscribe((ev) => onEvent(ev, token))
  meeting.psb = powerSaveBlocker.start('prevent-app-suspension')     // no App Nap / sleep while a meeting runs (an accessory app gets napped after ~30 s)
  sysAudio = new SystemAudio((chunk) => {
    meeting.stats.sys_chunks++; const now = Date.now(); if (!meeting.stats.sys_first) meeting.stats.sys_first = now; meeting.stats.sys_rate = +((meeting.stats.sys_chunks - 1) * 250 / Math.max(1, now - meeting.stats.sys_first)).toFixed(3)
    if (!meeting.paused) feedAudio(0, chunk)
    if (wins.toolbar && !wins.toolbar.isDestroyed()) wins.toolbar.webContents.send('system-audio', chunk)   // AEC3 far-end reference
  }, log, FAKE_SYSTEM)
  try { sysAudio.start() } catch (e) { log('system audio failed: ' + e); sysAudio = null }
  broadcast('meeting', publicState()); updateTray()
  if (settings.overlay !== false) open('meeting-overlay')
  if (wins.toolbar && !argv.includes('--keep-toolbar')) wins.toolbar.hide()
  if (DURATION) setTimeout(() => stopMeeting(), DURATION * 1000)
  const KILL = Number(flag('--test-kill-mic') || 0)          // blind acceptance of devicechange: stop the mic track mid-meeting
  if (KILL) setTimeout(() => { log('TEST kill mic track'); wins.toolbar?.webContents.send('capture-control', 'kill-track') }, KILL * 1000)
  meeting.watchdog = setInterval(() => {
    if (meeting.state !== 'recording' || meeting.paused) return
    const gap = Date.now() - (meeting.stats.mic_last || meeting.readyAt || Date.now())
    if (gap > 3000 && !meeting.micWarned) { meeting.micWarned = true; log('WARN mic silent ' + gap + ' ms'); broadcast('event', { type: 'warn', where: 'mic_silent', gap_ms: gap }); wins.toolbar?.webContents.send('capture-control', 'rebuild') }
    if (gap < 1000 && meeting.micWarned) { meeting.micWarned = false; broadcast('event', { type: 'warn', where: 'mic_back' }) }
  }, 1000)
  detector?.meetingStarted(); endDetect.reset()
  return { ok: true }
}

function onEvent(ev, token) {
  if (token !== meeting.token) return onBackgroundEvent(ev)   // Lỗi 14: a stopped meeting finishing in the daemon
  if (ev.type === 'transcript' && ev.source === 'system') endDetect.lastSystemAt = Date.now()   // last transcript entry of source "system"
  if (ev.type === 'ready') endDetect.lastSystemAt = Date.now()
  if (ev.type === 'ready') { setTimeout(updateTray, 0); meeting.state = 'recording'; meeting.readyAt = Date.now(); flushPrebuf(); meeting.id = ev.meeting_id || meeting.id; daemon.request({ cmd: 'status' }).then((s) => { if (s.ok) { meeting.id = s.status.meeting_id; broadcast('meeting', publicState()) } }) }
  if (ev.type === 'done') meeting.id = ev.meeting_id
  broadcast('event', ev)
  if (ev.type === 'done') endMeetingUi('meeting done')          // the daemon ended it by itself (file input / error)
}

// Lỗi 14 (29/09 18:45): Stop frees the app at once. The daemon finishes the old meeting in the background (last notes, MoM,
// diarization), saves it in the library and renames it after the MoM title; no Preview window. Record works right away.
function onBackgroundEvent(ev) {
  if (ev.type !== 'done') return
  finishing = Math.max(0, finishing - 1); updateTray()
  log('background meeting ' + ev.meeting_id + ' finished' + (ev.error ? ' with error: ' + ev.error : ''))
  broadcast('event', { ...ev, background: true })              // the library reloads its list
  if (AUTO_START && !SHOTS && !argv.includes('--test-flow')) setTimeout(() => { log('auto-run done; stats ' + JSON.stringify(meeting.stats) + ' aec ' + JSON.stringify(meeting.aec)); app.quit() }, 1500)
}

function endMeetingUi(why) {
  meeting.token = 0                                             // later events of this meeting are background events
  if (sysAudio) { sysAudio.stop(); sysAudio = null }
  clearInterval(meeting.watchdog); if (meeting.psb !== undefined) { powerSaveBlocker.stop(meeting.psb); meeting.psb = undefined }
  detector?.meetingEnded(); endDetect.reset(); setReminder(null)
  meeting.state = 'idle'
  broadcast('meeting', publicState()); close('meeting-overlay'); showToolbar(why); updateTray()
}

async function stopMeeting() {
  if (!['recording', 'starting'].includes(meeting.state)) return { ok: false, error: 'no meeting' }
  const r = await daemon.request({ cmd: 'stop' })
  if (r.ok) finishing++                                        // the menu bar shows "processing" until the daemon reports done
  log('stop -> ' + JSON.stringify(r) + '; app idle, meeting ' + meeting.id + ' finishes in the background')
  endMeetingUi('meeting stopped')
  return r
}

const publicState = () => ({ state: meeting.state, id: meeting.id, title: meeting.title, startedAt: meeting.startedAt, readyAt: meeting.readyAt || 0, language: meeting.language, space: settings.space, stats: meeting.stats, systemAudioMode: sysAudio ? sysAudio.mode : (FAKE_SYSTEM ? 'fake' : 'audiotee'), paused: !!meeting.paused })

// ---- meeting detection (Lỗi 7) — lite detector + notification + auto-end, see main/meeting_detector.js ---------------------
// UX 05/10: a small macOS notification (was a 300x100 window on top of everything). Click = Start recording; it goes away by itself;
// not clicked within 30 s (or closed) = dismissed: that app is suppressed until its mic has been released for 20 s. Test flags:
// --no-meeting-detect, --probe-script <file of probe JSON lines, replayed in order, last one repeats>, --probe-speed <n>.
const DETECTED_DISMISS_MS = 30e3
let detector = null, detected = null, detectedTimer = null, detectedNote = null
function probeBinary() {
  const p = app.isPackaged ? path.join(process.resourcesPath, 'native', 'meeting-probe') : path.join(__dirname, '..', 'build', 'native', 'meeting-probe')
  return fs.existsSync(p) ? p : null
}
function startMeetingDetection() {
  if (argv.includes('--no-meeting-detect')) { log('meeting-detect off (--no-meeting-detect)'); return }
  const script = flag('--probe-script')
  let probe
  if (script) { const lines = fs.readFileSync(script, 'utf8').split('\n').filter((l) => l.trim()); let i = 0; probe = async () => parseProbe(lines[Math.min(i++, lines.length - 1)]) }
  else { const bin = probeBinary(); if (!bin) { log('meeting-detect off: meeting-probe binary missing'); return } probe = () => runProbe(bin) }
  detector = createDetector({ probe, log, speed: Number(flag('--probe-speed') || 1), isRecording: () => ['starting', 'recording', 'finishing'].includes(meeting.state), onDetected: showMeetingDetected, onEvent: onDetectorEvent })
  detector.start()
}
function showMeetingDetected(m) {
  if (meeting.state !== 'idle') { log('meeting-detect notice skipped: a meeting is already running'); return }
  closeMeetingDetected(); detected = m
  if (!NO_SHOW && Notification.isSupported()) {
    detectedNote = new Notification({ title: m.appLabel + ' meeting detected', body: 'Click to start recording with Middy.', silent: true })
    detectedNote.on('click', () => acceptMeetingDetected('notification click'))
    detectedNote.on('close', () => { if (detected === m) dismissMeetingDetected('notification closed') })
    detectedNote.show()
  }
  detectedTimer = setTimeout(() => dismissMeetingDetected('timed out (30 s)'), DETECTED_DISMISS_MS)
  updateTray(); log('meeting-detect notice shown: ' + m.appLabel)
}
function closeMeetingDetected() { clearTimeout(detectedTimer); detectedTimer = null; detected = null; const n = detectedNote; detectedNote = null; if (n) n.close(); updateTray() }
function dismissMeetingDetected(why) { if (!detected) return; log('meeting-detect notice dismissed: ' + why); detector?.dismiss(detected.sourceId); closeMeetingDetected() }
async function acceptMeetingDetected(why) {
  log('meeting-detect Start recording (' + why + ', ' + (detected?.appLabel || '?') + ')'); closeMeetingDetected()
  const r = await startMeeting({}); log('meeting-detect start -> ' + JSON.stringify(r)); return r
}

// Auto-end: while recording, once the meeting app has released its mic (detector
// "meeting_ended", i.e. 2 polls = 20 s) AND no "system" transcript for 20 s -> "No activity — end meeting?" with a 20 s countdown,
// then the meeting ends exactly like End meeting (stop + open preview). "Keep recording"
// suppresses the prompt for 120 s.
const END_DETECTION_SYSTEM_AUDIO_QUIET_MS = 20e3, END_DETECTION_AUTO_END_COUNTDOWN_MS = 20e3, END_DETECTION_KEEP_RECORDING_SUPPRESS_MS = 120e3
const endDetect = { micReleasedAt: null, deadline: null, dismissedAt: null, lastSystemAt: Date.now(), reset() { this.micReleasedAt = null; this.deadline = null; this.dismissedAt = null; this.lastSystemAt = Date.now() } }
let reminder = null
function setReminder(r) { if (JSON.stringify(r) === JSON.stringify(reminder)) return; reminder = r; broadcast('reminder', r) }
function onDetectorEvent(e) {
  if (e.type === 'meeting_ended' && endDetect.micReleasedAt === null) { endDetect.micReleasedAt = Date.now(); log('meeting-detect ' + e.appLabel + ' released the mic') }
  if (e.type === 'meeting_started_candidate') { endDetect.micReleasedAt = null; endDetect.deadline = null }
}
setInterval(() => {                                                                              // ticks every 250 ms
  if (meeting.state !== 'recording' || meeting.paused) { if (reminder) setReminder(null); return }
  const now = Date.now()
  const met = endDetect.micReleasedAt !== null && now - endDetect.lastSystemAt >= END_DETECTION_SYSTEM_AUDIO_QUIET_MS
  if (!met) { endDetect.deadline = null; if (reminder) setReminder(null); return }
  if (endDetect.dismissedAt !== null && now - endDetect.dismissedAt < END_DETECTION_KEEP_RECORDING_SUPPRESS_MS) return
  if (!endDetect.deadline) {
    endDetect.deadline = now + END_DETECTION_AUTO_END_COUNTDOWN_MS
    log('meeting-detect auto-end countdown started (20 s)')
    if (!wins['meeting-overlay']?.isVisible()) open('meeting-overlay')                        // a minimized overlay is reopened
  }
  setReminder({ kind: 'end_meeting_no_activity', deadlineAt: endDetect.deadline })
  if (now < endDetect.deadline) return
  endDetect.deadline = null; setReminder(null)
  log('meeting-detect auto-end: meeting app left, no speech 20 s + countdown 20 s -> End meeting')
  stopMeeting()
}, 250)
ipcMain.handle('reminder:get', () => reminder)
ipcMain.handle('reminder:keep', () => { endDetect.deadline = null; endDetect.dismissedAt = Date.now(); endDetect.micReleasedAt = null; setReminder(null); log('meeting-detect auto-end: Keep recording'); return true })
ipcMain.handle('reminder:end', () => { endDetect.deadline = null; setReminder(null); log('meeting-detect auto-end: End meeting clicked'); return stopMeeting() })

// ---- IPC ----------------------------------------------------------------------------------------------------------------------
const { allowedDaemonRequest } = require('../preload/channels.js')
ipcMain.handle('daemon', async (_e, req) => {                 // renderer -> daemon: only the commands the UI uses (preload/channels.js)
  if (!allowedDaemonRequest(req)) return { ok: false, error: 'daemon command not allowed from the UI: ' + (req && req.cmd) }
  await daemon.start(); return daemon.request(req)
})
ipcMain.handle('meeting:start', (_e, o) => startMeeting(o || {}))
ipcMain.handle('meeting:stop', () => stopMeeting())
ipcMain.handle('meeting:ask', (_e, question) => daemon.request({ cmd: 'ask', question }))   // Việc 17: answer streams back as ask_delta / ask_done events

// ---- Việc 16: system-wide Start/Stop shortcut (main/shortcut.js) ---------------------------------------------------------------
let shortcut = null
function initShortcut() {
  shortcut = createShortcut({
    globalShortcut, log, isMeeting: () => ['starting', 'recording'].includes(meeting.state), stop: () => stopMeeting(),
    // same entry as the toolbar's Record; when Record is refused, show Settings where the reason is, like the toolbar does
    start: () => startMeeting({}).then((r) => { if (!r.ok) { log('shortcut start refused: ' + r.error); open('settings') } }),
  })
  broadcast('shortcut', shortcut.register(settings.shortcut || DEFAULT_SHORTCUT))
}
function setShortcut(acc) {
  const r = shortcut.register(acc)
  if (r.ok && r.accelerator === acc) { settings.shortcut = acc; saveSettings() }
  const st = { ...shortcut.state(), error: r.error, rejected: r.rejected }
  broadcast('shortcut', st); return st
}
ipcMain.handle('shortcut:get', () => shortcut ? shortcut.state() : null)
ipcMain.handle('shortcut:record', (_e, key) => { const a = accelFromKey(key); return a.error ? { ...shortcut.state(), error: a.error } : setShortcut(a.accelerator) })
ipcMain.handle('shortcut:reset', () => setShortcut(DEFAULT_SHORTCUT))
// while the Settings field records a new combination, the current one must not fire (pressing ⌃⌥R there would start a meeting)
ipcMain.handle('shortcut:suspend', (_e, on) => { if (on) globalShortcut.unregisterAll(); else shortcut.register(shortcut.state().accelerator); return shortcut.state() })
ipcMain.handle('meeting:state', () => publicState())
ipcMain.handle('meeting:title', (_e, t) => { meeting.title = t; broadcast('meeting', publicState()); if (meeting.id) daemon.request({ cmd: 'set_name', meeting_id: meeting.id, name: t }); return true })
ipcMain.on('mic-audio', (_e, buf) => { meeting.stats.mic_chunks++; const now = Date.now(); if (!meeting.stats.mic_first) meeting.stats.mic_first = now; meeting.stats.mic_last = now; meeting.stats.mic_rate = +((meeting.stats.mic_chunks - 1) * 250 / Math.max(1, now - meeting.stats.mic_first)).toFixed(3); if (meeting.paused) return
  // Lỗi 9: mic input off -> the chunk is replaced by digital silence of the same length HERE, before the daemon: no mic sample
  // reaches ASR or any file, while both streams stay time-aligned (dropping chunks would trip the stream-drift guard)
  const eff = micInputOn()
  if (eff !== meeting.micEff) { meeting.micEff = eff; log('mic input effective ' + (eff ? 'ON' : 'OFF') + ' (button ' + (settings.micInput !== false ? 'on' : 'off') + ', meeting app mic ' + micGate.appMicOn + ')') }
  if (eff) feedAudio(1, Buffer.from(buf)); else { meeting.stats.mic_zeroed = (meeting.stats.mic_zeroed || 0) + 1; feedAudio(1, Buffer.alloc(buf.byteLength)) }
})
// recording starts the moment Record is pressed; the local models need 6-40 s to load, so chunks of both streams are kept
// (cap PREBUF_MAX per stream) and flushed in arrival order when the daemon reports ready (rnd M2 item 3).
const PREBUF_MAX = 4 * 120                                    // 120 s per stream (~7.7 MB for both)
function feedAudio(stream, chunk) {
  const t = Date.now() / 1000                                 // capture wall time of the chunk end (daemon keeps latencies honest after a flush)
  if (meeting.state === 'starting' && meeting.prebuf) { const q = meeting.prebuf[stream]; q.push([chunk, t]); if (q.length > PREBUF_MAX) { q.shift(); meeting.stats.prebuf_dropped = (meeting.stats.prebuf_dropped || 0) + 1 } return }
  if (meeting.state === 'recording') daemon.sendAudio(stream, chunk, t)
}
function flushPrebuf() {
  const pb = meeting.prebuf; meeting.prebuf = null
  if (!pb) return
  const n = Math.max(pb[0].length, pb[1].length)
  for (let i = 0; i < n; i++) { if (pb[0][i]) daemon.sendAudio(0, pb[0][i][0], pb[0][i][1]); if (pb[1][i]) daemon.sendAudio(1, pb[1][i][0], pb[1][i][1]) }
  meeting.stats.prebuf_flushed = { system: pb[0].length, mic: pb[1].length }
  log('prebuffer flushed ' + JSON.stringify(meeting.stats.prebuf_flushed))
}
ipcMain.handle('window:open', (_e, name) => { open(name); return true })
ipcMain.handle('window:close', (e, name) => { close(name || nameOf(e.sender)); return true })
ipcMain.handle('daemon-log', (_e, o) => { if (o && o.aec) meeting.aec = o.aec; if (o && o.ctx) meeting.ctx = o.ctx; if (o && o.capture) { meeting.stats.capture_events = (meeting.stats.capture_events || []).concat([o.capture]).slice(-20); broadcast('event', { captureType: o.capture.type, ...o.capture, type: 'capture' }) } log('renderer ' + JSON.stringify(o).slice(0, 400)); return true })
ipcMain.handle('toolbar:capture', (_e, c) => { meeting.paused = c === 'pause'; setTimeout(updateTray, 0); if (wins.toolbar) wins.toolbar.webContents.send('capture-control', c); return true })
// overlay X: hidden until the next meeting or "Show overlay" in the menu bar (the recording goes on)
ipcMain.handle('window:hide', (e) => { BrowserWindow.fromWebContents(e.sender)?.hide(); log('window hidden ' + nameOf(e.sender)); showToolbar('window hidden'); updateTray(); return true })
ipcMain.handle('window:size', (e, key) => { const bw = BrowserWindow.fromWebContents(e.sender); const [w, h] = SIZES[key]; const b = bw.getBounds(); bw.setBounds({ x: b.x, y: b.y + b.height - h, width: w, height: h }); return true })
ipcMain.handle('window:move', (e, d) => { const bw = BrowserWindow.fromWebContents(e.sender); const b = bw.getBounds(); bw.setPosition(b.x + d.dx, b.y + d.dy); return true })
ipcMain.handle('toolbar:show', () => { if (wins.toolbar) { wins.toolbar.show() } return true })
ipcMain.handle('settings:get', () => settings)
// Lỗi 10: recognition language picked on the meeting overlay. Remembered for the next meeting; during a meeting the daemon
// applies it to utterances that start after the switch (proto/core.py set_language), never to text already recognised.
ipcMain.handle('meeting:language', async (_e, lang) => {
  settings = { ...settings, language: lang }; saveSettings(); broadcast('settings', settings)
  if (!['starting', 'recording'].includes(meeting.state)) { log('language ' + lang + ' (next meeting)'); return { ok: true, applied: 'next meeting' } }
  const r = await daemon.request({ cmd: 'set_language', language: lang })
  if (r.ok) { meeting.language = lang; broadcast('meeting', publicState()) }
  log('language -> ' + lang + ' during meeting: ' + JSON.stringify(r)); return r
})
ipcMain.handle('settings:set', (_e, patch) => { if ('micInput' in patch && patch.micInput !== settings.micInput) log('mic input ' + (patch.micInput ? 'ON' : 'OFF') + ' (button)' + (meeting.state !== 'idle' ? ' during meeting, mic chunks so far ' + meeting.stats.mic_chunks : '')); settings = { ...settings, ...patch }; saveSettings(); broadcast('settings', settings); if ('widget' in patch && meeting.state === 'idle') { if (settings.widget) showToolbar('setting'); else wins.toolbar?.hide() }
  updateTray(); return settings })
// Lỗi 9b: while a meeting records and the meeting app is known (detector), watch that app's mic control through the read-only
// helper; its state feeds micGate.appMicOn (case 3: unmuted there -> always take the mic). Anything unclear -> null -> button.
let micWatch = null, micWatchFor = null
const micStateBinary = () => { const p = app.isPackaged ? path.join(process.resourcesPath, 'native', 'mic-state') : path.join(__dirname, '..', 'build', 'native', 'mic-state'); return fs.existsSync(p) ? p : null }
setInterval(() => {
  const want = ['starting', 'recording', 'finishing'].includes(meeting.state) ? (detector?.currentBundleId || null) : null
  if (want === micWatchFor) return
  if (micWatch) { micWatch.stop(); micWatch = null }
  micWatchFor = want; micGate.appMicOn = null; broadcast('mic-state', null)
  if (!want) return
  const bin = micStateBinary(); if (!bin) { log('mic-state helper missing'); return }
  log('mic-state watching ' + want)
  micWatch = createMicStateWatcher({ binary: bin, bundleId: want, log, onState: (st) => { micGate.appMicOn = st.appMicOn; log('mic-state ' + want + ' ' + JSON.stringify(st)); broadcast('mic-state', { ...st, app: labelFor(want) }) } })
}, 1000)
ipcMain.handle('mic-state:get', () => (micWatchFor ? { appMicOn: micGate.appMicOn, app: labelFor(micWatchFor) } : null))
// Accessibility permission (Settings). Only a click asks macOS (prompt); status checks never prompt.
ipcMain.handle('ax:status', () => ({ trusted: process.platform === 'darwin' ? systemPreferences.isTrustedAccessibilityClient(false) : false }))
ipcMain.handle('ax:request', () => { log('accessibility permission requested (Settings click)'); return { trusted: systemPreferences.isTrustedAccessibilityClient(true) } })
ipcMain.handle('ax:open-settings', () => shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'))
ipcMain.handle('net:status', () => ({ ...net_selftest, blockedRequests: net_blocked.count, last: net_blocked.last }))
ipcMain.handle('output-device', () => FORCE_AEC ? { label: 'forced', isHeadphoneOutput: false } : FORCE_HP ? { label: 'forced', isHeadphoneOutput: true } : outputDevice())
// Việc 13: the meeting's MoM in the company Word form (.docx, written by the daemon with the local Gemma). Same dialog / --export-dir.
ipcMain.handle('export:docx', async (e, { meetingId, title }) => {
  const name = ((title || 'meeting') + ' - MoM').replace(/[\\/:*?"<>|]+/g, '_') + '.docx'
  let file = flag('--export-dir') ? path.join(flag('--export-dir'), name) : null
  if (!file) {
    const r = await dialog.showSaveDialog(BrowserWindow.fromWebContents(e.sender), { defaultPath: path.join(app.getPath('documents'), name), filters: [{ name: 'Word', extensions: ['docx'] }] })
    if (r.canceled) return { ok: false, canceled: true }
    file = r.filePath
  }
  fs.mkdirSync(path.dirname(file), { recursive: true })
  await daemon.start()
  const r = await daemon.requestOnce({ cmd: 'export_docx', meeting_id: meetingId, path: file, summarizer: settings.summarizer })
  log('export docx ' + JSON.stringify({ ...r, path: undefined })); return r
})
// Export a note or the original transcript as .md / .txt (file mode 600). --export-dir <dir> writes there without the dialog (tests).
ipcMain.handle('export:file', async (e, { title, text, ext }) => {
  ext = ext === 'txt' ? 'txt' : 'md'
  const name = (title || 'note').replace(/[\\/:*?"<>|]+/g, '_') + '.' + ext
  let file = flag('--export-dir') ? path.join(flag('--export-dir'), name) : null
  if (!file) {
    const bw = BrowserWindow.fromWebContents(e.sender)
    const r = await dialog.showSaveDialog(bw, { defaultPath: path.join(app.getPath('documents'), name), filters: [{ name: ext === 'md' ? 'Markdown' : 'Text', extensions: [ext] }] })
    if (r.canceled) return { ok: false }
    file = r.filePath
  }
  fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, text, { mode: 0o600 }); fs.chmodSync(file, 0o600)
  log('exported ' + ext + ' ' + text.length + ' chars -> ' + file); return { ok: true, path: file }
})

ipcMain.handle('shot', async (e, name) => {                 // regression screenshots
  if (!SHOTS) return false
  const bw = wins[name] || BrowserWindow.fromWebContents(e.sender)
  const img = await bw.webContents.capturePage(); fs.mkdirSync(SHOTS, { recursive: true })
  fs.writeFileSync(path.join(SHOTS, name + '.png'), img.toPNG()); return true
})
ipcMain.handle('quit', () => { app.quit(); return true })

// AEC3 is bypassed when the default output is a headphone. macOS default output via system_profiler.
function outputDevice() {
  return new Promise((resolve) => {
    execFile('/usr/sbin/system_profiler', ['SPAudioDataType', '-json'], { timeout: 8000 }, (err, out) => {
      if (err) return resolve({ label: null, isHeadphoneOutput: false, error: String(err) })
      try {
        const items = JSON.parse(out).SPAudioDataType?.[0]?._items || []
        const dev = items.find((d) => d.coreaudio_default_audio_output_device === 'spaudio_yes') || {}
        const label = dev._name || '', transport = dev.coreaudio_device_transport || ''
        const isHeadphoneOutput = /headphone|airpods|earbuds|beats|buds/i.test(label) || /bluetooth|usb/i.test(transport) && /head|pod|bud/i.test(label)
        resolve({ label, transport, isHeadphoneOutput })
      } catch (e) { resolve({ label: null, isHeadphoneOutput: false, error: String(e) }) }
    })
  })
}

// ---- tray -------------------------------------------------------------------------------------------------------------------------
function makeTray() {
  // template image (black on transparent, @1x 18 px + @2x 36 px): macOS recolours it for light/dark menu bars
  const icon = nativeImage.createFromPath(flag('--tray-icon') || path.join(__dirname, '..', 'renderer', 'icons', 'trayTemplate.png'))
  icon.setTemplateImage(true)
  const tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon)
  // getBounds() right after new Tray() is ALWAYS {x:0, h:0} (macOS 26 places the item asynchronously, measured 28/09 with a bare
  // Electron tray) -> judge placement a few seconds later. height 0 then = Control Center parked the item off-screen.
  log('tray created icon_empty=' + icon.isEmpty() + ' size=' + JSON.stringify(icon.getSize()))
  const trayCheck = (sec) => setTimeout(() => { if (tray.isDestroyed()) return; const b = tray.getBounds(); log('tray t+' + sec + 's bounds=' + JSON.stringify(b) + (b.height === 0 ? ' PARKED (not shown in the menu bar)' : ' shown')) }, sec * 1000)
  trayCheck(3); trayCheck(15)
  // Lỗi 8 watch: every 30 s, log only when the placement changes (parked <-> shown, or moved), so a late show/hide leaves numbers
  let lastTray = ''
  setInterval(() => { if (tray.isDestroyed()) return; const b = tray.getBounds(); const k = JSON.stringify(b); if (k !== lastTray) { lastTray = k; log('tray watch bounds=' + k + (b.height === 0 ? ' PARKED' : ' shown')) } }, 30000)
  const shot = flag('--shot-tray')                            // review evidence: screenshot of the menu bar around our icon
  if (shot) setTimeout(() => { const b = tray.getBounds(); const d = screen.getPrimaryDisplay(); execFile('/usr/sbin/screencapture', ['-x', '-R', `${Math.max(d.bounds.x, b.x - 150)},${b.y},${b.width + 300},${b.height}`, shot], () => log('tray shot ' + shot)) }, 1500)
  return tray
}

// UX 05/10: the menu bar icon is the way in. Its title shows the state (main/ux.js trayState); the menu starts/stops, brings the hidden overlay back, opens the library and settings.
let finishing = 0                                             // stopped meetings the daemon is still finishing
function trayMenu() {
  const live = ['starting', 'recording'].includes(meeting.state)
  const ov = wins['meeting-overlay']
  return [
    live ? { label: 'Stop meeting', click: () => stopMeeting() } : { label: detected ? 'Start recording (' + detected.appLabel + ')' : 'Start meeting', click: () => (detected ? acceptMeetingDetected('menu') : startMeeting({}).then((r) => { if (!r.ok) open('settings') })) },
    { label: 'Show overlay', enabled: live, visible: live, click: () => open('meeting-overlay') },
    { type: 'separator' },
    { label: 'Open Middy', click: () => open('library') },
    { label: 'Settings', click: () => open('settings') },
    { label: 'Floating button', type: 'checkbox', checked: !!settings.widget, click: (i) => { settings = { ...settings, widget: i.checked }; saveSettings(); broadcast('settings', settings); if (i.checked) showToolbar('menu'); else wins.toolbar?.hide() } },
    { label: 'Middy v' + app.getVersion() + (ov && !ov.isDestroyed() && !ov.isVisible() && live ? ' · overlay hidden' : ''), enabled: false },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]
}
function updateTray() {
  if (!tray || tray.isDestroyed()) return
  const st = trayState(meeting, finishing, detected); tray.setTitle(st.title); tray.setToolTip(st.tip)
  tray.setContextMenu(Menu.buildFromTemplate(trayMenu()))
}

// ---- network guard (design 1.9): every request that is not file:/blob:/data: is cancelled and counted -------------------------------
function installNetworkGuard() {
  session.defaultSession.webRequest.onBeforeRequest((d, cb) => {
    const ok = /^(file|blob|data|devtools|chrome-extension):/.test(d.url)
    if (!ok) { net_blocked.count++; net_blocked.last = d.url.slice(0, 120); log('BLOCKED ' + d.url.slice(0, 120)) }
    cb({ cancel: !ok })
  })
  session.defaultSession.setPermissionRequestHandler((_wc, permission, cb) => cb(['media', 'audioCapture'].includes(permission)))
}

// --net-probe: prove the OS-level block of the packaged app (App Sandbox without network entitlements): TCP + DNS must fail.
function netProbe() {
  const net = require('net'), dns = require('dns')
  const finish = () => { if (net_selftest.tcp !== null && net_selftest.dns !== null) { net_selftest.done = true; net_selftest.blocked = net_selftest.tcp !== 'OPEN' && net_selftest.dns !== 'RESOLVED'; log('NETPROBE tcp:' + net_selftest.tcp + ' dns:' + net_selftest.dns + ' => blocked:' + net_selftest.blocked); broadcast('net', { ...net_selftest, blockedRequests: net_blocked.count }) } }
  const s = net.connect({ host: '1.1.1.1', port: 443, timeout: 4000 })
  s.on('connect', () => { net_selftest.tcp = 'OPEN'; s.destroy(); finish() })
  s.on('error', (e) => { net_selftest.tcp = 'blocked:' + e.code; finish() }); s.on('timeout', () => { net_selftest.tcp = 'timeout'; s.destroy(); finish() })
  dns.lookup('apple.com', (e) => { net_selftest.dns = e ? 'blocked:' + e.code : 'RESOLVED'; finish() })
}
// Dock click: focus what is visible, else bring the toolbar back
app.on('activate', () => { if (NO_SHOW) return; const vis = Object.values(wins).filter((w) => !w.isDestroyed() && w.isVisible()); if (vis.length) vis[vis.length - 1].show(); else if (settings.widget) showToolbar('dock activate'); else open('library') })
// --test-expand: drive the hover state without a mouse (shots + frame counts land in the log)
function scriptedExpand() {
  const tb = () => wins.toolbar?.webContents
  const at0 = Number(flag('--expand-at') || 0) * 1000                     // shift the whole script, e.g. into a running fake-mic meeting
  setTimeout(() => shot('toolbar').then(() => fs.renameSync(path.join(SHOTS, 'toolbar.png'), path.join(SHOTS, 'toolbar-initial.png'))).catch(() => {}), at0 + 1200)
  setTimeout(() => tb()?.send('ui', 'tb-expand'), at0 + 2000)
  setTimeout(() => shot('toolbar').then(() => fs.renameSync(path.join(SHOTS, 'toolbar.png'), path.join(SHOTS, 'toolbar-expanded.png'))).catch(() => {}), at0 + 3200)
  setTimeout(() => tb()?.send('ui', 'tb-collapse'), at0 + 4000)
  setTimeout(() => shot('toolbar').catch(() => {}), at0 + 5200)
}
// --test-detect (with --probe-script): Lỗi 7 end to end — accept the detection like a notification click, shoot the auto-end
// prompt when it appears, then let the countdown end the meeting and quit.
function scriptedDetect() {
  let accepted = false, reminderDone = false
  const t = setInterval(async () => {
    if (!accepted && detected) { accepted = true; log('TEST accept detection'); acceptMeetingDetected('test') }
    if (!reminderDone && reminder) {
      reminderDone = true; await new Promise((r) => setTimeout(r, 1500))
      await shot('meeting-overlay'); fs.renameSync(path.join(SHOTS, 'meeting-overlay.png'), path.join(SHOTS, 'auto-end-reminder.png'))
    }
    if (reminderDone && meeting.state === 'idle') { clearInterval(t); log('TEST detect flow done'); setTimeout(() => app.quit(), 3000) }
  }, 300)
}
let tray = null   // module-level reference keeps the Tray from being garbage-collected
app.whenReady().then(async () => {
  initShortcut()
  // Lỗi 15: start the daemon now; it loads + warms up one ASR and one Gemma worker, so Record starts in < 1 s instead of 6-12 s
  if (!argv.includes('--no-prewarm')) daemon.start().then(() => log('daemon started at launch (prewarm)'), (e) => log('daemon prewarm failed: ' + e))
  netProbe()                                                 // startup self-test; Record stays disabled until it reports "blocked"
  installNetworkGuard()
  // Dock stays visible (a regular, Dock-visible app); dev runs show our icon too
  if (process.platform === 'darwin' && !app.isPackaged) { try { app.dock.setIcon(path.join(__dirname, '..', 'build', 'icons', 'icon_1024.png')) } catch (e) { log('dock icon ' + e.message) } }
  tray = NO_SHOW ? null : makeTray(); updateTray()
  const quitAfter = Number(flag('--quit-after') || 0)
  if (quitAfter) setTimeout(() => app.quit(), quitAfter * 1000)
  const tb = makeWindow('toolbar'); place('toolbar'); tb.once('ready-to-show', () => { if (!NO_SHOW && settings.widget) tb.show() })   // hidden unless the floating button is on; --no-show: tests
  if (AUTO_START) tb.webContents.once('did-finish-load', () => setTimeout(() => startMeeting({ language: LANG || undefined }).then((r) => log('auto-start ' + JSON.stringify(r))), 1500))
  if (AUTO_START && SHOTS) scriptedShots()
  if (argv.includes('--test-flow')) scriptedFlow()
  if (argv.includes('--test-expand')) scriptedExpand()
  startMeetingDetection()
  if (argv.includes('--test-detect')) scriptedDetect()
})

// Bug-1 regression: after `done`, drive the Preview like the user did (Save -> Done, then Save -> Open in library) and check the
// app is still alive with the toolbar visible.
async function scriptedFlow() {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms))
  const untilPreview = () => new Promise((res) => { const t = setInterval(() => { if (wins['preview-window'] && meeting.state === 'idle') { clearInterval(t); res() } }, 300) })
  await untilPreview(); await wait(2500)
  const step = (what) => log('FLOW ' + what + ' | toolbar visible ' + (wins.toolbar && wins.toolbar.isVisible()) + ' | windows ' + Object.keys(wins).join(',') + ' | state ' + meeting.state)
  step('preview open')
  wins['preview-window'].webContents.send('ui', 'save'); await wait(2000); step('after Save')
  wins['preview-window'].webContents.send('ui', 'done'); await wait(1500); step('after Done')
  open('preview-window'); await wait(2000); wins['preview-window'].webContents.send('ui', 'save'); await wait(1500)
  wins['preview-window'].webContents.send('ui', 'open-library'); await wait(2500); step('after Open in library')
  await wait(5000); step('5 s later, still alive')
  const r = await daemon.request({ cmd: 'meeting', meeting_id: meeting.id })
  log('FLOW notes in db ' + JSON.stringify((r.notes || []).map((n) => n.kind + n.idx)) + ' name set ' + !!(r.meeting && r.meeting.name))
  // bug 3: export the original transcript (.md and .txt) from the library, then from the preview
  wins.library?.webContents.send('ui', 'select:last'); await wait(1500)
  wins.library?.webContents.send('ui', 'export-transcript:md'); await wait(1200)
  wins.library?.webContents.send('ui', 'export-transcript:txt'); await wait(1200)
  open('preview-window'); await wait(2500); wins['preview-window']?.webContents.send('ui', 'export-transcript:txt'); await wait(1500); close('preview-window'); await wait(800)
  if (flag('--export-dir')) for (const f of fs.readdirSync(flag('--export-dir'))) { const t = fs.readFileSync(path.join(flag('--export-dir'), f), 'utf8'); log('FLOW export ' + f + ' mode ' + (fs.statSync(path.join(flag('--export-dir'), f)).mode & 0o777).toString(8) + ' lines ' + t.split('\n').length + ' stamped ' + (t.match(/\[\d\d:\d\d:\d\d\] [^:]+:/g) || []).length) }
  // bug 2: delete from the library with the in-app confirmation
  wins.library?.webContents.send('ui', 'select:last'); await wait(1500)
  wins.library?.webContents.send('ui', 'delete'); await wait(800)
  wins.library?.webContents.send('ui', 'confirm-delete'); await wait(2500)
  const after = await daemon.request({ cmd: 'meeting', meeting_id: meeting.id })
  log('FLOW after delete: meeting row ' + JSON.stringify(after.meeting) + ' notes ' + (after.notes || []).length + ' segments ' + (after.segments || []).length + ' run dir exists ' + fs.existsSync(path.join(ROOT, 'run', 'meeting ' + new Date(meeting.startedAt).toISOString().slice(0, 16))))
  step('after delete')
  log('FLOW DONE'); app.quit()
}

// Regression: shoot every window with real (public B1) data, then quit. Timeline in seconds after launch.
async function shot(name) { const bw = wins[name]; if (!bw || bw.isDestroyed()) return; const img = await bw.webContents.capturePage(); fs.mkdirSync(SHOTS, { recursive: true }); fs.writeFileSync(path.join(SHOTS, name + '.png'), img.toPNG()); log('shot ' + name) }
async function scriptedShots() {
  const at = (s, fn) => setTimeout(fn, s * 1000)
  at(3, () => shot('toolbar'))
  at(Math.max(20, DURATION * 0.6), () => shot('meeting-overlay'))
  at(Math.max(25, DURATION * 0.7), () => { wins['meeting-overlay']?.webContents.send('ui', 'tab:notes') })
  at(Math.max(28, DURATION * 0.75), () => shot('meeting-overlay').then(() => fs.renameSync(path.join(SHOTS, 'meeting-overlay.png'), path.join(SHOTS, 'meeting-overlay-notes.png'))))
  at(Math.max(29, DURATION * 0.76), () => { wins['meeting-overlay']?.webContents.send('ui', 'tab:transcript') })
  at(Math.max(30, DURATION * 0.8), () => { wins['meeting-overlay']?.webContents.send('ui', 'confirm'); setTimeout(() => shot('meeting-overlay').then(() => fs.renameSync(path.join(SHOTS, 'meeting-overlay.png'), path.join(SHOTS, 'meeting-overlay-end-confirm.png'))), 800); setTimeout(() => wins['meeting-overlay']?.webContents.send('ui', 'confirm:off'), 1600) })
  at(Math.max(33, DURATION * 0.85), () => shot('meeting-overlay'))
  const afterDone = () => new Promise((res) => { const t = setInterval(() => { if (meeting.state === 'idle' && wins['preview-window']) { clearInterval(t); res() } }, 500) })
  at(DURATION + 2, async () => {
    await afterDone(); await new Promise((r) => setTimeout(r, 2500)); await shot('preview-window')
    open('settings'); await new Promise((r) => setTimeout(r, 2000)); await shot('settings')
    open('library'); await new Promise((r) => setTimeout(r, 3000)); wins.library?.webContents.send('ui', 'select:last'); await new Promise((r) => setTimeout(r, 2500)); await shot('library')
    open('quit-warning'); await new Promise((r) => setTimeout(r, 1500)); await shot('quit-warning')
    log('shots done; stats ' + JSON.stringify(meeting.stats) + ' aec ' + JSON.stringify(meeting.aec) + ' net ' + JSON.stringify(net_blocked))
    app.quit()
  })
}
app.on('window-all-closed', () => {})
app.on('will-quit', () => { globalShortcut.unregisterAll(); log('will-quit') })
app.on('window-all-closed', () => log('window-all-closed (ignored: tray app)'))
let quitting = false
app.on('before-quit', async (e) => {
  log('before-quit (source: ' + (new Error().stack || '').split('\n').slice(2, 4).join(' | ').trim() + ')')
  if (quitting) return
  e.preventDefault(); quitting = true
  try { if (['recording', 'starting'].includes(meeting.state)) await stopMeeting() } catch {}
  try { await daemon.quit() } catch {}
  app.exit(0)
})
