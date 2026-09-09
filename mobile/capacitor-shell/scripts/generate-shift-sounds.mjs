import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve, relative, sep } from "node:path";

export const SAMPLE_RATE = 48_000;
export const CHANNELS = 1;
export const BITS_PER_SAMPLE = 16;
export const TARGET_PEAK_DBFS = -1;

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(shellRoot, "..", "..");
const manifestPath = resolve(shellRoot, "audio", "shift-sounds-manifest.json");

const signalDefinitions = {
  shift_start: {
    durationSeconds: 1.2,
    description: "Three short, identical high-presence industrial pulses.",
    synthesize(time) {
      const pulseStarts = [0.055, 0.395, 0.735];
      let value = 0;
      for (const start of pulseStarts) {
        const localTime = time - start;
        const envelope = pulseEnvelope(localTime, 0.245, 0.006, 0.055);
        if (envelope === 0) {
          continue;
        }

        const phase = 2 * Math.PI * (
          1_420 * localTime
          + 2.4 * Math.sin(2 * Math.PI * 17 * localTime) / (2 * Math.PI * 17)
        );
        const metallicTone = (
          0.62 * Math.sin(phase)
          + 0.23 * Math.sin(2 * phase + 0.31)
          + 0.10 * Math.sin(3 * phase + 0.73)
        );
        const presenceBand = 0.25 * Math.sin(2 * Math.PI * 2_180 * localTime + 0.18);
        const body = 0.13 * Math.sin(2 * Math.PI * 710 * localTime);
        const strike = attackNoise(localTime, start * 1_000 + 73) * 0.22;

        value += envelope * (metallicTone + presenceBand + body + strike);
      }
      return value;
    },
  },
  shift_end: {
    durationSeconds: 1.55,
    description: "Two long, descending industrial shutdown pulses.",
    synthesize(time) {
      const pulseStarts = [0.055, 0.79];
      let value = 0;
      for (const start of pulseStarts) {
        const localTime = time - start;
        const pulseLength = 0.57;
        const envelope = pulseEnvelope(localTime, pulseLength, 0.009, 0.075);
        if (envelope === 0) {
          continue;
        }

        const progress = Math.min(1, Math.max(0, localTime / pulseLength));
        const startFrequency = 1_180;
        const endFrequency = 790;
        const sweep = endFrequency - startFrequency;
        const phase = 2 * Math.PI * (
          startFrequency * localTime
          + 0.5 * sweep * localTime * localTime / pulseLength
        );
        const mechanicalTone = (
          0.68 * Math.sin(phase)
          + 0.21 * Math.sin(2 * phase + 0.27)
          + 0.09 * Math.sin(3 * phase + 0.56)
        );
        const warningBand = 0.23 * Math.sin(
          2 * Math.PI * (1_690 - 190 * progress) * localTime + 0.11,
        );
        const body = 0.16 * Math.sin(2 * Math.PI * (430 - 40 * progress) * localTime);
        const tremolo = 0.82 + 0.18 * Math.sin(2 * Math.PI * 14 * localTime);
        const strike = attackNoise(localTime, start * 1_000 + 181) * 0.17;

        value += envelope * tremolo * (mechanicalTone + warningBand + body + strike);
      }
      return value;
    },
  },
};

function pulseEnvelope(time, duration, attack, release) {
  if (time < 0 || time >= duration) {
    return 0;
  }
  const attackGain = Math.min(1, time / attack);
  const releaseGain = Math.min(1, (duration - time) / release);
  const edgeGain = Math.sin(Math.PI * 0.5 * Math.min(attackGain, releaseGain));
  const decay = 0.82 + 0.18 * Math.exp(-3.2 * time / duration);
  return edgeGain * decay;
}

function attackNoise(time, seed) {
  if (time < 0 || time >= 0.032) {
    return 0;
  }
  const sampleIndex = Math.floor(time * SAMPLE_RATE);
  let state = (Math.trunc(seed) ^ (sampleIndex + 1) * 0x9e3779b1) >>> 0;
  state ^= state << 13;
  state ^= state >>> 17;
  state ^= state << 5;
  const noise = ((state >>> 0) / 0x7fffffff) - 1;
  const highPass = noise * Math.sin(2 * Math.PI * 3_600 * time);
  return highPass * Math.exp(-105 * time);
}

