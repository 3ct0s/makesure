// Pure protocol helpers shared by the viewer page (and its Node test).

export const HEADER_SIZE = 21;
export const CODEC_H264 = 1;
export const CODEC_JPEG = 2;
const MAGIC_0 = 0x4d; // "M"
const MAGIC_1 = 0x53; // "S"
const VERSION = 1;

// Parse one binary video message. Returns null when it is not a valid frame.
export function parseFrame(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength <= HEADER_SIZE) return null;
  const view = new DataView(buffer);
  if (view.getUint8(0) !== MAGIC_0 || view.getUint8(1) !== MAGIC_1) return null;
  if (view.getUint8(2) !== VERSION) return null;
  const codec = view.getUint8(3);
  if (codec !== CODEC_H264 && codec !== CODEC_JPEG) return null;
  return {
    codec,
    keyframe: (view.getUint8(4) & 1) === 1,
    seq: view.getUint32(5, true),
    captureTsMs: Number(view.getBigUint64(9, true)),
    width: view.getUint16(17, true),
    height: view.getUint16(19, true),
    payload: new Uint8Array(buffer, HEADER_SIZE),
  };
}

// NAL unit types present in an Annex B access unit.
export function nalTypes(bytes) {
  const types = [];
  for (let i = 0; i + 3 < bytes.length; i++) {
    if (bytes[i] === 0 && bytes[i + 1] === 0) {
      if (bytes[i + 2] === 1) {
        types.push(bytes[i + 3] & 0x1f);
        i += 3;
      } else if (bytes[i + 2] === 0 && bytes[i + 3] === 1 && i + 4 < bytes.length) {
        types.push(bytes[i + 4] & 0x1f);
        i += 4;
      }
    }
  }
  return types;
}

// WebCodecs codec string ("avc1.PPCCLL") read from the SPS of a keyframe.
export function codecFromAnnexB(bytes) {
  for (let i = 0; i + 7 < bytes.length; i++) {
    if (bytes[i] !== 0 || bytes[i + 1] !== 0) continue;
    let start = -1;
    if (bytes[i + 2] === 1) start = i + 3;
    else if (bytes[i + 2] === 0 && bytes[i + 3] === 1) start = i + 4;
    if (start < 0 || start + 3 >= bytes.length) continue;
    if ((bytes[start] & 0x1f) === 7) {
      const hex = (b) => b.toString(16).padStart(2, "0").toUpperCase();
      return "avc1." + hex(bytes[start + 1]) + hex(bytes[start + 2]) + hex(bytes[start + 3]);
    }
  }
  return null;
}

// NTP-style offset estimate: remote clock minus local clock, from the
// lowest-round-trip sample in a sliding window.
export class ClockSync {
  constructor(window = 16) {
    this.window = window;
    this.samples = [];
  }
  addSample(t0, t1, t2, t3) {
    const rtt = t3 - t0 - (t2 - t1);
    if (rtt < 0) return;
    this.samples.push({ rtt, offset: (t1 - t0 + (t2 - t3)) / 2 });
    if (this.samples.length > this.window) this.samples.shift();
  }
  get ready() {
    return this.samples.length > 0;
  }
  best() {
    let best = null;
    for (const s of this.samples) if (best === null || s.rtt < best.rtt) best = s;
    return best;
  }
  get offsetMs() {
    const b = this.best();
    return b ? b.offset : 0;
  }
  get rttMs() {
    const b = this.best();
    return b ? b.rtt : 0;
  }
}

export class Backoff {
  constructor(baseMs = 1000, capMs = 15000) {
    this.baseMs = baseMs;
    this.capMs = capMs;
    this.attempt = 0;
  }
  next() {
    const ceiling = Math.min(this.capMs, this.baseMs * 2 ** this.attempt);
    this.attempt += 1;
    return ceiling / 2 + Math.random() * (ceiling / 2);
  }
  reset() {
    this.attempt = 0;
  }
}
