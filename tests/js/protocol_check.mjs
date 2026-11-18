// Runs the viewer's protocol.js under Node against frames produced by Python.
// Usage: node protocol_check.mjs <protocol.js> <frames.json>
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [protocolPath, framesPath] = process.argv.slice(2);
const proto = await import(pathToFileURL(protocolPath).href);
const frames = JSON.parse(readFileSync(framesPath, "utf8"));

const results = frames.map((hex) => {
  const bytes = Buffer.from(hex, "hex");
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const frame = proto.parseFrame(buffer);
  if (!frame) return null;
  return {
    codec: frame.codec,
    keyframe: frame.keyframe,
    seq: frame.seq,
    captureTsMs: frame.captureTsMs,
    width: frame.width,
    height: frame.height,
    payloadLength: frame.payload.length,
    nalTypes: proto.nalTypes(frame.payload),
    codecString: proto.codecFromAnnexB(frame.payload),
  };
});

const clock = new proto.ClockSync();
clock.addSample(0, 700, 700, 220);
clock.addSample(1000, 1505, 1505, 1010);
const backoff = new proto.Backoff(1000, 4000);
const delays = [backoff.next(), backoff.next(), backoff.next(), backoff.next()];

console.log(JSON.stringify({ results, offset: clock.offsetMs, rtt: clock.rttMs, delays }));
