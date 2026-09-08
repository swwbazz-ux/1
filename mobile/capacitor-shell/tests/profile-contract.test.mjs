import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { test } from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function profile(name) {
  return Object.fromEntries(
    readFileSync(resolve(root, "profiles", name, "app.properties"), "utf8")
      .split(/\r?\n/)
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => {
        const separator = line.indexOf("=");
        return [line.slice(0, separator), line.slice(separator + 1)];
      })
  );
}

const expectedProfiles = {
  excavator: {
    serverUrl: "https://excavator.driverform.ru/",
    startUrl: "https://excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "34",
    versionName: "0.1.22",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  driver: {
    serverUrl: "https://driver.driverform.ru/",
    startUrl: "https://driver.driverform.ru/driver/",
    applicationId: "ru.copperresources.driver",
    appName: "Водитель",
    versionCode: "37",
    versionName: "0.1.23",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#8CFF2E",
    splashIconResource: "app_icon",
  },
  driver_qa: {
    serverUrl: "https://qa-driver.driverform.ru/",
    startUrl: "https://qa-driver.driverform.ru/driver/",
    applicationId: "ru.copperresources.driver.qa",
    appName: "Водитель QA",
    versionCode: "4",
    versionName: "1.0.3-qa",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#8CFF2E",
    splashIconResource: "app_icon",
  },
  driver_rustore: {
    serverUrl: "https://driver.driverform.ru/",
    startUrl: "https://driver.driverform.ru/driver/",
    applicationId: "ru.copperresources.driver",
    appName: "Водитель",
    versionCode: "39",
    versionName: "0.1.23",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#8CFF2E",
    splashIconResource: "app_icon",
  },
  driver_rustore_qa: {
    serverUrl: "https://qa-driver.driverform.ru/",
    startUrl: "https://qa-driver.driverform.ru/driver/",
    applicationId: "ru.copperresources.driver",
    appName: "Водитель",
    versionCode: "38",
    versionName: "0.1.23-rc",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#8CFF2E",
    splashIconResource: "app_icon",
  },
  excavator_qa: {
    serverUrl: "https://qa-excavator.driverform.ru/",
    startUrl: "https://qa-excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator.qa",
    appName: "Экскаваторщик QA",
    versionCode: "7",
    versionName: "1.0.6-qa",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  excavator_rustore: {
    serverUrl: "https://excavator.driverform.ru/",
    startUrl: "https://excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "36",
    versionName: "0.1.22",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
  excavator_rustore_qa: {
    serverUrl: "https://qa-excavator.driverform.ru/",
    startUrl: "https://qa-excavator.driverform.ru/excavator/work/",
    applicationId: "ru.copperresources.excavator",
    appName: "Экскаваторщик",
    versionCode: "35",
    versionName: "0.1.22-rc",
    splashBackgroundColor: "#02080b",
    splashAccentColor: "#FFD200",
    splashIconResource: "app_icon",
  },
};

for (const [profileName, expected] of Object.entries(expectedProfiles)) {
  test(`${profileName} build profile contains every role-specific parameter`, () => {
    const config = profile(profileName);
    assert.equal(config.serverUrl, expected.serverUrl);
    assert.equal(config.startUrl, expected.startUrl);
    assert.ok(config.startUrl.startsWith(config.serverUrl));
    assert.equal(config.applicationId, expected.applicationId);
    assert.equal(config.appName, expected.appName);
    assert.match(config.applicationId, /^[a-z][a-z0-9_.]+$/);
    assert.ok(config.heartbeatUrl.startsWith(config.serverUrl));
    assert.ok(Number(config.heartbeatIntervalSeconds) >= 15);
    if (config.inAppUpdaterEnabled === "false") {
      assert.equal(config.updateManifestUrl, "");
      assert.equal(config.updateApkBaseUrl, "");
    } else {
      assert.match(config.updateManifestUrl, /^https:\/\//);
      assert.match(config.updateApkBaseUrl, /^https:\/\//);
    }
    assert.ok(Number(config.updateCheckIntervalMinutes) >= 5);
    assert.ok(config.alertSoundResource);
    assert.ok(Number(config.alertCueDurationMs || 720) >= 0);
    assert.ok(Number(config.voiceAfterCueDelayMs || 200) >= 0);
    assert.ok(config.syncTokenEnv);
    assert.equal(config.versionCode, expected.versionCode);
    assert.equal(config.versionName, expected.versionName);
    assert.equal(config.splashBackgroundColor, expected.splashBackgroundColor);
    assert.equal(config.splashAccentColor, expected.splashAccentColor);
    assert.equal(config.splashIconResource, expected.splashIconResource);
  });
}

test("profiles remain isolated by URL and application id", () => {
  const excavator = profile("excavator");
  const driver = profile("driver");
  assert.notEqual(excavator.serverUrl, driver.serverUrl);
  assert.notEqual(excavator.applicationId, driver.applicationId);
  assert.notEqual(excavator.foregroundChannelId, driver.foregroundChannelId);
  assert.notEqual(excavator.alertChannelId, driver.alertChannelId);
});

test("QA and RuStore variants keep role identity but disable sideload updates", () => {
  for (const role of ["excavator", "driver"]) {
    const qa = profile(`${role}_qa`);
    const rustoreQa = profile(`${role}_rustore_qa`);
    const rustore = profile(`${role}_rustore`);
    assert.equal(qa.appProfileId, role);
    assert.equal(rustoreQa.appProfileId, role);
    assert.equal(rustore.appProfileId, role);
    assert.equal(qa.resourceProfile, role);
    assert.equal(rustoreQa.resourceProfile, role);
    assert.equal(rustore.resourceProfile, role);
    assert.equal(qa.inAppUpdaterEnabled, "false");
    assert.equal(rustoreQa.inAppUpdaterEnabled, "false");
    assert.equal(rustore.inAppUpdaterEnabled, "false");
    assert.notEqual(qa.applicationId, rustore.applicationId);
    assert.equal(rustoreQa.applicationId, rustore.applicationId);
    assert.notEqual(rustoreQa.serverUrl, rustore.serverUrl);
    assert.ok(Number(profile(role).versionCode) <= Number(rustoreQa.versionCode));
    assert.ok(Number(rustore.versionCode) > Number(rustoreQa.versionCode));
    assert.ok(Number(rustore.versionCode) > Number(profile(role).versionCode));
  }
});

test("driver QA and store variants use isolated notification channels and QA credentials", () => {
  const driverProfiles = ["driver", "driver_qa", "driver_rustore_qa", "driver_rustore"].map(profile);
  assert.equal(new Set(driverProfiles.map((config) => config.foregroundChannelId)).size, driverProfiles.length);
  assert.equal(new Set(driverProfiles.map((config) => config.alertChannelId)).size, driverProfiles.length);

  for (const config of [profile("driver_qa"), profile("driver_rustore_qa")]) {
    assert.equal(config.syncTokenEnv, "COPPER_DRIVER_QA_SYNC_TOKEN");
    assert.match(config.heartbeatUrl, /[?&]role_app_code=driver(?:&|$)/);
  }
  assert.equal(profile("driver_rustore").syncTokenEnv, "COPPER_DRIVER_SYNC_TOKEN");
});

test("package scripts cover every driver QA and RuStore build", () => {
  const scripts = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")).scripts;
  assert.equal(scripts["android:build:driver-qa"], "node scripts/build-android.mjs driver_qa debug");
  assert.equal(scripts["android:release:driver-qa"], "node scripts/build-android.mjs driver_qa release");
  assert.equal(scripts["android:release:driver-rustore-qa"], "node scripts/build-android.mjs driver_rustore_qa release");
  assert.equal(scripts["android:release:driver-rustore"], "node scripts/build-android.mjs driver_rustore release");
});

test("driver and excavator builds embed the same complete loud sound pack", () => {
  const eventNames = [
    "truck_assigned",
    "action_ok",
    "action_error",
    "connection_lost",
    "connection_restored",
    "shift_start",
    "shift_end",
  ];
  for (const profileName of ["driver", "excavator"]) {
    const nativeRaw = resolve(root, "profiles", profileName, "res", "raw");
    const webAudio = resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "static", "audio", profileName);
    for (const eventName of eventNames) {
      const soundName = `${profileName}_${eventName}.wav`;
      const nativeBytes = readFileSync(resolve(nativeRaw, soundName));
      const webBytes = readFileSync(resolve(webAudio, soundName));
      assert.equal(nativeBytes.subarray(0, 4).toString("ascii"), "RIFF");
      assert.equal(nativeBytes.subarray(8, 12).toString("ascii"), "WAVE");
      assert.deepEqual(nativeBytes, webBytes);
    }
  }
  for (const profileName of [
    "excavator",
    "excavator_qa",
    "excavator_rustore_qa",
    "excavator_rustore",
    "driver",
    "driver_qa",
    "driver_rustore_qa",
    "driver_rustore",
  ]) {
    const config = profile(profileName);
    const role = profileName.startsWith("excavator") ? "excavator" : "driver";
    assert.equal(config.resourceProfile || profileName, role);
    assert.equal(config.alertSoundResource, `${role}_truck_assigned`);
    assert.match(config.alertChannelId, /_v2$/);
  }

  const activity = readFileSync(resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"), "utf8");
  const plugin = readFileSync(resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "NativeSoundPlugin.java"), "utf8");
  assert.match(activity, /"excavator"\.equals\(BuildConfig\.APP_PROFILE_ID\)[\s\S]*?"driver"\.equals\(BuildConfig\.APP_PROFILE_ID\)[\s\S]*?registerPlugin\(NativeSoundPlugin\.class\)/);
  assert.match(plugin, /@CapacitorPlugin\(name = "NativeSound"\)/);
  assert.match(plugin, /BuildConfig\.APP_PROFILE_ID \+ "_" \+ soundName/);
  assert.match(plugin, /setVolume\(1\.0f, 1\.0f\)/);
  assert.match(plugin, /USAGE_ASSISTANCE_SONIFICATION/);
  const driver = profile("driver");
  assert.equal(driver.alertSoundResource, "driver_truck_assigned");
  assert.match(driver.alertChannelId, /_v2$/);
});

test("native builds expose an explicit keyboard close bridge", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");
  const plugin = readFileSync(resolve(javaRoot, "NativeKeyboardPlugin.java"), "utf8");
  const imeWebView = readFileSync(resolve(javaRoot, "NativeImeWebView.java"), "utf8");
  const bridgeLayout = readFileSync(resolve(root, "android", "app", "src", "main", "res", "layout", "capacitor_bridge_layout_main.xml"), "utf8");

  assert.match(activity, /registerPlugin\(NativeKeyboardPlugin\.class\)/);
  assert.match(plugin, /@CapacitorPlugin\(name = "NativeKeyboard"\)/);
  assert.match(plugin, /hideSoftInputFromWindow\(webView\.getWindowToken\(\), 0\)/);
  assert.match(plugin, /setAction\(PluginCall call\)/);
  assert.match(bridgeLayout, /ru\.copperresources\.mobile\.NativeImeWebView/);
  assert.match(bridgeLayout, /android:id="@\+id\/webview"/);
  assert.match(imeWebView, /extends CapacitorWebView/);
  assert.match(imeWebView, /new InputConnectionWrapper\(inputConnection, false\)/);
  assert.match(imeWebView, /performEditorAction\(int actionCode\)/);
  assert.match(imeWebView, /native-ime-action/);
  assert.match(imeWebView, /return true;/);
});

test("rejected native phone handoff remains disabled", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  const manifest = readFileSync(resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"), "utf8");
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");

  assert.match(gradle, /new URI\(rawValue\)/);
  assert.match(gradle, /startUrl must stay inside the exact serverUrl origin/);
  assert.doesNotMatch(gradle, /APP_LINK_|appLinkPath|manifestPlaceholders\.appLink/);
  assert.doesNotMatch(manifest, /android:autoVerify|android\.intent\.action\.VIEW|appLinkPath/);
  assert.doesNotMatch(activity, /NativeAppLink|resolveNativeAppLink|onNewIntent\(Intent intent\)/);
});

test("in-app updater mutates only dedicated version badges", () => {
  const updater = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "AppUpdateManager.java"),
    "utf8"
  );

  assert.match(
    updater,
    /querySelectorAll\('\.native-app-version\[data-native-app-version\]'\)/
  );
  assert.doesNotMatch(
    updater,
    /querySelectorAll\('\[data-native-app-version\]'\)/
  );
});

test("only the driver profile enables recorded dump-point voice alerts", () => {
  const excavator = profile("excavator");
  const driver = profile("driver");
  assert.notEqual(excavator.driverVoiceAlertsEnabled, "true");
  assert.equal(driver.driverVoiceAlertsEnabled, "true");
  assert.equal(driver.alertCueDurationMs, "900");
  assert.equal(driver.voiceAfterCueDelayMs, "200");
});

test("driver profile packages every approved recorded dump-point phrase", () => {
  const rawRoot = resolve(root, "profiles", "driver", "res", "raw");
  const resources = [
    "voice_na_skdr.m4a",
    "voice_edem_na_kkd.m4a",
    "voice_na_otval.m4a",
    "voice_edem_na_svh.m4a",
    "voice_na_kisluhu.m4a",
    "voice_na_sklad_negabaritov.m4a",
    "voice_na_bufernyi_sklad.m4a",
    "voice_na_podsypku.m4a",
  ];
  for (const resource of resources) {
    const path = resolve(rawRoot, resource);
    assert.ok(statSync(path).size > 8_000, `${resource} must contain real audio`);
    const header = readFileSync(path).subarray(0, 32).toString("latin1");
    assert.match(header, /ftyp/, `${resource} must be an MPEG-4 audio resource`);
  }
});

test("background service announces only a fresh driver truck_loaded event", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const service = readFileSync(resolve(javaRoot, "ConnectivityForegroundService.java"), "utf8");
  const player = readFileSync(resolve(javaRoot, "DriverVoicePlayer.java"), "utf8");
  const announcer = readFileSync(resolve(javaRoot, "DriverDumpPointAnnouncer.java"), "utf8");
  const catalog = readFileSync(resolve(javaRoot, "DriverVoiceCatalog.java"), "utf8");
  assert.match(service, /"trip_changed"\.equals\(event\.optString\("type"\)\)/);
  assert.match(service, /"truck_loaded"\.equals\(payload\.optString\("action"\)\)/);
  assert.match(service, /last_driver_dump_point_alert_version/);
  assert.match(announcer, /ALERT_CUE_DURATION_MS[\s\S]*?VOICE_AFTER_CUE_DELAY_MS/);
  assert.match(player, /AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK/);
  assert.match(player, /DriverVoiceCatalog\.resourceNameFor/);
  for (const [id, resource] of [
    [1, "voice_edem_na_kkd"],
    [2, "voice_na_skdr"],
    [3, "voice_na_otval"],
    [4, "voice_na_sklad_negabaritov"],
    [5, "voice_na_kisluhu"],
    [6, "voice_na_podsypku"],
  ]) {
    assert.match(catalog, new RegExp(`case ${id}:[\\s\\S]*?return "${resource}";`));
  }
  assert.match(catalog, /normalized\.equals\("свх"\)[\s\S]*?voice_edem_na_svh/);
  assert.match(catalog, /normalized\.equals\("буферный склад"\)[\s\S]*?voice_na_bufernyi_sklad/);
});

