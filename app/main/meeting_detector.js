// Meeting detector (Lỗi 7) — a "lite" detection approach, used on macOS when system audio comes from
// audiotee. Middy always uses audiotee.
// Detection only ASKS (popup "… meeting detected" +
// "Start recording"); recording starts only when the user clicks.

// States: idle (10 s polls) -> watching (known meeting app frontmost, 3 s) -> confirming (its mic is on, 3 s; 2 polls if its
// speaker was seen, else 5) -> active (10 s). In active: the app's mic off for 2 polls -> "meeting_ended" (once) if we record,
// otherwise the detection just expires.
const { execFile } = require('child_process')

const MEETING_APPS = [                                                     //  (Windows-only keywords kept, harmless)
  { primaryBundleId: 'us.zoom.xos', keywords: ['zoom'], label: 'Zoom' },
  { primaryBundleId: 'com.tinyspeck.slackmacgap', keywords: ['slack'], label: 'Slack' },
  { primaryBundleId: 'com.microsoft.teams2', keywords: ['microsoft.teams', 'ms-teams.exe', 'teams.exe', 'msteams'], label: 'Microsoft Teams' },
  { primaryBundleId: 'com.microsoft.teams', keywords: ['microsoft.teams', 'ms-teams.exe', 'teams.exe', 'msteams'], label: 'Microsoft Teams' },
  { primaryBundleId: 'com.cisco.webex.meetings', keywords: ['webex'], label: 'Webex' },
  { primaryBundleId: 'Cisco-Systems.Spark', keywords: ['cisco-systems.spark'], label: 'Webex' },
  { primaryBundleId: 'com.larksuite.lark', keywords: ['lark'], label: 'Lark' },
  { primaryBundleId: 'com.bytedance.lark', keywords: ['lark'], label: 'Lark' },
  { primaryBundleId: 'com.bytedance.feishu', keywords: ['feishu'], label: 'Feishu' },
  { primaryBundleId: 'com.larksuite.feishu', keywords: ['feishu'], label: 'Feishu' },
  { primaryBundleId: 'com.hnc.Discord', keywords: ['discord'], label: 'Discord' },
  { primaryBundleId: 'com.skype.skype', keywords: ['skype'], label: 'Skype' },
  { primaryBundleId: 'com.apple.FaceTime', keywords: ['facetime'], label: 'FaceTime' },
  { primaryBundleId: 'net.whatsapp.WhatsApp', keywords: ['whatsapp'], label: 'WhatsApp' },
  { primaryBundleId: 'com.tencent.xinWeChat', keywords: ['xinwechat', 'wechat.exe', 'weixin.exe'], label: 'WeChat' },
  { primaryBundleId: 'com.tencent.WeWorkMac', keywords: ['wework', 'wxwork.exe', 'wecom.exe'], label: 'WeCom' },
  { primaryBundleId: 'com.tencent.wemeet', keywords: ['wemeet'], label: 'Tencent Meeting' },
  { primaryBundleId: 'com.tencent.meeting', keywords: ['tencent.meeting'], label: 'Tencent Meeting' },
  { primaryBundleId: 'com.alibaba.DingTalkMac', keywords: ['dingtalk'], label: 'DingTalk' },
]
const MEETING_APP_BUNDLE_IDS = MEETING_APPS.map((a) => a.primaryBundleId)
const POLL_INTERVAL_IDLE_MS = 10e3, POLL_INTERVAL_WATCHING_MS = 3e3, POLL_INTERVAL_ACTIVE_MS = 10e3
const FAST_CONFIRM_POLLS = 2, SLOW_CONFIRM_POLLS = 5, END_CONFIRM_POLLS = 2, DISMISS_EXPIRY_MS = 20e3

const findByBundleId = (id) => { if (!id) return undefined; const l = id.toLowerCase(); return MEETING_APPS.find((a) => a.keywords.some((k) => l.includes(k))) }
const isMeetingAppBundleId = (id) => !!findByBundleId(id)
const labelFor = (id, fallback = 'Meeting') => (id && findByBundleId(id)?.label) || fallback
const belongs = (procId, meetingId) => { const app = findByBundleId(meetingId); return !!(procId && app && app.keywords.some((k) => procId.toLowerCase().includes(k))) }

function emptyResult() { return { mic: 'error', speaker: 'error', frontmostBundleId: '', frontmostName: '', processes: [] } }
function parse(stdout) {
  try {
    const o = JSON.parse(stdout)
    const st = (v) => (v === 'active' || v === 'inactive' || v === 'error' ? v : 'error')
    const processes = (Array.isArray(o.processes) ? o.processes : []).filter((p) => p && typeof p.bundleId === 'string' && p.bundleId)
      .map((p) => ({ pid: typeof p.pid === 'number' ? p.pid : -1, bundleId: p.bundleId, mic: p.mic === true, speaker: p.speaker === true }))
    return { mic: st(o.mic), speaker: st(o.speaker), frontmostBundleId: typeof o.frontmostBundleId === 'string' ? o.frontmostBundleId : '', frontmostName: typeof o.frontmostName === 'string' ? o.frontmostName : '', processes }
  } catch { return emptyResult() }
}

