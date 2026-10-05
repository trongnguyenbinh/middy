// Clean mic at the context rate (48 kHz) -> 16 kHz s16le chunks of 250 ms, decimated INSIDE the same AudioContext
// (no second AudioContext / cross-context MediaStream). Low-pass = 31-tap windowed sinc at 0.9 * 8 kHz, then keep every 3rd sample.
class PcmDownProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const o = options.processorOptions || {}
    this.outRate = o.outputSampleRate || 16000
    this.ratio = Math.round(sampleRate / this.outRate)                 // 3 for 48 k
    this.n = o.bufferSize || Math.round(this.outRate * 0.25)
    this.buf = new Int16Array(this.n); this.fill = 0; this.running = false; this.chunks = 0
    const taps = 31, fc = 0.9 / (2 * this.ratio); this.h = new Float32Array(taps)
    let sum = 0
    for (let i = 0; i < taps; i++) { const m = i - (taps - 1) / 2; const sinc = m === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * m) / (Math.PI * m); const w = 0.54 - 0.46 * Math.cos((2 * Math.PI * i) / (taps - 1)); this.h[i] = sinc * w; sum += this.h[i] }
    for (let i = 0; i < taps; i++) this.h[i] /= sum
    this.hist = new Float32Array(taps); this.phase = 0
    this.port.onmessage = (e) => { const t = e.data?.type; if (t === 'start' || t === 'resume') this.running = true; else if (t === 'pause' || t === 'stop') this.running = false }
  }
  process(inputs, outputs) {
    // Keep the physical output stream alive: Chromium suspends a sink that stays exactly silent for 30 s (SilentSinkSuspender)
    // and then drives the context from a fake timer clock, which ran at ~0.4x here (measured 27/09). -120 dBFS is inaudible.
    const o = outputs[0]?.[0]
    if (o) { const v = this.tick ? 1e-6 : -1e-6; this.tick = !this.tick; o.fill(v) }
    const x = inputs[0]?.[0]
    if (!x || !this.running) return true
    const taps = this.h.length, hist = this.hist
    for (let i = 0; i < x.length; i++) {
      hist.copyWithin(1, 0, taps - 1); hist[0] = x[i]
      if (this.phase === 0) {
        let y = 0
        for (let k = 0; k < taps; k++) y += this.h[k] * hist[k]
        const v = Math.max(-1, Math.min(1, y))
        this.buf[this.fill++] = v < 0 ? v * 32768 : v * 32767
        if (this.fill === this.n) { this.port.postMessage({ type: 'pcm_data', data: this.buf.buffer.slice(0) }); this.fill = 0; this.chunks++ }
      }
      this.phase = (this.phase + 1) % this.ratio
    }
    return true
  }
}
registerProcessor('pcm-down-processor', PcmDownProcessor)
