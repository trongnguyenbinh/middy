// WebRTC AEC3 in an AudioWorklet (48 kHz): input 0 = mic (near end, delayed by the caller when the far end arrives late),
// input 1 = system audio (far end / render reference), output 0 = clean mic. The webrtcaec3 glue (BSD-3-Clause) is
// prepended to this file by the caller (AudioWorkletGlobalScope cannot import); it defines `WebRtcAec3`.
// Headphone mode = pass-through (no echo path), as the reference app does.
function b64(str) {                                   // no atob in AudioWorkletGlobalScope
  const T = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', L = new Uint8Array(256)
  for (let i = 0; i < 64; i++) L[T.charCodeAt(i)] = i
  const n = str.length, pad = str.endsWith('==') ? 2 : str.endsWith('=') ? 1 : 0, out = new Uint8Array((n * 3) / 4 - pad)
  for (let i = 0, j = 0; i < n; i += 4) {
    const v = (L[str.charCodeAt(i)] << 18) | (L[str.charCodeAt(i + 1)] << 12) | (L[str.charCodeAt(i + 2)] << 6) | L[str.charCodeAt(i + 3)]
    out[j++] = v >> 16; if (j < out.length) out[j++] = (v >> 8) & 255; if (j < out.length) out[j++] = v & 255
  }
  return out
}
class EchoCancelProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.aec = null; this.status = 'init'; this.err = null; this.headphone = false
    this.renderIn = [null]; this.captureIn = [null]; this.out = [new Float32Array(2048)]
    this.ring = new Float32Array(sampleRate); this.rr = 0; this.rw = 0; this.rsize = 0
    this.underruns = 0; this.frames = 0; this.busyMs = 0; this.busyFrames = 0; this.pMic = 0; this.pRef = 0; this.pOut = 0; this.lastReport = 0
    this.port.onmessage = (e) => { if (e.data?.type === 'setHeadphoneMode') { this.headphone = !!e.data.enabled; this.report('headphone') } }
    try {
      WebRtcAec3({ wasmBinary: b64(__AEC_WASM_B64) }).then((mod) => { this.aec = new mod.AEC3(sampleRate, 1, 1); this.status = 'ready'; this.report('ready') })
        .catch((e) => { this.status = 'error'; this.err = String(e); this.report('init-error') })
    } catch (e) { this.status = 'error'; this.err = String(e) + (globalThis.__aecGlueError ? ' | glue: ' + String(globalThis.__aecGlueError) : ''); this.report('init-error') }
  }
  report(reason) { this.port.postMessage({ type: 'status', reason, status: this.status, error: this.err, headphone: this.headphone, sampleRate }) }
  pushRing(a, n) { for (let i = 0; i < n; i++) { if (this.rsize === this.ring.length) { this.rr = (this.rr + 1) % this.ring.length; this.rsize-- } this.ring[this.rw] = a[i]; this.rw = (this.rw + 1) % this.ring.length; this.rsize++ } }
  popRing(out) { if (this.rsize < out.length) return false; for (let i = 0; i < out.length; i++) { out[i] = this.ring[this.rr]; this.rr = (this.rr + 1) % this.ring.length } this.rsize -= out.length; return true }
  power(a) { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * a[i]; return s / a.length }
  process(inputs, outputs) {
    const mic = inputs[0]?.[0], ref = inputs[1]?.[0], out = outputs[0]?.[0]
    if (!out) return true
    if (!mic) { out.fill(0); return true }
    if (this.headphone || this.status !== 'ready' || !ref) { out.set(mic) }
    else {
      const t0 = Date.now()
      try {
        this.renderIn[0] = ref; this.aec.analyze(this.renderIn)
        this.captureIn[0] = mic
        const n = this.aec.processSize(this.captureIn)
        if (n > this.out[0].length) this.out[0] = new Float32Array(n)
        this.aec.process(this.out, this.captureIn)
        if (n > 0) this.pushRing(this.out[0], n)
        if (!this.popRing(out)) { out.set(mic); this.underruns++ }
      } catch (e) { this.status = 'error'; this.err = String(e); out.set(mic); this.report('process-error') }
      this.busyMs += Date.now() - t0; this.busyFrames++
    }
    this.frames++
    this.pMic = 0.98 * this.pMic + 0.02 * this.power(mic); this.pRef = 0.98 * this.pRef + 0.02 * (ref ? this.power(ref) : 0); this.pOut = 0.98 * this.pOut + 0.02 * this.power(out)
    if (currentTime - this.lastReport > 1) {
      this.lastReport = currentTime
      this.port.postMessage({ type: 'diagnostics', status: this.status, headphone: this.headphone, micDb: 10 * Math.log10(this.pMic + 1e-12), refDb: 10 * Math.log10(this.pRef + 1e-12), outDb: 10 * Math.log10(this.pOut + 1e-12), underruns: this.underruns, frames: this.frames, aecMsPerFrame: this.busyFrames ? +(this.busyMs / this.busyFrames).toFixed(3) : null, aecFrames: this.busyFrames })
    }
    return true
  }
}
registerProcessor('echo-cancel-processor', EchoCancelProcessor)
