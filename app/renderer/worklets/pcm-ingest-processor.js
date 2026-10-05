// System audio (audiotee, s16le 16 kHz, 250 ms chunks from the main process) -> Float32 samples in the 48 kHz AEC context.
// Ring buffer of ~1 s; underrun = silence, overrun = drop oldest (same strategy as the reference app's ingest processor, own code).
class PcmIngestProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const o = options.processorOptions || {}
    this.inRate = o.inputSampleRate || 16000
    this.ratio = sampleRate / this.inRate                 // e.g. 3 for 16 k -> 48 k, linear interpolation
    this.cap = Math.round(sampleRate * (o.capacitySeconds || 1.0))
    this.buf = new Float32Array(this.cap); this.r = 0; this.w = 0; this.size = 0
    this.underruns = 0; this.overruns = 0; this.lastReport = 0
    this.prev = 0
    this.port.onmessage = (e) => { if (e.data?.type === 'pcm') this.ingest(e.data.buffer) }
  }
  push(v) {
    if (this.size === this.cap) { this.r = (this.r + 1) % this.cap; this.size--; this.overruns++ }
    this.buf[this.w] = v; this.w = (this.w + 1) % this.cap; this.size++
  }
  ingest(ab) {
    const x = new Int16Array(ab); const n = x.length
    for (let i = 0; i < n; i++) {
      const cur = x[i] / 32768
      for (let k = 1; k <= this.ratio; k++) this.push(this.prev + (cur - this.prev) * (k / this.ratio))
      this.prev = cur
    }
  }
  process(_inputs, outputs) {
    const out = outputs[0]?.[0]; if (!out) return true
    for (let i = 0; i < out.length; i++) {
      if (this.size > 0) { out[i] = this.buf[this.r]; this.r = (this.r + 1) % this.cap; this.size-- } else { out[i] = 0; this.underruns++ }
    }
    if (currentTime - this.lastReport > 2) { this.lastReport = currentTime; this.port.postMessage({ type: 'diagnostics', underruns: this.underruns, overruns: this.overruns, level: this.size }); this.underruns = 0; this.overruns = 0 }
    return true
  }
}
registerProcessor('pcm-ingest-processor', PcmIngestProcessor)
