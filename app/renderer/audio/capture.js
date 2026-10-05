// Mic capture in the renderer: getUserMedia with default constraints -> 48 kHz AudioContext ->
// [DelayNode 0.58 s when the far end comes through audiotee] -> AEC3 worklet (far end = system audio pushed by main) ->
// decimator worklet 48 k -> 16 k (same context) -> 250 ms s16le chunks -> main -> daemon (stream 1).
// Device changes: `devicechange` debounced 500 ms; default output changed => headphone mode re-evaluated;
// the mic in use gone (or its track ended) => the whole path is rebuilt on the first available mic.
const MIC_DELAY_AUDIOTEE_S = 0.58        // audiotee's IPC/ingest path arrives about 580 ms behind the mic on macOS
const HEADPHONE_RE = /airpod|headphone|headset|earbud|earphone|耳机|耳麦/i

export async function defaultOutputIsHeadphone() {
  try {
    const devs = await navigator.mediaDevices.enumerateDevices()
    const outs = devs.filter((d) => d.kind === 'audiooutput')
    const def = outs.find((d) => d.deviceId === 'default') || outs[0]
    if (def && def.label) return { label: def.label, isHeadphoneOutput: HEADPHONE_RE.test(def.label), source: 'enumerateDevices' }
  } catch {}
  const o = await window.midy.invoke('output-device')                 // labels empty (no permission yet): main asks system_profiler
  return { ...o, source: 'system_profiler' }
}

