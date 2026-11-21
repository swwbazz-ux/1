import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {resolve} from "node:path";
import test from "node:test";

const root = resolve(import.meta.dirname, "..");
const javaRoot = resolve(root, "android/app/src/main/java/ru/copperresources/mobile");
const read = (relative) => readFileSync(resolve(root, relative), "utf8");
const java = (name) => readFileSync(resolve(javaRoot, name), "utf8");

test("pilot APK profiles are isolated, local-only, and disable push/updater", () => {
    for (const [profile, appId, host] of [
        ["driver_sse_pilot", "ru.copperresources.driver.ssepilot", "driver.sse-pilot.local"],
        ["excavator_sse_pilot", "ru.copperresources.excavator.ssepilot", "excavator.sse-pilot.local"],
    ]) {
        const properties = read(`profiles/${profile}/app.properties`);
        assert.match(properties, new RegExp(`applicationId=${appId.replaceAll(".", "\\.")}`));
        assert.match(properties, new RegExp(`serverUrl=https://${host.replaceAll(".", "\\.")}/`));
        assert.match(properties, /nativePushEnabled=false/);
        assert.match(properties, /inAppUpdaterEnabled=false/);
        assert.match(properties, /syncTokenEnv=\s*(?:\r?\n|$)/);
        assert.doesNotMatch(properties, /driverform\.ru/);
    }
});