test("both production profiles package every approved operational voice phrase", () => {
  const common = [
    "voice_shift_opened",
    "voice_shift_closed",
    "voice_downtime_started",
    "voice_downtime_finished",
    "voice_action_failed",
    "voice_connection_lost",
    "voice_connection_restored",
  ];
  const byRole = {
    driver: [
      "voice_excavator_assigned",
      "voice_excavator_changed",
      "voice_assignment_removed",
      "voice_trip_finished",
      "voice_trip_finish_failed",
    ],
    excavator: [
      "voice_truck_assigned",
      "voice_truck_removed",
      "voice_face_settings_saved",
      "voice_truck_sent",
      "voice_truck_send_failed",
    ],
  };
  for (const role of Object.keys(byRole)) {
    const rawRoot = resolve(root, "profiles", role, "res", "raw");
    for (const suffix of [...common, ...byRole[role]]) {
      const resource = resolve(rawRoot, `${role}_${suffix}.m4a`);
      assert.ok(statSync(resource).size > 8_000, `${resource} must contain real audio`);
      assert.match(
        readFileSync(resource).subarray(0, 32).toString("latin1"),
        /ftyp/,
        `${resource} must be an MPEG-4 audio resource`
      );
    }
  }

  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const plugin = readFileSync(resolve(javaRoot, "NativeSoundPlugin.java"), "utf8");
  const player = readFileSync(resolve(javaRoot, "OperationalVoicePlayer.java"), "utf8");
  const announcer = readFileSync(resolve(javaRoot, "OperationalVoiceAnnouncer.java"), "utf8");
  const service = readFileSync(resolve(javaRoot, "ConnectivityForegroundService.java"), "utf8");
  assert.match(plugin, /public void announceOperational\(PluginCall call\)/);
  assert.match(player, /VOICE_AFTER_CUE_DELAY_MS/);
  assert.match(player, /AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK/);
  assert.match(announcer, /last_operational_voice_/);
  assert.match(service, /showLatestAssignmentAlert/);
  assert.match(service, /CONNECTION_LOSS_ANNOUNCED/);
});