export async function startCapture({ systemAudioMode, headphone, onDiag, onStatus, onEvent, deviceId }) {
  const cap = { stopped: false, chunks: 0, rebuilds: 0, headphone: !!headphone, deviceId: deviceId || 'default', systemAudioMode, paused: false, rebuilding: false }
  let g = null                                                         // the current audio graph
  let debounce = null

  async function build(devId) {
    const constraints = { audio: devId && devId !== 'default' ? { deviceId: { exact: devId } } : true }
    const mic = await navigator.mediaDevices.getUserMedia(constraints)
    const track = mic.getAudioTracks()[0]
    // latencyHint 'playback': bigger device buffers so the render thread survives the CPU load of the local models
    const ctx = new AudioContext({ sampleRate: 48000, latencyHint: 'playback' })
    if (ctx.state === 'suspended') await ctx.resume()
    await ctx.audioWorklet.addModule('worklets/aec-bundle.js')          // AEC3 glue + processor, concatenated at build time
    await ctx.audioWorklet.addModule('worklets/pcm-ingest-processor.js')
    await ctx.audioWorklet.addModule('worklets/pcm-down-processor.js')
    const micSrc = ctx.createMediaStreamSource(mic)
    // the 0.58 s mic delay applies whenever the far end comes through audiotee; with headphones the AEC is
    // passthrough anyway, so the delay is applied in both cases here too (report L18)
    const delay = systemAudioMode === 'audiotee' ? ctx.createDelay(1.0) : null
    if (delay) delay.delayTime.value = MIC_DELAY_AUDIOTEE_S
    const ingest = new AudioWorkletNode(ctx, 'pcm-ingest-processor', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: { inputSampleRate: 16000 } })
    const aec = new AudioWorkletNode(ctx, 'echo-cancel-processor', { numberOfInputs: 2, numberOfOutputs: 1, outputChannelCount: [1] })
    const diag = { aec: null, ingest: null }
    aec.port.onmessage = (e) => { if (e.data?.type === 'diagnostics') { diag.aec = e.data; onDiag?.(diag) } else if (e.data?.type === 'status') onStatus?.(e.data) }
    ingest.port.onmessage = (e) => { if (e.data?.type === 'diagnostics') { diag.ingest = e.data; onDiag?.(diag) } }
    aec.port.postMessage({ type: 'setHeadphoneMode', enabled: cap.headphone })
    if (delay) { micSrc.connect(delay); delay.connect(aec, 0, 0) } else micSrc.connect(aec, 0, 0)
    ingest.connect(aec, 0, 1)
    const pcm = new AudioWorkletNode(ctx, 'pcm-down-processor', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: { outputSampleRate: 16000, bufferSize: 4000 } })
    pcm.port.onmessage = (e) => { if (e.data?.type === 'pcm_data') { cap.chunks++; window.midy.sendMic(e.data.data) } }
    // the decimator's own output is a +-1e-6 (-120 dBFS) keep-alive signal, not audio: it reaches the device unmuted so
    // Chromium never classifies the sink as silent (see pcm-down-processor.js)
    aec.connect(pcm); pcm.connect(ctx.destination)
    pcm.port.postMessage({ type: cap.paused ? 'stop' : 'start' })
    const t0 = performance.now(), c0 = ctx.currentTime
    const tick = setInterval(() => onDiag?.({ ctx: { state: ctx.state, rate: +((ctx.currentTime - c0) / ((performance.now() - t0) / 1000)).toFixed(3), currentTime: +ctx.currentTime.toFixed(1), hidden: document.hidden, chunks: cap.chunks, rebuilds: cap.rebuilds, device: track.label } }), 5000)
    const offSys = window.midy.on('system-audio', (buf) => ingest.port.postMessage({ type: 'pcm', buffer: buf.buffer ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf }))
    track.onended = () => { onEvent?.({ type: 'mic-ended', label: track.label }); rebuild('track-ended') }
    return { mic, track, ctx, aec, pcm, teardown: async () => { clearInterval(tick); offSys(); try { pcm.port.postMessage({ type: 'stop' }) } catch {} track.onended = null; mic.getTracks().forEach((t) => t.stop()); try { await ctx.close() } catch {} } }
  }

  async function rebuild(reason) {
    if (cap.stopped || cap.rebuilding) return
    cap.rebuilding = true
    try {
      const old = g; g = null
      if (old) await old.teardown()
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput')
      const wanted = devs.find((d) => d.deviceId === cap.deviceId) ? cap.deviceId : (devs[0]?.deviceId || 'default')
      g = await build(wanted)
      cap.rebuilds++
      onEvent?.({ type: 'mic-rebuilt', reason, device: g.track.label, n: cap.rebuilds })
    } catch (e) { onEvent?.({ type: 'mic-rebuild-failed', reason, error: String(e) }) }
    cap.rebuilding = false
  }

  function onDeviceChange() {                                             // debounce 500 ms
    clearTimeout(debounce)
    debounce = setTimeout(async () => {
      if (cap.stopped) return
      const out = await defaultOutputIsHeadphone()
      if (out.isHeadphoneOutput !== cap.headphone) { cap.headphone = out.isHeadphoneOutput; g?.aec.port.postMessage({ type: 'setHeadphoneMode', enabled: cap.headphone }); onEvent?.({ type: 'output-changed', label: out.label, headphone: cap.headphone }) }
      const ins = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput')
      const alive = g && g.track.readyState === 'live'
      const stillThere = cap.deviceId === 'default' || ins.some((d) => d.deviceId === cap.deviceId)
      if (!alive || !stillThere) rebuild(!alive ? 'track-not-live' : 'mic-removed')
    }, 500)
  }
  navigator.mediaDevices.addEventListener('devicechange', onDeviceChange)
  g = await build(cap.deviceId)
  return {
    pause: () => { cap.paused = true; g?.pcm.port.postMessage({ type: 'pause' }) },
    resume: () => { cap.paused = false; g?.pcm.port.postMessage({ type: 'resume' }) },
    setHeadphoneMode: (on) => { cap.headphone = !!on; g?.aec.port.postMessage({ type: 'setHeadphoneMode', enabled: !!on }) },
    rebuild,
    killTrackForTest: () => { const t = g?.track; if (t) { t.stop(); if (t.onended) t.onended() } },   // blind acceptance of the devicechange path
    chunks: () => cap.chunks,
    stop: async () => { cap.stopped = true; clearTimeout(debounce); navigator.mediaDevices.removeEventListener('devicechange', onDeviceChange); const old = g; g = null; if (old) await old.teardown() },
  }
}
