// Mic capture: one getUserMedia stream shared by LiveKit (teammates hear you) and an
// AudioWorklet that produces 16 kHz PCM16 frames for the backend's transcription.
//
// Prefer an AudioContext running at 16 kHz so the browser's own (filtered, high-quality)
// resampler does the conversion. If the browser refuses, run at the native rate and
// downsample in the worklet by averaging each window, which avoids most aliasing.

export const FRAME_MS = 20;

const WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0; this.count = 0; this.pos = 0;
    this.buf = new Int16Array(320); this.n = 0; this.level = 0;
  }
  push(v) {
    const s = Math.max(-1, Math.min(1, v));
    this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this.n === this.buf.length) {
      this.port.postMessage({ pcm: this.buf.buffer, level: this.level }, [this.buf.buffer]);
      this.buf = new Int16Array(320); this.n = 0; this.level = 0;
    }
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const x = ch[i];
      if (Math.abs(x) > this.level) this.level = Math.abs(x);
      if (this.ratio === 1) { this.push(x); continue; }
      // Average every input sample that falls in this output sample's window.
      this.acc += x; this.count++; this.pos += 1;
      if (this.pos >= this.ratio) { this.push(this.acc / this.count); this.acc = 0; this.count = 0; this.pos -= this.ratio; }
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);
`;

export interface Mic {
  stream: MediaStream;
  track: MediaStreamTrack;
  ctx: AudioContext;
  stop: () => void;
}

export async function startMic(onFrame: (pcm: ArrayBuffer, level: number) => void): Promise<Mic> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  let ctx: AudioContext;
  let source: MediaStreamAudioSourceNode;
  try {
    ctx = new AudioContext({ sampleRate: 16000 });
    source = ctx.createMediaStreamSource(stream);
  } catch {
    ctx = new AudioContext();
    source = ctx.createMediaStreamSource(stream);
  }
  const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
  await ctx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  const node = new AudioWorkletNode(ctx, "pcm16");
  node.port.onmessage = (e) => onFrame(e.data.pcm, e.data.level);
  source.connect(node);
  if (ctx.state === "suspended") await ctx.resume();
  const track = stream.getAudioTracks()[0];
  return {
    stream,
    track,
    ctx,
    stop: () => {
      node.disconnect();
      source.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      ctx.close();
    },
  };
}
