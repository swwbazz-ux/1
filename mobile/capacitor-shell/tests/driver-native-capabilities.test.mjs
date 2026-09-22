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

test("Driver and Excavator expose native push and haptics to the WebView", () => {
  const activity = source(javaRoot, "MainActivity.java");
  const profiles = source(javaRoot, "NativeFieldProfile.java");

  assert.match(
    activity,
    /if \(NativeFieldProfile\.supportsPushAndHaptics\(\)\) \{[\s\S]*?registerPlugin\(NativePushPlugin\.class\);[\s\S]*?registerPlugin\(NativeHapticsPlugin\.class\);[\s\S]*?\}/
  );
  assert.match(profiles, /"driver"\.equals\(profileId\) \|\| "excavator"\.equals\(profileId\)/);
  assert.doesNotMatch(profiles, /dispatcher|mining_master/);
});

test("release workflow injects the matching Firebase config for each field APK", () => {
  const workflow = source(root, "..", "..", ".github", "workflows", "production-deploy.yml");

  assert.match(workflow, /DRIVER_GOOGLE_SERVICES_JSON:.*secrets\.DRIVER_GOOGLE_SERVICES_JSON/);
  assert.match(workflow, /EXCAVATOR_GOOGLE_SERVICES_JSON:.*secrets\.EXCAVATOR_GOOGLE_SERVICES_JSON/);
  assert.match(workflow, /APK_PROFILE" == "driver"[\s\S]*?DRIVER_GOOGLE_SERVICES_JSON/);
  assert.match(workflow, /APK_PROFILE" == "excavator"[\s\S]*?EXCAVATOR_GOOGLE_SERVICES_JSON/);
  assert.match(workflow, /trap 'rm -f android\/app\/google-services\.json' EXIT/);
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

test("Native push keeps a durable token and reports only the field-app identity envelope", () => {
  const plugin = source(javaRoot, "NativePushPlugin.java");

  assert.match(plugin, /@CapacitorPlugin\(name = "NativePush"\)/);
  assert.match(plugin, /getSharedPreferences\(PREFS_NAME, Context\.MODE_PRIVATE\)/);
  assert.match(plugin, /notifyListeners\("pushToken", tokenPayload\(normalizedToken\), true\)/);
  assert.match(plugin, /FirebaseApp\.getApps\(getContext\(\)\)\.isEmpty\(\)/);
  assert.match(plugin, /call\.reject\("FCM is not configured for this application build"\)/);
  assert.match(plugin, /\.put\("provider", "fcm"\)/);
  assert.match(plugin, /\.put\("platform", "android"\)/);
  assert.match(plugin, /\.put\("appId", BuildConfig\.APPLICATION_ID\)/);
  assert.match(plugin, /NativeFieldProfile\.supportsPushAndHaptics\(\)/);
  assert.match(plugin, /getInstallationIdentity\(PluginCall call\)/);
  assert.match(plugin, /AppInstallationIdentity\.get\(getContext\(\)\)/);
});

test("FCM presence challenge is acknowledged only through the authenticated heartbeat", () => {
  const fcmService = source(javaRoot, "CopperFirebaseMessagingService.java");
  const connectionService = source(javaRoot, "ConnectivityForegroundService.java");
  const identity = source(javaRoot, "AppInstallationIdentity.java");
  const probe = source(javaRoot, "PresenceProbeState.java");

  assert.match(fcmService, /"presence_probe"\.equals\(kind\)/);
  assert.match(fcmService, /PresenceProbeState\.remember/);
  assert.match(fcmService, /reconcilePresenceProbe/);
  assert.match(connectionService, /X-App-Installation-Id/);
  assert.match(connectionService, /X-App-Observed-Version/);
  assert.match(connectionService, /X-App-Heartbeat-Rtt-Ms/);
  assert.match(connectionService, /X-App-Presence-Probe-Capable/);
  assert.match(connectionService, /X-App-Presence-Probe/);
  assert.match(connectionService, /requestHeartbeat\(sentPresenceProbeId\)/);
  assert.match(connectionService, /PresenceProbeState\.clearIfEquals\(this, sentPresenceProbeId\)/);
  assert.match(identity, /"android-" \+ UUID\.randomUUID\(\)/);
  assert.match(probe, /MAX_AGE_MS = 2 \* 60_000L/);
  assert.match(probe, /sentProbeId\.equals\(currentProbeId\)/);
  assert.doesNotMatch(fcmService, /probe_id.*Log\./);
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
  assert.match(plugin, /NativeFieldProfile\.supportsPushAndHaptics\(\)/);
});
