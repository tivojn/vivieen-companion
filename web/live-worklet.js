/* Live-talk microphone capture: float32 -> PCM16, batched to ~100ms
   before posting, so the main thread ships fewer, larger frames. Served
   as a real file because AudioWorklet modules from blob: URLs are blocked
   by the app's CSP (the "Unable to load a worklet's module" bubble,
   2026-08-02). */
class VivLiveCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = [];
    this.len = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const out = new Int16Array(ch.length);
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      out[i] = s < 0 ? s * 32768 : s * 32767;
    }
    this.buf.push(out);
    this.len += out.length;
    if (this.len >= 2400) {
      const all = new Int16Array(this.len);
      let offset = 0;
      for (const b of this.buf) { all.set(b, offset); offset += b.length; }
      this.port.postMessage(all.buffer, [all.buffer]);
      this.buf = [];
      this.len = 0;
    }
    return true;
  }
}
registerProcessor('viv-live-capture', VivLiveCapture);
