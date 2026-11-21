import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

// T-019: доставка при выключенном экране. Специальный обработчик (точка
// разгрузки, назначение) владеет событием; рост версии, который никто не
// забрал, получает одно нейтральное уведомление и сигнал. Применение остаётся
// за WebView, поэтому курсор здесь не двигается.

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const javaRoot = resolve(shellRoot, "android/app/src/main/java/ru/copperresources/mobile");
const java = (name) => readFileSync(resolve(javaRoot, name), "utf8");

test("screen-off version growth without a specific handler raises one neutral alert", () => {
  const service = java("ConnectivityForegroundService.java");
  const start = service.indexOf(
    "if (voiceEligible && serverVersion > previousVersion && relevant && caughtUp)"
  );
  const end = service.indexOf("if (caughtUp && voiceProcessed)", start);
  const block = service.slice(start, end);

  assert.ok(start >= 0, "the catch-up voice block must stay in runHeartbeat");
  assert.ok(end > start, "the catch-up voice block must end before the cursor commit");
  assert.match(block, /VoiceAttempt addressed;/);
  assert.match(block, /boolean eventClaimed = dumpPoint\.candidate \|\| addressed\.candidate;/);
  assert.match(
    block,
    /if \(voiceProcessed && !eventClaimed && !appIsForeground\)\s*\{\s*notifyGenericOperationalEvent\(serverVersion\)/
  );
  assert.doesNotMatch(block, /commitApplied|appliedVersion/);
});

test("neutral alert is reserved once per version and never moves the WebView cursor", () => {
  const service = java("ConnectivityForegroundService.java");
  const start = service.indexOf("private void notifyGenericOperationalEvent");
  const end = service.indexOf("private String currentStatusText", start);
  const method = service.slice(start, end);

  assert.ok(start >= 0 && end > start, "the neutral alert must stay a separate method");
  assert.match(service, /LAST_ALERTED_VERSION = "last_alerted_version"/);
  assert.match(method, /getLong\(LAST_ALERTED_VERSION, 0L\) >= serverVersion/);
  assert.match(method, /putLong\(LAST_ALERTED_VERSION, serverVersion\)\.commit\(\)/);
  assert.match(method, /AppNotifications\.showNeutralOperationalAlert/);
  assert.match(method, /OperationalVoicePlayer\.playCueOnly\(\s*this,\s*OperationalCueCatalog\.SHIFT/);
  assert.match(method, /"Новое событие в смене"/);
  assert.doesNotMatch(method, /commitApplied|appliedVersion|after=/);
  assert.doesNotMatch(method, /№/);
});

test("neutral screen-off notification is private and keeps its own identifier", () => {
  const notifications = java("AppNotifications.java");
  const start = notifications.indexOf("public static boolean showNeutralOperationalAlert");
  const end = notifications.indexOf("public static boolean alertsEnabled", start);
  const method = notifications.slice(start, end);

  assert.ok(start > 0 && end > start, "the neutral alert builder must exist");
  assert.match(notifications, /NEUTRAL_ALERT_NOTIFICATION_ID = 1203/);
  assert.match(method, /setVisibility\(NotificationCompat\.VISIBILITY_PRIVATE\)/);
  assert.doesNotMatch(method, /VISIBILITY_PUBLIC/);
  assert.match(method, /BuildConfig\.ALERT_CHANNEL_ID/);
  assert.match(method, /notify\(NEUTRAL_ALERT_NOTIFICATION_ID/);
});

test("neutral cue plays without a spoken phrase", () => {
  const player = java("OperationalVoicePlayer.java");
  const start = player.indexOf("public static synchronized boolean playCueOnly");
  const end = player.indexOf("private boolean enqueue(String cueName", start);
  assert.ok(start > 0 && end > start, "cue-only playback must exist next to playSequence");
  assert.match(player, /private boolean enqueueCue\(String cueName\)/);
  assert.match(player, /if \(cueResourceId == 0\)\s*\{\s*return false;/);
  // A cue-only request must not require voice resources.
  const enqueueCue = player.slice(
    player.indexOf("private boolean enqueueCue(String cueName)"),
    player.indexOf("private int resourceId(String suffix)")
  );
  assert.doesNotMatch(enqueueCue, /voiceResourceIds|voiceNames/);
});

test("a rejected foreground promotion stops quietly instead of crash-looping", () => {
  const service = java("ConnectivityForegroundService.java");
  const guard = service.slice(
    service.indexOf("private boolean tryStartAsForeground"),
    service.indexOf("private void startAsForeground")
  );
  assert.match(guard, /catch \(RuntimeException error\)/);
  assert.match(guard, /return false;/);

  const command = service.slice(
    service.indexOf("public int onStartCommand"),
    service.indexOf("private boolean tryStartAsForeground")
  );
  assert.match(command, /if \(!tryStartAsForeground\(/);
  assert.match(command, /stopServiceAndRemoveNotification\(\);\s*return START_NOT_STICKY;/);
  assert.doesNotMatch(command, /^\s*startAsForeground\(/m);
});
