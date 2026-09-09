import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(root, "profiles", "excavator", "res", "raw", "app_alert.wav");
const sampleRate = 44100;
const durationSeconds = 0.72;
const sampleCount = Math.floor(sampleRate * durationSeconds);
const dataSize = sampleCount * 2;
const wav = Buffer.alloc(44 + dataSize);

wav.write("RIFF", 0);
wav.writeUInt32LE(36 + dataSize, 4);
wav.write("WAVE", 8);
wav.write("fmt ", 12);
wav.writeUInt32LE(16, 16);
wav.writeUInt16LE(1, 20);
wav.writeUInt16LE(1, 22);
wav.writeUInt32LE(sampleRate, 24);
wav.writeUInt32LE(sampleRate * 2, 28);
wav.writeUInt16LE(2, 32);
wav.writeUInt16LE(16, 34);
wav.write("data", 36);
wav.writeUInt32LE(dataSize, 40);

for (let index = 0; index < sampleCount; index += 1) {
  const time = index / sampleRate;
  const frequency = time < 0.32 ? 880 : 1174.66;
  const localTime = time < 0.32 ? time : time - 0.32;
  const segmentDuration = time < 0.32 ? 0.32 : 0.4;
  const envelope = Math.sin(Math.PI * Math.min(localTime / segmentDuration, 1)) ** 0.7;
  const sample = Math.round(Math.sin(2 * Math.PI * frequency * time) * envelope * 0.38 * 32767);
  wav.writeInt16LE(sample, 44 + index * 2);
}

mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, wav);
console.log(output);