function normalizeSamples(samples) {
  const mean = samples.reduce((sum, sample) => sum + sample, 0) / samples.length;
  const compressed = samples.map((sample) => Math.tanh((sample - mean) * 1.35));
  const maximum = compressed.reduce((peak, sample) => Math.max(peak, Math.abs(sample)), 0);
  const targetPeak = 10 ** (TARGET_PEAK_DBFS / 20);
  return compressed.map((sample) => sample / maximum * targetPeak);
}

function encodePcm16Wav(samples) {
  const dataSize = samples.length * 2;
  const wav = Buffer.alloc(44 + dataSize);
  wav.write("RIFF", 0);
  wav.writeUInt32LE(36 + dataSize, 4);
  wav.write("WAVE", 8);
  wav.write("fmt ", 12);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(CHANNELS, 22);
  wav.writeUInt32LE(SAMPLE_RATE, 24);
  wav.writeUInt32LE(SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE / 8, 28);
  wav.writeUInt16LE(CHANNELS * BITS_PER_SAMPLE / 8, 32);
  wav.writeUInt16LE(BITS_PER_SAMPLE, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(dataSize, 40);

  samples.forEach((sample, index) => {
    const pcmSample = Math.round(Math.max(-1, Math.min(1, sample)) * 32_767);
    wav.writeInt16LE(pcmSample, 44 + index * 2);
  });
  return wav;
}

export function synthesizeShiftSound(signalName) {
  const definition = signalDefinitions[signalName];
  if (!definition) {
    throw new Error(`Unknown shift signal: ${signalName}`);
  }
  const sampleCount = Math.round(definition.durationSeconds * SAMPLE_RATE);
  const samples = Array.from(
    { length: sampleCount },
    (_, index) => definition.synthesize(index / SAMPLE_RATE),
  );
  return encodePcm16Wav(normalizeSamples(samples));
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function inspectPcm16(bytes) {
  const sampleCount = bytes.readUInt32LE(40) / 2;
  let peak = 0;
  let squaredSum = 0;
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = bytes.readInt16LE(44 + index * 2);
    peak = Math.max(peak, Math.abs(sample));
    squaredSum += sample * sample;
  }
  return {
    durationMs: Math.round(sampleCount / SAMPLE_RATE * 1_000),
    peakDbfs: Number((20 * Math.log10(peak / 32_767)).toFixed(3)),
    rmsDbfs: Number((20 * Math.log10(Math.sqrt(squaredSum / sampleCount) / 32_767)).toFixed(3)),
  };
}

function portablePath(path) {
  return relative(repositoryRoot, path).split(sep).join("/");
}

function outputPaths(signalName) {
  return ["driver", "excavator"].flatMap((role) => {
    const filename = `${role}_${signalName}.wav`;
    return [
      resolve(shellRoot, "profiles", role, "res", "raw", filename),
      resolve(repositoryRoot, "СИСТЕМА_MVP", "backend", "static", "audio", role, filename),
    ];
  });
}

export function buildShiftSoundManifest() {
  const sounds = {};
  for (const [signalName, definition] of Object.entries(signalDefinitions)) {
    const bytes = synthesizeShiftSound(signalName);
    const analysis = inspectPcm16(bytes);
    sounds[signalName] = {
      description: definition.description,
      sha256: sha256(bytes),
      ...analysis,
      files: outputPaths(signalName).map(portablePath),
    };
  }
  return {
    schemaVersion: 1,
    provenance: "Original deterministic synthesis created inside this repository; no third-party recordings, samples, speech, or melodies are used.",
    generator: portablePath(fileURLToPath(import.meta.url)),
    format: {
      container: "WAV",
      encoding: "PCM signed 16-bit little-endian",
      sampleRateHz: SAMPLE_RATE,
      channels: CHANNELS,
      targetPeakDbfs: TARGET_PEAK_DBFS,
    },
    sounds,
  };
}

function generateFiles() {
  const manifest = buildShiftSoundManifest();
  for (const [signalName, sound] of Object.entries(manifest.sounds)) {
    const bytes = synthesizeShiftSound(signalName);
    for (const outputPath of outputPaths(signalName)) {
      mkdirSync(dirname(outputPath), { recursive: true });
      writeFileSync(outputPath, bytes);
    }
    process.stdout.write(
      `${signalName}: ${sound.durationMs} ms, peak ${sound.peakDbfs} dBFS, RMS ${sound.rmsDbfs} dBFS, SHA-256 ${sound.sha256}\n`,
    );
  }
  mkdirSync(dirname(manifestPath), { recursive: true });
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  generateFiles();
}