test("recorded equipment numbers are packaged and routed through native sequences", () => {
  const manifest = JSON.parse(readFileSync(resolve(root, "audio", "equipment-voices-manifest.json"), "utf8"));
  const counts = {
    excavatorCommon: 3,
    excavatorDestinations: 8,
    truckNumbers: 54,
    driverAssignments: 13,
    driverReserveAssignments: 2,
  };
  for (const [batchName, expectedCount] of Object.entries(counts)) {
    const batch = manifest.batches[batchName];
    assert.equal(batch.segments.length, expectedCount);
    for (const segment of batch.segments) {
      const audioPath = resolve(root, "profiles", batch.profile, "res", "raw", segment.resource);
      const bytes = readFileSync(audioPath);
      assert.ok(bytes.length > 1_000, `${segment.resource} must contain recorded audio`);
      assert.match(bytes.subarray(0, 32).toString("latin1"), /ftyp/);
      assert.ok(segment.peakDbfs > -20, `${segment.resource} must contain audible speech`);
      assert.equal(createHash("sha256").update(bytes).digest("hex"), segment.sha256);
    }
  }

  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const catalog = readFileSync(resolve(javaRoot, "EquipmentVoiceCatalog.java"), "utf8");
  const plugin = readFileSync(resolve(javaRoot, "NativeSoundPlugin.java"), "utf8");
  const player = readFileSync(resolve(javaRoot, "OperationalVoicePlayer.java"), "utf8");
  const service = readFileSync(resolve(javaRoot, "ConnectivityForegroundService.java"), "utf8");
  const sounds = readFileSync(resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "static", "js", "mobile-operational-sounds-v1.js"), "utf8");
  const driverTemplate = readFileSync(resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "templates", "users", "driver_shift.html"), "utf8");
  const excavatorTemplate = readFileSync(resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "templates", "trips", "excavator_work.html"), "utf8");

  assert.match(plugin, /public void announceEquipment\(PluginCall call\)/);
  assert.match(player, /playSequence\([\s\S]*?playVoiceSegment/);
  assert.match(catalog, /case "528":[\s\S]*?voice_excavator_assignment_" \+ normalized/);
  assert.match(catalog, /number >= 10 && number <= 52/);
  assert.match(catalog, /number >= 54 && number <= 63/);
  assert.match(catalog, /voice_truck_sent_sklad_okislennoy_rudy/);
  assert.match(service, /target_excavator_number/);
  assert.match(service, /truck_number/);
  assert.match(sounds, /announceEquipment: announceEquipment/);
  assert.match(driverTemplate, /driver_excavator_assigned/);
  assert.match(driverTemplate, /data-driver-excavator-number/);
  assert.match(excavatorTemplate, /excavator_truck_assigned/);
  assert.match(excavatorTemplate, /excavator_truck_removed/);
  assert.match(excavatorTemplate, /excavator_truck_sent/);
});

