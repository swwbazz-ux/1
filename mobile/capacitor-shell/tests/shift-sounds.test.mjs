import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BITS_PER_SAMPLE,
  CHANNELS,
  SAMPLE_RATE,
  TARGET_PEAK_DBFS,
  buildShiftSoundManifest,
  synthesizeShiftSound,
} from "../scripts/generate-shift-sounds.mjs";

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(shellRoot, "..", "..");
const manifestPath = resolve(shellRoot, "audio", "shift-sounds-manifest.json");
const documentationPath = resolve(shellRoot, "SHIFT_SOUNDS.md");

// These fixed fingerprints identify the reviewed, project-synthesized signals.
// An intentional redesign must update the generator, files, manifest and hashes together.
const expectedHashes = {
  shift_start: "e0f6f86ea48c2ae9349889834435d3711a5811e82275c81e7417d87f23791cfb",
  shift_end: "9a953e2a298a6eb5797c2cd95c5196060112f37a3899727ee8910ff3f9aba810",
};

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function inspectWav(bytes) {
  assert.equal(bytes.subarray(0, 4).toString("ascii"), "RIFF");
  assert.equal(bytes.subarray(8, 12).toString("ascii"), "WAVE");
  assert.equal(bytes.subarray(12, 16).toString("ascii"), "fmt ");
  assert.equal(bytes.readUInt16LE(20), 1, "audio encoding must be PCM");
  assert.equal(bytes.readUInt16LE(22), CHANNELS);
  assert.equal(bytes.readUInt32LE(24), SAMPLE_RATE);
  assert.equal(bytes.readUInt32LE(28), SAMPLE_RATE * 2);
  assert.equal(bytes.readUInt16LE(32), 2);
  assert.equal(bytes.readUInt16LE(34), BITS_PER_SAMPLE);
  assert.equal(bytes.subarray(36, 40).toString("ascii"), "data");
  assert.equal(bytes.readUInt32LE(40), bytes.length - 44);

  const sampleCount = (bytes.length - 44) / 2;
  let peak = 0;
  let squaredSum = 0;
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = bytes.readInt16LE(44 + index * 2);
    peak = Math.max(peak, Math.abs(sample));
    squaredSum += sample * sample;
  }
  return {
    durationSeconds: sampleCount / SAMPLE_RATE,
    peak,
    peakDbfs: 20 * Math.log10(peak / 32_767),
    rmsDbfs: 20 * Math.log10(Math.sqrt(squaredSum / sampleCount) / 32_767),
  };
}

test("shift signals are deterministic, original PCM assets with reviewed fingerprints", () => {
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const generatedManifest = buildShiftSoundManifest();
  assert.deepEqual(manifest, generatedManifest);
  assert.match(manifest.provenance, /Original deterministic synthesis/);
  assert.match(manifest.provenance, /no third-party recordings, samples, speech, or melodies/);

  for (const [signalName, expectedHash] of Object.entries(expectedHashes)) {
    const synthesized = synthesizeShiftSound(signalName);
    assert.equal(sha256(synthesized), expectedHash);
    assert.equal(manifest.sounds[signalName].sha256, expectedHash);

    const analysis = inspectWav(synthesized);
    assert.ok(analysis.durationSeconds >= 1 && analysis.durationSeconds <= 1.8);
    assert.ok(Math.abs(analysis.peakDbfs - TARGET_PEAK_DBFS) <= 0.01);
    assert.ok(analysis.peak < 32_767, "signal must not contain clipped PCM samples");
    assert.ok(analysis.rmsDbfs >= -14, "signal must retain enough average energy for a noisy cabin");

    for (const relativePath of manifest.sounds[signalName].files) {
      const storedBytes = readFileSync(resolve(repositoryRoot, relativePath));
      assert.deepEqual(storedBytes, synthesized, `${relativePath} must match deterministic synthesis`);
    }
  }
  assert.notEqual(expectedHashes.shift_start, expectedHashes.shift_end);
});

test("shift sound provenance documentation records the reviewed fingerprints", () => {
  const documentation = readFileSync(documentationPath, "utf8");
  assert.match(documentation, /нет речи, музыкальных фрагментов/);
  assert.match(documentation, /сторонних сэмплов/);
  assert.match(documentation, /Warcraft\s+не используются/);
  for (const hash of Object.values(expectedHashes)) {
    assert.match(documentation, new RegExp(hash));
  }
});
