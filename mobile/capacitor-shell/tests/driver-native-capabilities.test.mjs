import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const androidRoot = resolve(root, "android", "app");
const javaRoot = resolve(
  androidRoot,
  "src",
  "main",
  "java",
  "ru",
  "copperresources",
  "mobile"
);

function source(...parts) {
  return readFileSync(resolve(...parts), "utf8");
}

test("Driver build wires Firebase Messaging without tracking its private config", () => {
  const appGradle = source(androidRoot, "build.gradle");
  const rootGradle = source(root, "android", "build.gradle");
  const ignore = source(root, ".gitignore");

  assert.match(rootGradle, /com\.google\.gms:google-services:4\.4\.4/);
  assert.match(appGradle, /firebase-bom:34\.19\.0/);
  assert.match(appGradle, /firebase-messaging/);
  assert.match(appGradle, /google-services\.json not found/);
  assert.match(ignore, /android\/app\/google-services\.json/);
});

test("Driver alone exposes native push and haptics to the WebView", () => {
  const activity = source(javaRoot, "MainActivity.java");

  assert.match(
    activity,
    /if \("driver"\.equals\(BuildConfig\.APP_PROFILE_ID\)\) \{[\s\S]*?registerPlugin\(NativePushPlugin\.class\);[\s\S]*?registerPlugin\(NativeHapticsPlugin\.class\);[\s\S]*?\}/
  );
});

test("FCM wakes the existing reconciliation path without duplicating domain actions", () => {
  const manifest = source(androidRoot, "src", "main", "AndroidManifest.xml");
  const service = source(javaRoot, "CopperFirebaseMessagingService.java");

  assert.match(manifest, /\.CopperFirebaseMessagingService/);
  assert.match(manifest, /com\.google\.firebase\.MESSAGING_EVENT/);
  assert.match(service, /NativePushPlugin\.publishToken\(this, token\)/);
  assert.match(service, /ConnectivityForegroundService\.reconcileFromForeground\(this\)/);
  assert.doesNotMatch(service, /NotificationManager|MediaPlayer|TextToSpeech/);
});

test("Native push keeps a durable token and reports only the Driver identity envelope", () => {
  const plugin = source(javaRoot, "NativePushPlugin.java");

  assert.match(plugin, /@CapacitorPlugin\(name = "NativePush"\)/);
  assert.match(plugin, /getSharedPreferences\(PREFS_NAME, Context\.MODE_PRIVATE\)/);
  assert.match(plugin, /notifyListeners\("pushToken", tokenPayload\(normalizedToken\), true\)/);
  assert.match(plugin, /\.put\("provider", "fcm"\)/);
  assert.match(plugin, /\.put\("platform", "android"\)/);
  assert.match(plugin, /\.put\("appId", BuildConfig\.APPLICATION_ID\)/);
});

test("Native haptics are bounded and use the media vibration channel", () => {
  const plugin = source(javaRoot, "NativeHapticsPlugin.java");

  assert.match(plugin, /@CapacitorPlugin\(name = "NativeHaptics"\)/);
  assert.match(plugin, /MAX_PATTERN_ITEMS = 31/);
  assert.match(plugin, /MAX_PATTERN_MS = 15_000L/);
  assert.match(plugin, /Math\.max\(1, Math\.min\(255,/);
  assert.match(plugin, /VibrationEffect\.createWaveform\(timings, amplitudes, -1\)/);
  assert.match(plugin, /VibrationAttributes\.USAGE_MEDIA/);
  assert.match(plugin, /AudioAttributes\.USAGE_MEDIA/);
});