// opts: { probe: async () => probeResult, now, isRecording: () => bool, onDetected(meeting), onEvent(event), log, speed }
function createDetector(opts) {
  const now = opts.now || Date.now
  const log = opts.log || (() => {})
  const speed = opts.speed || 1                                            // tests only: divide the poll intervals
  const dismissed = new Set(), notified = new Set(), micLastActive = new Map()
  let state = 'idle', candidate = null, currentBundleId = null, endPolls = 0, lastSeenName = null, timer = null, running = false
  let endEmitted = false, legacyMicReleasedSince = null, lastSig = null

  const perProcess = (p) => p.processes.length > 0
  const bundleMic = (p, id) => !!id && p.processes.some((x) => x.mic && belongs(x.bundleId, id))
  const bundleSpk = (p, id) => !!id && p.processes.some((x) => x.speaker && belongs(x.bundleId, id))
  const isBundleMicActive = (p, id) => (perProcess(p) ? bundleMic(p, id) : p.mic === 'active')
  const isBundleSpeakerActive = (p, id) => (perProcess(p) ? bundleSpk(p, id) : p.speaker === 'active')
  const to = (next) => { if (state !== next) { log('meeting-detect state ' + state + ' -> ' + next); state = next } }
  const suppressed = (id) => dismissed.has(id) || notified.has(id)
  const releasedLongEnough = (p, id) => {
    if (!perProcess(p)) return legacyMicReleasedSince !== null && now() - legacyMicReleasedSince >= DISMISS_EXPIRY_MS
    if (bundleMic(p, id)) return false
    const last = micLastActive.get(id)
    return last !== undefined && now() - last >= DISMISS_EXPIRY_MS
  }
  const cleanup = () => { if (currentBundleId) { dismissed.delete(currentBundleId); notified.delete(currentBundleId); micLastActive.delete(currentBundleId) } currentBundleId = null; endPolls = 0; candidate = null; endEmitted = false }
  const startCandidate = (id, spk) => {
    if (suppressed(id)) { log('meeting-detect candidate suppressed ' + id + ' (' + (dismissed.has(id) ? 'dismissed' : 'already_notified') + ')'); return }
    log('meeting-detect candidate started ' + id + ' speaker=' + spk)
    candidate = { bundleId: id, confirmPolls: 1, speakerSeen: spk }
    to('confirming')
  }
  const emitDetected = (p, c) => {
    const meeting = { sourceId: 'bundle:' + c.bundleId, windowName: p.frontmostName || labelFor(c.bundleId), appLabel: labelFor(c.bundleId, p.frontmostName || 'Meeting') }
    log('meeting-detect DETECTED ' + c.bundleId + ' speakerSeen=' + c.speakerSeen + ' polls=' + c.confirmPolls)
    notified.add(c.bundleId); currentBundleId = c.bundleId; candidate = null; endPolls = 0
    to('active')
    opts.onEvent?.({ type: 'meeting_started_candidate', sourceId: meeting.sourceId, appLabel: meeting.appLabel, emittedAtMs: now() })
    opts.onDetected?.(meeting)
  }

  function pollOnce(p) {
    const front = p.frontmostBundleId, known = isMeetingAppBundleId(front)
    if (p.frontmostName) lastSeenName = p.frontmostName
    if (perProcess(p)) {
      const watch = new Set(dismissed); if (candidate) watch.add(candidate.bundleId); if (currentBundleId) watch.add(currentBundleId)
      for (const id of watch) if (bundleMic(p, id)) micLastActive.set(id, now())
    }
    if (p.mic === 'active') legacyMicReleasedSince = null; else if (legacyMicReleasedSince === null) legacyMicReleasedSince = now()
    const sig = JSON.stringify({ state, mic: p.mic, front, known, withMic: MEETING_APP_BUNDLE_IDS.filter((id) => bundleMic(p, id)) })
    if (sig !== lastSig) { lastSig = sig; log('meeting-detect poll ' + sig) }   // bundle ids + flags only, no content
    for (const id of Array.from(dismissed)) if (releasedLongEnough(p, id)) { log('meeting-detect dismissed expired ' + id); dismissed.delete(id); micLastActive.delete(id) }
    switch (state) {
      case 'idle':
        if (!known) return
        if (isBundleMicActive(p, front)) startCandidate(front, isBundleSpeakerActive(p, front)); else to('watching')
        return
      case 'watching':
        if (!known) { to('idle'); return }
        if (isBundleMicActive(p, front)) startCandidate(front, isBundleSpeakerActive(p, front))
        return
      case 'confirming': {
        if (!candidate) { to('idle'); return }
        if (!isBundleMicActive(p, candidate.bundleId)) { log('meeting-detect candidate cancelled (mic dropped) ' + candidate.bundleId); candidate = null; to(known ? 'watching' : 'idle'); return }
        candidate.confirmPolls += 1
        if (isBundleSpeakerActive(p, candidate.bundleId)) candidate.speakerSeen = true
        if (candidate.confirmPolls < (candidate.speakerSeen ? FAST_CONFIRM_POLLS : SLOW_CONFIRM_POLLS)) return
        if (suppressed(candidate.bundleId)) { candidate = null; to('idle'); return }
        emitDetected(p, candidate)
        return
      }
      case 'active': {
        const recording = !!opts.isRecording?.()
        if (!currentBundleId && !recording) { to('idle'); return }
        if (!currentBundleId && recording) {                               // manual start: find which meeting app holds the mic
          currentBundleId = MEETING_APP_BUNDLE_IDS.find((id) => bundleMic(p, id)) || null
          if (currentBundleId) log('meeting-detect manual-start app identified ' + currentBundleId)
          else { endPolls = 0; return }
        }
        if (isBundleMicActive(p, currentBundleId)) { endPolls = 0; endEmitted = false; return }
        endPolls += 1
        if (endPolls < END_CONFIRM_POLLS || endEmitted) return
        if (recording) {
          log('meeting-detect meeting_ended (mic released ' + END_CONFIRM_POLLS * POLL_INTERVAL_ACTIVE_MS / 1000 + ' s) ' + currentBundleId)
          opts.onEvent?.({ type: 'meeting_ended', sourceId: 'bundle:' + currentBundleId, windowName: lastSeenName || undefined, appLabel: labelFor(currentBundleId), emittedAtMs: now(), reason: 'mic_inactive' })
          endEmitted = true; endPolls = 0
          return
        }
        log('meeting-detect detected call expired without record start ' + currentBundleId)
        cleanup(); to('idle')
      }
    }
  }
  const interval = () => (state === 'watching' || state === 'confirming' ? POLL_INTERVAL_WATCHING_MS : state === 'active' ? POLL_INTERVAL_ACTIVE_MS : POLL_INTERVAL_IDLE_MS) / speed
  const schedule = () => { if (!running) return; clearTimeout(timer); timer = setTimeout(tick, interval()) }
  async function tick() { try { pollOnce(await opts.probe()) } catch (e) { log('meeting-detect poll error ' + e.message) } schedule() }

  return {
    pollOnce, get state() { return state }, get currentBundleId() { return currentBundleId },
    start() { if (running) return; running = true; state = 'idle'; lastSig = null; log('meeting-detect started (idle, ' + POLL_INTERVAL_IDLE_MS / 1000 / speed + ' s cadence)'); tick() },
    stop() { running = false; clearTimeout(timer); timer = null; candidate = null; endPolls = 0; currentBundleId = null; state = 'idle'; notified.clear(); micLastActive.clear() },
    meetingStarted() { if (!running) return; to('active'); endPolls = 0; schedule() },
    meetingEnded() { cleanup(); to('idle'); schedule() },
    dismiss(sourceId) {                                                    // : popup closed or timed out
      if (!sourceId || !sourceId.startsWith('bundle:')) return
      const id = sourceId.slice(7)
      dismissed.add(id); notified.delete(id); micLastActive.set(id, now()); log('meeting-detect dismissed ' + id)
      if (state === 'active' && currentBundleId === id) { currentBundleId = null; candidate = null; endPolls = 0; to('idle'); schedule() }
    },
  }
}