test("foreground driver screen uses the same deduplicated recorded voice bridge", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const plugin = readFileSync(resolve(javaRoot, "NativeSoundPlugin.java"), "utf8");
  const service = readFileSync(resolve(javaRoot, "ConnectivityForegroundService.java"), "utf8");
  const player = readFileSync(resolve(javaRoot, "DriverVoicePlayer.java"), "utf8");
  const sounds = readFileSync(resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "static", "js", "mobile-operational-sounds-v1.js"), "utf8");
  const driverTemplate = readFileSync(resolve(root, "..", "..", "СИСТЕМА_MVP", "backend", "templates", "users", "driver_shift.html"), "utf8");

  assert.match(plugin, /@PluginMethod\s+public void announceDumpPoint\(PluginCall call\)/);
  assert.match(plugin, /call\.getData\(\)\.opt\(name\)/);
  assert.match(plugin, /static long numericLong\(Object value\)[\s\S]*?value instanceof Number[\s\S]*?longValue\(\)/);
  assert.doesNotMatch(plugin, /call\.getLong\("(?:eventVersion|tripId|dumpPointId)"/);
  const announcer = readFileSync(resolve(javaRoot, "DriverDumpPointAnnouncer.java"), "utf8");
  assert.match(plugin, /DriverDumpPointAnnouncer\.announce/);
  assert.match(announcer, /private static DriverVoicePlayer sharedPlayer/);
  assert.match(announcer, /lastScheduledTripId/);
  assert.match(announcer, /last_driver_dump_point_alert_trip_id[\s\S]*?tripId == persistedTripId[\s\S]*?tripId == lastScheduledTripId/);
  assert.match(announcer, /ALERT_CUE_DURATION_MS[\s\S]*?VOICE_AFTER_CUE_DELAY_MS[\s\S]*?sharedPlayer\.announce/);
  assert.match(announcer, /sharedPlayer\.announce\([\s\S]*?preferences\.edit\(\)/);
  assert.match(plugin, /result\.announced[\s\S]*?cuePlayed/);
  assert.match(plugin, /dumpPointName,[\s\S]*?false,[\s\S]*?true/);
  assert.match(service, /displayName,[\s\S]*?showNotification,[\s\S]*?true/);
  assert.match(player, /void announce\([\s\S]*?boolean playCue\)[\s\S]*?playAlertCue\(\)/);
  assert.match(service, /DriverDumpPointAnnouncer\.announce/);
  assert.doesNotMatch(service, /private DriverVoicePlayer driverVoicePlayer/);
  assert.doesNotMatch(plugin, /private DriverVoicePlayer driverVoicePlayer/);
  assert.match(player, /recordStage\([\s\S]*?"queued"/);
  assert.match(sounds, /announceDumpPoint: announceDumpPoint/);
  assert.match(sounds, /diagnostics: diagnostics/);
  assert.match(driverTemplate, /operational-state-refresh-applied/);
  assert.match(driverTemplate, /event\.type !== "trip_changed"/);
  assert.match(driverTemplate, /payload\.action !== "truck_loaded"/);
  assert.match(driverTemplate, /oldShell\.dataset\.driverHasLoadedTrip !== "true"/);
  assert.match(driverTemplate, /freshShell\.dataset\.driverHasLoadedTrip === "true"/);
  assert.match(driverTemplate, /becameLoaded[\s\S]*?playDriverDumpPointAlert/);
  assert.match(plugin, /@PluginMethod\s+public void getDiagnostics\(PluginCall call\)/);
  assert.match(player, /recordStage\("cue_started"/);
  assert.match(player, /recordStage\("voice_started"/);
});

test("WebView cookies are accepted and flushed at every persistence boundary", () => {
  const activity = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"),
    "utf8"
  );
  assert.match(activity, /setAcceptCookie\(true\)/);
  assert.match(activity, /setAcceptThirdPartyCookies\(webView, true\)/);
  assert.match(activity, /bridgeBuilder\.addWebViewListener\(new WebViewListener/);
  assert.match(activity, /onPageLoaded\(WebView loadedWebView\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.match(activity, /onPause\(\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.match(activity, /onStop\(\)[\s\S]*?CookieManager\.getInstance\(\)\.flush\(\)/);
  assert.doesNotMatch(activity, /remove(All|Session)Cookies|clearCookies/);
});

test("Android backup cannot export persisted WebView sessions", () => {
  const manifest = readFileSync(
    resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"),
    "utf8"
  );
  assert.match(manifest, /android:allowBackup="false"/);
  assert.doesNotMatch(manifest, /android:allowBackup="true"/);
});

test("native heartbeat follows the active-shift lifecycle and reports the exact APK version", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");
  const service = readFileSync(resolve(javaRoot, "ConnectivityForegroundService.java"), "utf8");
  const manifest = readFileSync(
    resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"),
    "utf8"
  );

  assert.match(service, /return START_STICKY;/);
  assert.match(service, /return START_NOT_STICKY;/);
  assert.match(service, /onTaskRemoved\(Intent rootIntent\)[\s\S]*?ConnectionState\.disable\(this, "task_removed"\)/);
  assert.match(service, /onTaskRemoved\(Intent rootIntent\)[\s\S]*?stopServiceAndRemoveNotification\(\)/);
  assert.match(service, /background_connection_required/);
  assert.match(service, /ConnectionState\.applyServerRequirement/);
  assert.match(service, /reconcileFromForeground\(Context context\)[\s\S]*?!ConnectionState\.isDesired\(context\)[\s\S]*?return;/);
  assert.doesNotMatch(service, /allow_foreground_probe/);
  assert.match(service, /ACTION_STOP_CONNECTION/);
  assert.match(service, /MAX_BACKOFF_MS = 60_000L/);
  assert.match(service, /CopperResourcesNative\/" \+ BuildConfig\.APP_PROFILE_ID[\s\S]*?BuildConfig\.VERSION_NAME/);
  assert.match(manifest, /android:stopWithTask="false"/);
  assert.doesNotMatch(manifest, /android:stopWithTask="true"/);
  assert.match(activity, /registerPlugin\(BackgroundConnectionPlugin\.class\)/);
  assert.match(activity, /onStart\(\)[\s\S]*?ConnectivityForegroundService\.reconcileFromForeground\(this\)/);
  assert.doesNotMatch(activity, /onDestroy\(\)[\s\S]*?ConnectivityForegroundService\.stop\(this\)/);
  assert.match(activity, /BATTERY_PROMPT_COOLDOWN_MS/);
  assert.match(activity, /BuildConfig\.VERSION_NAME\.equals\(promptedVersion\)/);
  assert.match(activity, /ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS/);

  const connectionState = readFileSync(resolve(javaRoot, "ConnectionState.java"), "utf8");
  const connectionPlugin = readFileSync(resolve(javaRoot, "BackgroundConnectionPlugin.java"), "utf8");
  const pendingShiftClose = readFileSync(resolve(javaRoot, "PendingDriverShiftClose.java"), "utf8");
  const notifications = readFileSync(resolve(javaRoot, "AppNotifications.java"), "utf8");
  assert.match(connectionState, /putBoolean\(CONNECTION_DESIRED, true\)/);
  assert.match(connectionState, /putBoolean\(CONNECTION_DESIRED, false\)/);
  assert.match(connectionState, /\.commit\(\)/);
  assert.match(connectionPlugin, /@CapacitorPlugin\(name = "BackgroundConnection"\)/);
  assert.match(connectionPlugin, /public void sync\(PluginCall call\)/);
  assert.match(connectionPlugin, /public void stop\(PluginCall call\)/);
  assert.match(connectionPlugin, /public void queueDriverShiftClose\(PluginCall call\)/);
  assert.match(connectionPlugin, /public void acknowledgeDriverShiftClose\(PluginCall call\)/);
  assert.match(connectionPlugin, /PendingDriverShiftClose\.enqueue/);
  assert.match(pendingShiftClose, /pending_driver_shift_close_v1/);
  assert.match(pendingShiftClose, /\.commit\(\)/);
  assert.match(service, /runHeartbeat\(\)[\s\S]*?flushPendingDriverShiftClose\(\)[\s\S]*?requestHeartbeat\(\)/);
  assert.match(service, /onTaskRemoved\(Intent rootIntent\)[\s\S]*?PendingDriverShiftClose\.hasPending\(this\)[\s\S]*?scheduleHeartbeat\(0L\)/);
  assert.match(service, /requestDriverShiftClose[\s\S]*?X-CSRFToken[\s\S]*?client_action_id[\s\S]*?shift_id/);
  assert.match(notifications, /PendingDriverShiftClose\.hasPending\(context\)[\s\S]*?Открыть приложение/);
  assert.match(notifications, /Остановить связь/);
  assert.doesNotMatch(notifications, /Тест сигнала/);
  assert.match(notifications, /\.setSilent\(true\)/);
});

test("native implementation reads role data only from BuildConfig", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const sources = ["MainActivity.java", "ConnectivityForegroundService.java", "AppNotifications.java", "AppUpdateManager.java"]
    .map((file) => readFileSync(resolve(javaRoot, file), "utf8"))
    .join("\n");
  assert.doesNotMatch(sources, /https:\/\/(excavator|driver)\.driverform\.ru/);
  assert.match(sources, /BuildConfig\.APP_SERVER_URL/);
  assert.match(sources, /BuildConfig\.APP_START_URL/);
  assert.match(sources, /BuildConfig\.HEARTBEAT_URL/);
  assert.match(sources, /BuildConfig\.SYNC_AUTH_TOKEN/);
  assert.match(sources, /BuildConfig\.UPDATE_MANIFEST_URL/);
});

test("startup splash is profile-driven and waits for stable rendered layout", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  const styles = readFileSync(resolve(root, "android", "app", "src", "main", "res", "values", "styles.xml"), "utf8");
  const activity = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "MainActivity.java"),
    "utf8"
  );
  const overlay = readFileSync(
    resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile", "StartupLoadingOverlay.java"),
    "utf8"
  );
  const watchdogBody = overlay.slice(
    overlay.indexOf("private void runStartupWatchdog("),
    overlay.indexOf("private void restartProbeIfReady()")
  );

  assert.match(gradle, /profile\.getProperty\('splashBackgroundColor'/);
  assert.match(gradle, /profile\.getProperty\('splashAccentColor'/);
  assert.match(gradle, /profile\.getProperty\('splashIconResource'/);
  assert.match(gradle, /resValue "drawable", "startup_splash_icon"/);
  assert.match(styles, /windowSplashScreenBackground/);
  assert.match(styles, /windowSplashScreenAnimatedIcon/);
  assert.match(styles, /postSplashScreenTheme/);
  assert.match(activity, /SplashScreen\.installSplashScreen\(this\)[\s\S]*?super\.onCreate/);
  assert.match(activity, /setKeepOnScreenCondition\(\(\) -> !nativeCoverReady\)/);
  assert.match(activity, /onPageStarted\(WebView loadingWebView\)[\s\S]*?startupLoadingOverlay\.onPageStarted/);
  assert.match(activity, /onPageLoaded\(WebView loadedWebView\)[\s\S]*?startupLoadingOverlay\.onPageLoaded/);
  assert.match(activity, /lastObservedPageState = PageState\.STARTED[\s\S]*?startupLoadingOverlay\.onPageStarted\(loadingWebView\)/);
  assert.match(activity, /lastObservedPageState = PageState\.LOADED[\s\S]*?startupLoadingOverlay\.onPageLoaded\(loadedWebView\)/);
  assert.match(activity, /StartupLoadingOverlay\.attach\(this, webView\)[\s\S]*?lastObservedPageState == PageState\.LOADED[\s\S]*?startupLoadingOverlay\.onPageLoaded\(webView\)[\s\S]*?else[\s\S]*?startupLoadingOverlay\.onPageStarted\(webView\)/);
  assert.doesNotMatch(activity, /getProgress\(\)/);
  assert.match(overlay, /BuildConfig\.SPLASH_BACKGROUND_COLOR/);
  assert.match(overlay, /BuildConfig\.SPLASH_ACCENT_COLOR/);
  assert.match(overlay, /BuildConfig\.SPLASH_ICON_RESOURCE/);
  assert.match(overlay, /APPLICATION_DOCUMENT_PROBE/);
  assert.match(overlay, /hasAttribute\('data-app-contract-ready'\)/);
  assert.match(overlay, /onPageLoaded\(WebView loadedWebView\)[\s\S]*?APPLICATION_DOCUMENT_PROBE[\s\S]*?enterRecovery\(generation, DiagnosticReason\.ERROR\)/);
  assert.match(overlay, /document\.readyState/);
  assert.match(overlay, /window\.visualViewport/);
  assert.match(overlay, /document\.fonts/);
  assert.match(overlay, /ResizeObserver/);
  assert.match(overlay, /MutationObserver/);
  assert.match(overlay, /data-driver-shell-bound/);
  assert.match(overlay, /data-eo-initialized/);
  assert.match(overlay, /querySelectorAll\('\[data-driver-tab-panel\]\.is-active'\)/);
  assert.match(overlay, /excavatorShell\.dataset\.eoActiveTab/);
  assert.match(overlay, /panel\.dataset\.eoScreen===excavatorActiveName/);
  assert.match(overlay, /node\.getClientRects\(\)\.length>0/);
  assert.match(overlay, /Number\.isFinite\(value\.width\)[\s\S]*?value\.width>0&&value\.height>0/);
  assert.match(overlay, /visibleBox\(driverActivePanel\)/);
  assert.match(overlay, /visibleBox\(excavatorActivePanel\)&&!excavatorActivePanel\.hidden/);
  assert.match(overlay, /REQUIRED_STABLE_FRAMES/);
  assert.match(overlay, /REQUIRED_QUIET_WINDOW_MS/);
  assert.match(overlay, /WindowInsetsCompat\.Type\.ime\(\)/);
  assert.match(overlay, /webView\.setAlpha\(0f\)/);
  assert.doesNotMatch(overlay, /View\.INVISIBLE/);
  assert.match(overlay, /postVisualStateCallback/);
  assert.match(overlay, /postVisualStateCallback[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /webView\.setAlpha\(1f\)[\s\S]*?postOnAnimation\(\(\) -> dismiss\(generation\)\)/);
  assert.match(overlay, /waitForImeClose/);
  assert.match(overlay, /waitForImeClose = rootInsets != null && rootInsets\.isVisible\(WindowInsetsCompat\.Type\.ime\(\)\)/);
  assert.match(overlay, /if \(waitForImeClose && !imeVisible\)[\s\S]*?waitForImeClose = false/);
  assert.match(overlay, /now - nativeLastChangeMs >= REQUIRED_NATIVE_QUIET_WINDOW_MS[\s\S]*?&& !waitForImeClose/);
  assert.match(overlay, /generation != pageGeneration/);
  assert.match(overlay, /STARTUP_WATCHDOG_MS = 15_000L/);
  assert.match(overlay, /runStartupWatchdog\(int generation, WebView watchedWebView\)[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /enterRecovery\([\s\S]*?DiagnosticReason\.WATCHDOG/);
  assert.doesNotMatch(watchdogBody, /webView\.setAlpha\(1f\)|dismiss\(generation\)/);
  assert.match(overlay, /restartProbeIfReady\(\)[\s\S]*?int generation = \+\+pageGeneration;[\s\S]*?armStartupWatchdog\(generation, STARTUP_WATCHDOG_MS\)/);
  assert.match(overlay, /runStartupWatchdog\(int generation[\s\S]*?generation != pageGeneration[\s\S]*?ensureStartupWatchdogArmed/);
  assert.match(overlay, /armStartupWatchdog\(int generation, long delayMs\)[\s\S]*?!hostResumed[\s\S]*?!windowFocused[\s\S]*?!activity\.hasWindowFocus\(\)/);
  assert.match(overlay, /onHostResumed\(\)[\s\S]*?cancelWatchdogs\(\);[\s\S]*?restartProbeOrArmWatchdog\(\)/);
  assert.match(overlay, /if \(!hostResumed \|\| !windowFocused \|\| !activity\.hasWindowFocus\(\)\) \{\s*return;\s*\}/);
  assert.match(activity, /setPageRevealedListener\([\s\S]*?notifyWebViewPageRevealed\(webView\)/);
  assert.match(activity, /notifyWebViewPageRevealed\(WebView revealedWebView\)[\s\S]*?new Event\('focus'\)[\s\S]*?native-connectivity-resume[\s\S]*?native_startup_revealed/);
  assert.match(overlay, /private Runnable pageRevealedListener;/);
  assert.match(overlay, /setPageRevealedListener\(Runnable listener\)[\s\S]*?pageRevealedListener = listener/);
  assert.match(overlay, /parent\.removeView\(overlay\);[\s\S]*?notifyPageRevealed\(generation\)/);
  assert.match(overlay, /private Runnable startupWatchdog;[\s\S]*?private Runnable recoveryWatchdog;/);
  assert.match(overlay, /armRecoveryWatchdog\(generation, DiagnosticReason\.WATCHDOG\)/);
  assert.match(overlay, /armRecoveryWatchdog\(generation, DiagnosticReason\.ERROR\)[\s\S]*?evaluateJavascript\(READINESS_PROBE/);
  assert.match(overlay, /cancelPendingProbe\(\)[\s\S]*?cancelWatchdogs\(\)/);
  assert.match(overlay, /cancelRecoveryWatchdog\(\)[\s\S]*?removeCallbacks\(recoveryWatchdog\)/);
  assert.match(overlay, /retryButton\.setText\("Повторить"\)/);
  assert.match(overlay, /retryButton\.setOnClickListener\([\s\S]*?retryCurrentPage\(\)/);
  assert.match(overlay, /retryCurrentPage\(\)[\s\S]*?webView\.reload\(\)/);
  assert.match(overlay, /IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS/);
  assert.match(overlay, /restoreWebViewInteraction\(\)[\s\S]*?setImportantForAccessibility\(savedWebViewImportantForAccessibility\)/);
  assert.match(overlay, /retryButton\.requestFocus\(\)[\s\S]*?announceForAccessibility/);
  assert.match(overlay, /new ScrollView\(activity\)/);
  assert.match(overlay, /contentScroll\.setFillViewport\(true\)/);
  assert.match(overlay, /WindowInsetsCompat\.Type\.displayCutout\(\)[\s\S]*?WindowInsetsCompat\.Type\.ime\(\)/);
  assert.match(overlay, /fontScale >= 1\.5f[\s\S]*?availableWidth > availableHeight/);
  assert.match(overlay, /retryButton\.setMinHeight\(dp\(48\)\)/);
  assert.match(overlay, /content\.addView\(retryButton, linearParams\([\s\S]*?ViewGroup\.LayoutParams\.WRAP_CONTENT,[\s\S]*?ViewGroup\.LayoutParams\.WRAP_CONTENT/);
  assert.match(overlay, /NORMAL\("normal"\)[\s\S]*?WATCHDOG\("watchdog"\)[\s\S]*?ERROR\("error"\)/);
  assert.match(overlay, /startup_overlay state=/);
  assert.doesNotMatch(overlay, /Log\.[idwe]\([^\n]*(getUrl|APP_START_URL|APP_SERVER_URL|encodedResult)/);
  assert.match(activity, /onReceivedError\(WebView erroredWebView\)[\s\S]*?lastObservedPageState != PageState\.STARTED[\s\S]*?startupLoadingOverlay\.onPageError\(erroredWebView\)/);
  assert.match(overlay, /private boolean destroyed;/);
  assert.match(overlay, /private boolean visible;/);
  assert.doesNotMatch(overlay, /private boolean dismissed;/);
  assert.match(overlay, /ValueAnimator\.ofFloat\(0f, 360f\)/);
  assert.match(overlay, /animator\.setDuration\(900L\)/);
});

test("release signing uses only an external credentials file", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  assert.match(gradle, /COPPER_RELEASE_KEYSTORE_PROPERTIES/);
  assert.match(gradle, /CopperResourcesKeys\/keystore-credentials\.txt/);
  assert.match(gradle, /Release keystore was not found/);
  assert.doesNotMatch(gradle, /storePassword\s+["'][^"']+["']/);
  assert.doesNotMatch(gradle, /keyPassword\s+["'][^"']+["']/);
});

test("native updater is profile-driven, deferrable and verifies the APK", () => {
  const javaRoot = resolve(root, "android", "app", "src", "main", "java", "ru", "copperresources", "mobile");
  const activity = readFileSync(resolve(javaRoot, "MainActivity.java"), "utf8");
  const updater = readFileSync(resolve(javaRoot, "AppUpdateManager.java"), "utf8");
  const manifest = readFileSync(resolve(root, "android", "app", "src", "main", "AndroidManifest.xml"), "utf8");
  const buildScript = readFileSync(resolve(root, "scripts", "build-android.mjs"), "utf8");

  assert.match(activity, /BuildConfig\.IN_APP_UPDATER_ENABLED/);
  assert.match(activity, /new AppUpdateManager\(this\)/);
  assert.match(activity, /appUpdateManager\.onPageLoaded\(loadedWebView\)/);
  assert.match(updater, /BuildConfig\.UPDATE_MANIFEST_URL/);
  assert.match(updater, /setNegativeButton\("Позже"/);
  assert.match(updater, /setPositiveButton\("Обновить сейчас"/);
  assert.match(updater, /DEFERRED_VERSION_CODE/);
  assert.match(updater, /data-native-app-version/);
  assert.match(updater, /Update checksum mismatch/);
  assert.match(updater, /Update signing certificate mismatch/);
  assert.match(updater, /activity\.getPackageName\(\)\.equals\(archive\.packageName\)/);
  assert.match(updater, /!hostResumed \|\| !pageLoaded/);
  assert.match(activity, /onPageStarted\(loadingWebView\)[\s\S]*?appUpdateManager\.onPageStarted\(loadingWebView\)/);
  assert.match(manifest, /android\.permission\.REQUEST_INSTALL_PACKAGES/);
  assert.doesNotMatch(manifest, /android\.permission\.UPDATE_PACKAGES_WITHOUT_USER_ACTION/);
  assert.match(buildScript, /createHash\("sha256"\)/);
  assert.match(buildScript, /updateManifest/);
  assert.match(buildScript, /properties\.inAppUpdaterEnabled !== "false"/);
  assert.match(buildScript, /const publicApkName = `\$\{appProfileId\}-\$\{versionName\}\.apk`/);
  assert.match(buildScript, /apkUrl: `\$\{apkBaseUrl\}\$\{publicApkName\}`/);
  assert.doesNotMatch(buildScript, /apkUrl:[^\n]*versionCode/);
});

test("profiles without the sideload updater remove package-installer permission", () => {
  const gradle = readFileSync(resolve(root, "android", "app", "build.gradle"), "utf8");
  for (const profileName of [
    "excavator_qa",
    "excavator_rustore_qa",
    "excavator_rustore",
    "driver_qa",
    "driver_rustore_qa",
    "driver_rustore",
  ]) {
    const overlay = readFileSync(
      resolve(root, "profiles", profileName, "AndroidManifest.xml"),
      "utf8"
    );
    assert.match(overlay, /REQUEST_INSTALL_PACKAGES/);
    assert.match(overlay, /tools:node="remove"/);
  }
  assert.match(gradle, /manifest\.srcFile\(profileManifest\)/);
});