test("native applied cursor is separate from transport high-water", () => {
    const service = java("ConnectivityForegroundService.java");
    const ledger = java("OperationalDeliveryLedger.java");
    assert.match(service, /OperationalDeliveryLedger\.appliedVersion\(this\)/);
    assert.match(service, /prepareServerResponse\(this, result\.body, serverVersion\)/);
    assert.match(service, /commitApplied\(this, serverVersion\)/);
    assert.match(service, /putLong\("last_server_version", serverVersion/);
    assert.match(ledger, /operational_context_v1/);
    assert.match(ledger, /operational_journal_epoch_v1/);
    assert.match(ledger, /worker_equipment_ids/);
    assert.match(ledger, /session_revision/);
});

test("voice arbiter has persistent serial claim commit release and documents crash window", () => {
    const announcer = java("OperationalVoiceAnnouncer.java");
    const ledger = java("OperationalDeliveryLedger.java");
    const driver = java("DriverDumpPointAnnouncer.java");
    assert.match(announcer, /static synchronized String claimOperation/);
    assert.match(announcer, /static synchronized boolean commitOperation/);
    assert.match(announcer, /static synchronized void releaseOperation/);
    assert.match(announcer, /crash after playback starts but before commit/i);
    assert.match(announcer, /claimOperation\(appContext, qualifiedKey, true\)/);
    assert.match(announcer, /readOperationOrder\(appContext, preferences\)/);
    assert.match(announcer, /if \(!historyCommitted\)/);
    assert.match(announcer, /queuedButUncommitted/);
    assert.match(ledger, /while \(normalized\.startsWith\(prefix\)\)/);
    assert.match(driver, /"driver-dump:" \+ tripId \+ ":"/);
    assert.match(driver, /commitOperation\(appContext, claimToken, true\)/);
});

test("native Driver heartbeat uses the same semantic assignment keys as WebView and truth", () => {
    const service = java("ConnectivityForegroundService.java");
    const start = service.indexOf("private VoiceAttempt showLatestAssignmentAlert");
    const end = service.indexOf("private VoiceAttempt showLatestExcavatorAssignmentAlerts", start);
    const method = service.slice(start, end);

    assert.match(method, /event\.optString\("object_id", ""\)/);
    assert.match(method, /"assign:" \+ assignmentId/);
    assert.match(method, /"release:" \+ assignmentId/);
    assert.match(method, /OperationalVoiceAnnouncer\.announceOperations/);
    assert.doesNotMatch(method, /announceSequence\([\s\S]*?roleCode \+ "_assignment"/);
});

test("native voice handoff reports durable processed state before WebView cursor commit", () => {
    const sounds = java("NativeSoundPlugin.java");
    const ledger = java("OperationalDeliveryLedger.java");
    const background = java("BackgroundConnectionPlugin.java");

    assert.match(sounds, /OperationalDeliveryLedger\.prepareWebViewHandoff/);
    assert.doesNotMatch(
        sounds.slice(sounds.indexOf("private void syncDeliveryContext")),
        /syncFromWebView/
    );
    assert.match(sounds, /put\("processed", result\.processed\)/);
    assert.match(ledger, /static synchronized Snapshot prepareWebViewHandoff/);
    assert.match(ledger, /current\.appliedVersion,[\s\S]*pending/);
    assert.match(background, /authoritativeReset/);
    assert.match(background, /resetAccepted/);
    assert.match(background, /isNonNegativeInteger\(resetValue\)/);
});

test("native equipment handoff requires semantic keys and rejects partial batches", () => {
    const sounds = java("NativeSoundPlugin.java");
    const singleStart = sounds.indexOf("public void announceEquipment(PluginCall call)");
    const batchStart = sounds.indexOf("public void announceEquipmentBatch(PluginCall call)", singleStart);
    const helperStart = sounds.indexOf("private String[] equipmentVoiceSequence", batchStart);
    const single = sounds.slice(singleStart, batchStart);
    const batch = sounds.slice(batchStart, helperStart);

    assert.match(single, /operationKey\.isEmpty\(\)[\s\S]*?REASON_SEMANTIC_KEY_REQUIRED/);
    assert.match(single, /announceOperations/);
    assert.doesNotMatch(single, /announceSequence/);
    assert.match(batch, /operationKey\.isEmpty\(\)[\s\S]*?REASON_SEMANTIC_KEY_REQUIRED/);
    assert.match(batch, /operations\.size\(\) != items\.length\(\)/);
    assert.ok(
        batch.indexOf("if (!invalidReason.isEmpty()") < batch.indexOf("OperationalVoiceAnnouncer.announceOperations"),
        "the native plugin must reject an incomplete batch before queue arbitration"
    );
    assert.match(batch, /put\("processed", false\)/);
    assert.match(batch, /requestedOperationCount/);
});

test("local pilot build flag makes FCM an unavailable stub", () => {
    const gradle = read("android/app/build.gradle");
    const push = java("NativePushPlugin.java");
    const messaging = java("CopperFirebaseMessagingService.java");
    assert.match(gradle, /buildConfigField "boolean", "NATIVE_PUSH_ENABLED"/);
    assert.match(push, /!BuildConfig\.NATIVE_PUSH_ENABLED/);
    assert.match(messaging, /!BuildConfig\.NATIVE_PUSH_ENABLED/);
});

test("native Driver batch arbitration lets terminal actions cancel stale voice and later replacement win", () => {
    const service = java("ConnectivityForegroundService.java");
    const methodStart = service.indexOf("private VoiceAttempt showLatestAssignmentAlert");
    const methodEnd = service.indexOf("private VoiceAttempt showLatestExcavatorAssignmentAlerts", methodStart);
    const method = service.slice(methodStart, methodEnd);

    assert.match(method, /pending_cancelled\/assignment_applied\/release_pending/);
    assert.match(method, /selectedVersion\s*=\s*version;\s*selectedVoices\s*=\s*voices;/s);
    assert.doesNotMatch(method, /if\s*\(voices\.length\s*>\s*0\)/);
    assert.ok(
        method.indexOf('"assignment_pending".equals(action)') < method.indexOf("selectedVersion = version;"),
        "a later replacement is selected only after every newer action has taken ownership"
    );
});

test("native recovery pages across a truncated foreign window before voice arbitration", () => {
    const service = java("ConnectivityForegroundService.java");
    const buffer = java("OperationalRecoveryBuffer.java");
    const truncation = service.indexOf("boolean eventsTruncated = readEventsTruncated(result.body)");
    const voice = service.indexOf("showLatestDriverDumpPointAlert(", truncation);
    const commit = service.indexOf("OperationalDeliveryLedger.commitApplied(this, serverVersion)", voice);

    assert.ok(truncation >= 0 && truncation < voice);
    assert.match(service, /readPageCursor\(result\.body\)/);
    assert.match(service, /OperationalDeliveryLedger\.commitApplied\(this, pageCursor\)/);
    assert.match(service, /OperationalRecoveryBuffer\.append\(this, result\.body/);
    assert.match(service, /OperationalRecoveryBuffer\.merge\(this, result\.body\)/);
    assert.match(service.slice(truncation, voice), /voiceEligible[\s\S]+relevant\s*&&\s*caughtUp/);
    assert.match(service.slice(voice, commit + 100), /if\s*\(caughtUp\s*&&\s*voiceProcessed\)/);
    assert.match(buffer, /TreeMap<Long, JSONObject>/);
    assert.match(buffer, /put\("events", merged\)/);
    assert.match(buffer, /operational_recovery_voice_eligible_v1/);
});

test("native reset discards replay and commits only matching authoritative truth", () => {
    const service = java("ConnectivityForegroundService.java");
    const resetStart = service.indexOf("if (operationalResetRequired)");
    const replayStart = service.indexOf("boolean eventsTruncated = readEventsTruncated(result.body)", resetStart);
    const resetBranch = service.slice(resetStart, replayStart);
    const truthStart = service.indexOf("private boolean applyAuthoritativeVoiceState");
    const truthEnd = service.indexOf("private boolean announceCurrentDriverTruth", truthStart);
    const truth = service.slice(truthStart, truthEnd);

    assert.ok(resetStart >= 0 && resetStart < replayStart);
    assert.ok(
        resetBranch.indexOf("OperationalRecoveryBuffer.clear(this)")
            < resetBranch.indexOf("applyAuthoritativeVoiceState("),
        "a reset must discard any replay prefix before truth arbitration"
    );
    assert.match(resetBranch, /truthApplied\s*&&\s*resetToVersion\s*>=\s*0L\s*&&\s*resetToVersion\s*==\s*serverVersion/);
    assert.match(resetBranch, /resetApplied\(this, resetToVersion\)/);
    assert.match(resetBranch, /last_operational_reset_reason/);
    assert.match(service, /ConnectionState\.recordTransport\([\s\S]*?operationalResetRequired[\s\S]*?operationalResetReason[\s\S]*?operationalResetToVersion/);
    assert.match(service, /return root\.optBoolean\("reset_required", false\)/);
    assert.match(truth, /schema_version/);
    assert.match(truth, /authoritative/);
    assert.match(truth, /domain_tables/);
    assert.match(truth, /observed_at_version/);
    assert.match(truth, /voice_operations/);
    assert.doesNotMatch(truth, /optJSONArray\("events"\)/);
    const ledger = java("OperationalDeliveryLedger.java");
    assert.match(ledger, /static synchronized Snapshot resetApplied[\s\S]*?Math\.max\(0L, version\)[\s\S]*?0L,\s*0L/);
});

test("authoritative Driver truth announces only current assignment or dump-point semantic keys", () => {
    const service = java("ConnectivityForegroundService.java");
    const start = service.indexOf("private boolean announceCurrentDriverTruth");
    const end = service.indexOf("private boolean announceCurrentExcavatorTruth", start);
    const method = service.slice(start, end);

    assert.match(method, /"driver_assignment"\.equals\(kind\)/);
    assert.match(method, /"pending"\.equals\(state\)\s*\|\|\s*"active"\.equals\(state\)/);
    assert.match(method, /"assign:" \+ assignmentId/);
    assert.match(method, /new OperationalVoiceAnnouncer\.Operation\(\s*operationKey/s);
    assert.match(method, /"driver_dump_point"\.equals\(kind\)/);
    assert.match(method, /"dump:" \+ tripId \+ ":id-" \+ dumpPointId/);
    assert.match(method, /DriverDumpPointAnnouncer\.announce/);
    assert.doesNotMatch(method, /pending_cancelled|release_pending|truck_loaded_cancel/);
    assert.match(method, /if \(invalidKnownOperation\) return false/);
});

test("empty authoritative operations suppress cancelled or replaced stale voice", () => {
    const service = java("ConnectivityForegroundService.java");
    const start = service.indexOf("private boolean applyAuthoritativeVoiceState");
    const end = service.indexOf("private boolean announceCurrentDriverTruth", start);
    const truth = service.slice(start, end);
    const resetStart = service.indexOf("if (operationalResetRequired)");
    const resetEnd = service.indexOf("boolean eventsTruncated = readEventsTruncated(result.body)", resetStart);
    const reset = service.slice(resetStart, resetEnd);

    assert.match(truth, /!voiceEligible \|\| operations\.length\(\) == 0[\s\S]*?return true/);
    assert.match(reset, /scheduleHeartbeat\(truthApplied \? BuildConfig\.HEARTBEAT_INTERVAL_MS : 2_000L\)/);
    assert.match(reset, /return;/);
    assert.doesNotMatch(reset, /showLatestDriverDumpPointAlert|showLatestAssignmentAlert/);
});

test("authoritative Excavator truth uses current accepted assignment semantic keys", () => {
    const service = java("ConnectivityForegroundService.java");
    const start = service.indexOf("private boolean announceCurrentExcavatorTruth");
    const end = service.indexOf("private VoiceAttempt showLatestDriverDumpPointAlert", start);
    const method = service.slice(start, end);

    assert.match(method, /excavator_truck_assigned/);
    assert.match(method, /"excavator-assignment:assign:" \+ assignmentId \+ ":" \+ excavatorId/);
    assert.match(method, /OperationalVoiceAnnouncer\.announceOperations/);
    assert.match(method, /return result\.processed/);
});

test("player failure and active claim keep both reset and catch-up cursors retryable", () => {
    const service = java("ConnectivityForegroundService.java");
    const announcer = java("OperationalVoiceAnnouncer.java");
    const dump = java("DriverDumpPointAnnouncer.java");
    const catchupStart = service.indexOf("boolean voiceProcessed = true");
    const catchupEnd = service.indexOf("if (shiftCloseFlush ==", catchupStart);
    const catchup = service.slice(catchupStart, catchupEnd);

    assert.match(announcer, /REASON_IN_PROGRESS = "in_progress"/);
    assert.match(announcer, /claimInProgress[\s\S]*?Result\.rejected\(REASON_IN_PROGRESS, false\)/);
    assert.match(announcer, /Result\.rejected\(REASON_RESOURCE_UNAVAILABLE, false\)/);
    assert.match(announcer, /public final boolean processed/);
    assert.match(dump, /REASON_IN_PROGRESS, false/);
    assert.match(dump, /resource_unavailable", false/);
    assert.match(catchup, /if \(caughtUp && voiceProcessed\)[\s\S]*?commitApplied\(this, serverVersion\)/);
});

test("WebView and reset truth share persistent Driver assignment operation keys", () => {
    const driverVoice = read("../../СИСТЕМА_MVP/backend/static/js/driver-shift-voice-v1.js");
    const sounds = read("../../СИСТЕМА_MVP/backend/static/js/mobile-operational-sounds-v1.js");
    const nativeSound = java("NativeSoundPlugin.java");

    assert.match(driverVoice, /operationKey:\s*opKey/);
    assert.match(sounds, /if \(operationKey\) nativeDetails\.operationKey = operationKey/);
    assert.match(nativeSound, /call\.getString\("operationKey", ""\)/);
    assert.match(nativeSound, /announceOperations\([\s\S]*?new OperationalVoiceAnnouncer\.Operation\(/);
});
