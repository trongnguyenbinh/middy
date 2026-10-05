// Clean mic (16 kHz context) -> s16le chunks of `bufferSize` samples (250 ms), posted to the page (16 kHz / 250 ms).
class PcmProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.n = (options.processorOptions || {}).bufferSize || 4000
    this.buf = new Int16Array(this.n); this.fill = 0; this.running = false; this.chunks = 0
    this.port.onmessage = (e) => { const t = e.data?.type; if (t === 'start' || t === 'resume') this.running = true; else if (t === 'pause' || t === 'stop') this.running = false }
  }
  process(inputs) {
    const x = inputs[0]?.[0]
    if (!x || !this.running) return true
    for (let i = 0; i < x.length; i++) {
      const v = Math.max(-1, Math.min(1, x[i]))
      this.buf[this.fill++] = v < 0 ? v * 32768 : v * 32767
      if (this.fill === this.n) { this.port.postMessage({ type: 'pcm_data', data: this.buf.buffer.slice(0) }); this.fill = 0; this.chunks++ }
    }
    return true
  }
}
registerProcessor('pcm-processor', PcmProcessor)