// One probe run of the Swift helper (tools/meeting_probe.swift), 3 s timeout.
function runProbe(binary) {
  return new Promise((resolve) => execFile(binary, [], { timeout: 3e3 }, (err, stdout) => resolve(err ? emptyResult() : parse(String(stdout).trim()))))
}

// Lỗi 9 (28/09): 1/ app muted + Middy mic off -> nothing from the mic; 2/ app muted + Middy mic on -> mic;
// 3/ app unmuted -> always mic. `appMicOn` must be true only on a TRUSTWORTHY "unmuted" signal. macOS has none without new
// permissions (SDK: CoreAudio/AVAudioApplication mute state is per calling process; other processes only expose IsRunningInput,
// and a muted Teams may keep its input stream open), so today appMicOn stays null and the button alone decides.
function micInputEffective({ appMicOn, button }) { return appMicOn === true || button !== false }

module.exports = { micInputEffective, createDetector, runProbe, parse, MEETING_APPS, isMeetingAppBundleId, labelFor,
  CONST: { POLL_INTERVAL_IDLE_MS, POLL_INTERVAL_WATCHING_MS, POLL_INTERVAL_ACTIVE_MS, FAST_CONFIRM_POLLS, SLOW_CONFIRM_POLLS, END_CONFIRM_POLLS, DISMISS_EXPIRY_MS } }
