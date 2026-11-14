import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {mkdtempSync, mkdirSync, readdirSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {dirname, join, resolve} from "node:path";
import test from "node:test";

const root = resolve(import.meta.dirname, "..");
const javaRoot = process.env.SSE_NATIVE_JAVA_ROOT
    ? resolve(process.env.SSE_NATIVE_JAVA_ROOT)
    : resolve(root, "android/app/src/main/java/ru/copperresources/mobile");

const sources = {
    "android/content/Context.java": `
package android.content;
public abstract class Context {
    public static final int MODE_PRIVATE = 0;
    public Context getApplicationContext() { return this; }
    public abstract SharedPreferences getSharedPreferences(String name, int mode);
}
`,
    "android/content/SharedPreferences.java": `
package android.content;
public interface SharedPreferences {
    String getString(String key, String fallback);
    long getLong(String key, long fallback);
    Editor edit();
    interface Editor {
        Editor putString(String key, String value);
        Editor putLong(String key, long value);
        Editor remove(String key);
        boolean commit();
    }
}
`,
    "com/getcapacitor/JSObject.java": `
package com.getcapacitor;
public class JSObject {
    public JSObject put(String key, Object value) { return this; }
}
`,
    "org/json/JSONArray.java": `
package org.json;
public class JSONArray {
    public int length() { return 0; }
    public Object opt(int index) { return null; }
}
`,
    "org/json/JSONObject.java": `
package org.json;
public class JSONObject {
    public JSONObject() {}
    public JSONObject(String ignored) {}
    public JSONObject optJSONObject(String name) { return null; }
    public JSONArray optJSONArray(String name) { return null; }
    public String optString(String name, String fallback) { return fallback; }
}
`,
    "ru/copperresources/mobile/BuildConfig.java": `
package ru.copperresources.mobile;
final class BuildConfig { static final String APP_PROFILE_ID = "driver"; }
`,
    "ru/copperresources/mobile/ConnectivityForegroundService.java": `
package ru.copperresources.mobile;
final class ConnectivityForegroundService { static final String PREFS_NAME = "connection"; }
`,
    "ru/copperresources/mobile/AppNotifications.java": `
package ru.copperresources.mobile;
final class AppNotifications {
    static boolean showOperationalAlert(android.content.Context c, String t, String b) { return true; }
}
`,
    "ru/copperresources/mobile/OperationalVoicePlayer.java": `
package ru.copperresources.mobile;
final class OperationalVoicePlayer {
    static boolean accept = true;
    static int queued = 0;
    static boolean playSequence(android.content.Context c, String cue, String[] voices, boolean focus, long delay) {
        if (!accept) return false;
        queued += 1;
        return true;
    }
}
`,
    "ru/copperresources/mobile/OperationalVoiceBehaviorHarness.java": `
package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;

public final class OperationalVoiceBehaviorHarness {
    private static final String HISTORY = "announced_operational_voice_operations";

    public static void main(String[] args) throws Exception {
        if ("assignment-key".equals(System.getenv("SSE_NATIVE_REPRO_MODE"))) {
            reproduceMixedAssignmentKeys();
            return;
        }
        if ("double-qualified".equals(System.getenv("SSE_NATIVE_REPRO_MODE"))) {
            reproduceDoubleQualification();
            return;
        }
        if ("scope-history".equals(System.getenv("SSE_NATIVE_REPRO_MODE"))) {
            reproduceScopeHistoryCorruption();
            return;
        }
        Map<String, MemoryPreferences> stores = new HashMap<>();
        MemoryContext firstProcess = new MemoryContext(stores);
        OperationalDeliveryLedger.syncFromWebView(firstProcess, "user:1|driver|truck:7", "epoch:a", 5L, 0L);

        OperationalVoicePlayer.queued = 0;
        ResultPair first = announce(firstProcess, "assign:42");
        check(first.announced && first.processed, "first operation must be durably queued");
        check(OperationalVoicePlayer.queued == 1, "first operation must queue once");

        ResultPair duplicate = announce(firstProcess, "assign:42");
        check(!duplicate.announced && duplicate.processed && "already_announced".equals(duplicate.reason),
            "duplicate must be processed, not in_progress");
        check(OperationalVoicePlayer.queued == 1, "duplicate must not queue");

        MemoryContext recreatedProcess = new MemoryContext(stores);
        ResultPair afterRestart = announce(recreatedProcess, "assign:42");
        check(!afterRestart.announced && afterRestart.processed, "restart must retain committed ownership");

        String rawLegacy = "assign:legacy";
        String qualifiedLegacy = OperationalDeliveryLedger.qualifyOperationKey(firstProcess, rawLegacy);
        String prefix = qualifiedLegacy.substring(0, qualifiedLegacy.length() - rawLegacy.length());
        firstProcess.preferences().edit().putString(HISTORY, prefix + qualifiedLegacy).commit();
        int beforeLegacy = OperationalVoicePlayer.queued;
        ResultPair legacy = announce(firstProcess, rawLegacy);
        check(!legacy.announced && legacy.processed && "already_announced".equals(legacy.reason),
            "double-qualified pilot history must migrate logically");
        check(OperationalVoicePlayer.queued == beforeLegacy, "legacy duplicate must not queue");
        ResultPair migrationWrite = announce(firstProcess, "assign:legacy-next");
        String migratedHistory = firstProcess.preferences().getString(HISTORY, "");
        check(migrationWrite.announced && migratedHistory.contains(qualifiedLegacy),
            "a successful write must retain the legacy operation");
        check(!migratedHistory.contains(prefix + qualifiedLegacy),
            "a successful write must collapse only the repeated identical prefix");

        Map<String, MemoryPreferences> scopedStores = new HashMap<>();
        MemoryContext scopeA = new MemoryContext(scopedStores);
        OperationalDeliveryLedger.syncFromWebView(scopeA, "user:A|driver|truck:7", "epoch:a", 0L, 0L);
        OperationalVoicePlayer.queued = 0;
        ResultPair scopeAFirst = announce(scopeA, "assign:same");
        String qualifiedScopeA = OperationalDeliveryLedger.qualifyOperationKey(scopeA, "assign:same");
        MemoryContext scopeB = new MemoryContext(scopedStores);
        OperationalDeliveryLedger.syncFromWebView(scopeB, "user:B|driver|truck:8", "epoch:a", 0L, 0L);
        ResultPair scopeBFirst = announce(scopeB, "assign:same");
        String qualifiedScopeB = OperationalDeliveryLedger.qualifyOperationKey(scopeB, "assign:same");
        String crossScopeHistory = scopeB.preferences().getString(HISTORY, "");
        check(scopeAFirst.announced && scopeBFirst.announced && OperationalVoicePlayer.queued == 2,
            "the same semantic operation name in different scopes must remain independent");
        check(crossScopeHistory.contains(qualifiedScopeA) && crossScopeHistory.contains(qualifiedScopeB),
            "a B write must preserve the existing A key without nesting or reassignment");
        check(!crossScopeHistory.contains(qualifiedScopeB.substring(
                0, qualifiedScopeB.length() - "assign:same".length()) + qualifiedScopeA),
            "a foreign A key must not be rewritten as B:A");
        MemoryContext scopeARestart = new MemoryContext(scopedStores);
        OperationalDeliveryLedger.syncFromWebView(scopeARestart, "user:A|driver|truck:7", "epoch:a", 0L, 0L);
        ResultPair scopeAReturn = announce(scopeARestart, "assign:same");
        MemoryContext scopeBRestart = new MemoryContext(scopedStores);
        OperationalDeliveryLedger.syncFromWebView(scopeBRestart, "user:B|driver|truck:8", "epoch:a", 0L, 0L);
        ResultPair scopeBReturn = announce(scopeBRestart, "assign:same");
        check(!scopeAReturn.announced && scopeAReturn.processed
                && !scopeBReturn.announced && scopeBReturn.processed
                && OperationalVoicePlayer.queued == 2,
            "restart and A-B-A-B switching must retain each scope's durable ownership");

        firstProcess.preferences().edit().putString(HISTORY, "").commit();
        OperationalDeliveryLedger.syncFromWebView(firstProcess, "user:1|driver|truck:7", "epoch:a", 5L, 0L);
        OperationalVoicePlayer.queued = 0;
        ResultPair nativeThenWebA = announce(firstProcess, "assign:100");
        ResultPair nativeThenWebB = announce(firstProcess, "assign:100");
        check(nativeThenWebA.announced && nativeThenWebB.processed && !nativeThenWebB.announced,
            "native to WebView must share assign:<id>");
        check(OperationalVoicePlayer.queued == 1, "native to WebView must queue once");

        ResultPair webThenNativeA = announce(firstProcess, "assign:101");
        ResultPair webThenNativeB = announce(firstProcess, "assign:101");
        check(webThenNativeA.announced && webThenNativeB.processed && !webThenNativeB.announced,
            "WebView to native must share assign:<id>");
        check(OperationalVoicePlayer.queued == 2, "second real assignment must not be suppressed");
        ResultPair release = announce(firstProcess, "release:101");
        check(release.announced && release.processed && OperationalVoicePlayer.queued == 3,
            "release is a distinct semantic operation");

        int beforeConcurrent = OperationalVoicePlayer.queued;
        CountDownLatch ready = new CountDownLatch(2);
        CountDownLatch start = new CountDownLatch(1);
        ResultPair[] results = new ResultPair[2];
        Thread left = new Thread(() -> results[0] = concurrentAnnounce(firstProcess, ready, start, "assign:concurrent"));
        Thread right = new Thread(() -> results[1] = concurrentAnnounce(firstProcess, ready, start, "assign:concurrent"));
        left.start(); right.start(); ready.await(); start.countDown(); left.join(); right.join();
        check(results[0].processed && results[1].processed, "both concurrent callers must settle processed");
        check(OperationalVoicePlayer.queued == beforeConcurrent + 1, "concurrent operation must queue once");

        OperationalDeliveryLedger.syncFromWebView(firstProcess, "user:1|driver|truck:7", "epoch:a", 7L, 0L);
        java.lang.reflect.Method prepareHandoff = OperationalDeliveryLedger.class.getDeclaredMethod(
            "prepareWebViewHandoff", Context.class, String.class, String.class, long.class
        );
        OperationalDeliveryLedger.Snapshot pending = (OperationalDeliveryLedger.Snapshot) prepareHandoff.invoke(
            null, firstProcess, "user:1|driver|truck:7", "epoch:a", 9L
        );
        check(pending.appliedVersion == 7L && pending.pendingVersion == 9L,
            "voice handoff preparation must not commit applied cursor");
        OperationalVoicePlayer.accept = false;
        ResultPair failedQueue = announce(firstProcess, "assign:queue-retry");
        check(!failedQueue.announced && !failedQueue.processed && "resource_unavailable".equals(failedQueue.reason),
            "queue failure must remain retryable");
        check(OperationalDeliveryLedger.snapshot(firstProcess).appliedVersion == 7L,
            "queue failure must not advance cursor");
        OperationalVoicePlayer.accept = true;
        ResultPair retry = announce(new MemoryContext(stores), "assign:queue-retry");
        check(retry.announced && retry.processed, "recreated process must retry an unaccepted handoff");
        check(OperationalDeliveryLedger.snapshot(firstProcess).appliedVersion == 7L,
            "durable voice ownership and delivery cursor are separate barriers");
        OperationalDeliveryLedger.syncFromWebView(firstProcess, "user:1|driver|truck:7", "epoch:a", 9L, 0L);
        check(OperationalDeliveryLedger.snapshot(firstProcess).appliedVersion == 9L,
            "cursor commits only after caller confirms processed handoff");

        int beforeReset = OperationalVoicePlayer.queued;
        OperationalDeliveryLedger.syncFromWebView(firstProcess, "user:2|driver|truck:7", "epoch:a", 0L, 0L);
        ResultPair afterContextReset = announce(firstProcess, "assign:100");
        check(afterContextReset.announced && OperationalVoicePlayer.queued == beforeReset + 1,
            "same operation id in a new identity context must be eligible");
        OperationalDeliveryLedger.resetApplied(firstProcess, 0L);
        check(OperationalDeliveryLedger.snapshot(firstProcess).appliedVersion == 0L,
            "authoritative reset must accept zero");

        String held = OperationalVoiceAnnouncer.claimOperation(firstProcess, "assign:held", false);
        check(!held.isEmpty(), "test must acquire claim");
        ResultPair inProgress = announce(firstProcess, "assign:held");
        check(!inProgress.processed && "in_progress".equals(inProgress.reason),
            "active claim is not durable acceptance");
        OperationalVoiceAnnouncer.releaseOperation(firstProcess, held, true);

        Map<String, MemoryPreferences> batchStores = new HashMap<>();
        MemoryContext batchProcess = new MemoryContext(batchStores);
        OperationalDeliveryLedger.syncFromWebView(
            batchProcess, "user:4|excavator|unit:9", "epoch:b", 20L, 21L
        );
        OperationalVoicePlayer.queued = 0;
        ResultPair invalidBatch = announceBatch(batchProcess,
            new OperationalVoiceAnnouncer.Operation("assign:batch-valid", new String[] {"voice"}),
            new OperationalVoiceAnnouncer.Operation("", new String[] {"voice"})
        );
        check(!invalidBatch.announced && !invalidBatch.processed && OperationalVoicePlayer.queued == 0,
            "an invalid semantic batch must not partially queue or claim its valid prefix");
        ResultPair firstBatch = announceBatch(batchProcess,
            new OperationalVoiceAnnouncer.Operation("assign:batch-1", new String[] {"voice"}),
            new OperationalVoiceAnnouncer.Operation("release:batch-2", new String[] {"voice"})
        );
        check(firstBatch.announced && firstBatch.processed && OperationalVoicePlayer.queued == 1,
            "all batch operations must become durable in one accepted queue");
        ResultPair repeatedBatch = announceBatch(new MemoryContext(batchStores),
            new OperationalVoiceAnnouncer.Operation("assign:batch-1", new String[] {"voice"}),
            new OperationalVoiceAnnouncer.Operation("release:batch-2", new String[] {"voice"})
        );
        check(!repeatedBatch.announced && repeatedBatch.processed && OperationalVoicePlayer.queued == 1,
            "a recreated process must recognize every operation in the committed batch");
        String heldBatch = OperationalVoiceAnnouncer.claimOperation(batchProcess, "assign:batch-held", false);
        check(!heldBatch.isEmpty(), "test must acquire a batch claim");
        ResultPair blockedBatch = announceBatch(batchProcess,
            new OperationalVoiceAnnouncer.Operation("assign:batch-free", new String[] {"voice"}),
            new OperationalVoiceAnnouncer.Operation("assign:batch-held", new String[] {"voice"})
        );
        check(!blockedBatch.processed && "in_progress".equals(blockedBatch.reason)
                && OperationalVoicePlayer.queued == 1,
            "one active claim must keep the whole semantic batch retryable without partial audio");
        OperationalVoiceAnnouncer.releaseOperation(batchProcess, heldBatch, true);
        ResultPair retriedBatch = announceBatch(batchProcess,
            new OperationalVoiceAnnouncer.Operation("assign:batch-free", new String[] {"voice"}),
            new OperationalVoiceAnnouncer.Operation("assign:batch-held", new String[] {"voice"})
        );
        check(retriedBatch.announced && retriedBatch.processed && OperationalVoicePlayer.queued == 2,
            "the complete batch must queue after the competing claim is released");

        Map<String, MemoryPreferences> persistenceStores = new HashMap<>();
        MemoryContext persistenceProcess = new MemoryContext(persistenceStores);
        OperationalDeliveryLedger.syncFromWebView(
            persistenceProcess, "user:3|driver|truck:8", "epoch:p", 11L, 12L
        );
        persistenceProcess.preferences().failNextHistoryCommit();
        int beforePersistenceFailure = OperationalVoicePlayer.queued;
        ResultPair persistenceFailure = announce(persistenceProcess, "assign:persistence-retry");
        check(persistenceFailure.announced && !persistenceFailure.processed
                && "persistence_unavailable".equals(persistenceFailure.reason),
            "accepted audio without durable history must keep delivery retryable");
        check(OperationalVoicePlayer.queued == beforePersistenceFailure + 1,
            "persistence failure occurs after one queue acceptance");
        check(OperationalDeliveryLedger.snapshot(persistenceProcess).appliedVersion == 11L,
            "persistence failure must not commit the pending delivery cursor");
        persistenceProcess.preferences().expireClaims();
        ResultPair persistenceRetry = announce(
            new MemoryContext(persistenceStores), "assign:persistence-retry"
        );
        check(persistenceRetry.announced && persistenceRetry.processed,
            "expired failed persistence claim must retry and commit durably");

        System.out.println("PASS first_duplicate_restart_legacy_reset");
        System.out.println("PASS native_web_semantic_assignment_and_concurrency");
        System.out.println("PASS durable_queue_handoff_barrier");
        System.out.println("PASS durable_persistence_failure_retry");
        System.out.println("PASS scoped_history_restart_and_batch_atomicity history="
            + crossScopeHistory.replace("\\n", "|") + " queued=2");
    }

    private static void reproduceMixedAssignmentKeys() {
        MemoryContext context = new MemoryContext(new HashMap<>());
        OperationalDeliveryLedger.syncFromWebView(context, "user:1|driver|truck:7", "epoch:a", 0L, 0L);
        OperationalVoicePlayer.queued = 0;
        OperationalVoiceAnnouncer.Result nativeHeartbeat = OperationalVoiceAnnouncer.announceSequence(
            context, "assignment_notice", new String[] {"voice"}, 17L,
            "driver_assignment", false, "", ""
        );
        ResultPair webView = announce(context, "assign:100");
        check(nativeHeartbeat.announced && webView.announced && OperationalVoicePlayer.queued == 2,
            "version key and semantic key must reproduce duplicate ownership");
        System.out.println("REPRO assignment_mixed_keys_queued=" + OperationalVoicePlayer.queued);
    }

    private static void reproduceDoubleQualification() {
        MemoryContext context = new MemoryContext(new HashMap<>());
        OperationalDeliveryLedger.syncFromWebView(context, "user:1|driver|truck:7", "epoch:a", 0L, 0L);
        OperationalVoicePlayer.queued = 0;
        ResultPair first = announce(context, "assign:42");
        ResultPair duplicate = announce(context, "assign:42");
        String stored = context.preferences().getString(HISTORY, "");
        check(first.announced && first.processed && OperationalVoicePlayer.queued == 1,
            "first operation must queue once");
        check(!duplicate.announced && !duplicate.processed && "in_progress".equals(duplicate.reason),
            "legacy duplicate must reproduce in_progress defect");
        check(stored.matches("^(ctx:[^:]+:){2}assign:42$"),
            "legacy history must contain a double-qualified key");
        System.out.println("REPRO duplicate_processed=" + duplicate.processed
            + " reason=" + duplicate.reason + " queued=" + OperationalVoicePlayer.queued);
        System.out.println("REPRO stored=" + stored);
    }

    private static void reproduceScopeHistoryCorruption() {
        Map<String, MemoryPreferences> stores = new HashMap<>();
        MemoryContext contextA = new MemoryContext(stores);
        OperationalDeliveryLedger.syncFromWebView(contextA, "user:A|driver|truck:7", "epoch:a", 0L, 0L);
        OperationalVoicePlayer.queued = 0;
        ResultPair firstA = announce(contextA, "assign:42");
        ResultPair duplicateA = announce(contextA, "assign:42");

        String qualifiedA = OperationalDeliveryLedger.qualifyOperationKey(contextA, "assign:42");
        OperationalDeliveryLedger.syncFromWebView(contextA, "user:B|driver|truck:8", "epoch:a", 0L, 0L);
        String qualifiedB = OperationalDeliveryLedger.qualifyOperationKey(contextA, "assign:43");
        ResultPair firstB = announce(contextA, "assign:43");
        String storedAfterB = contextA.preferences().getString(HISTORY, "");

        MemoryContext restartedA = new MemoryContext(stores);
        OperationalDeliveryLedger.syncFromWebView(restartedA, "user:A|driver|truck:7", "epoch:a", 0L, 0L);
        ResultPair replayedA = announce(restartedA, "assign:42");

        String prefixB = qualifiedB.substring(0, qualifiedB.length() - "assign:43".length());
        check(firstA.announced && duplicateA.processed && !duplicateA.announced,
            "scope A must initially own assign:42");
        check(firstB.announced, "scope B must own its different assignment");
        check(storedAfterB.contains(prefixB + qualifiedA),
            "reading A history in B must reproduce nested foreign qualification");
        check(replayedA.announced && replayedA.processed && OperationalVoicePlayer.queued == 3,
            "return to A must reproduce duplicate queue after foreign history corruption");
        System.out.println("REPRO scope_history_after_B=" + storedAfterB.replace("\\n", "|"));
        System.out.println("REPRO scope_A_return_announced=" + replayedA.announced
            + " processed=" + replayedA.processed + " queued=" + OperationalVoicePlayer.queued);
    }

    private static ResultPair concurrentAnnounce(
            Context context, CountDownLatch ready, CountDownLatch start, String key) {
        try {
            ready.countDown();
            start.await();
            return announce(context, key);
        } catch (Exception error) {
            throw new RuntimeException(error);
        }
    }

    private static ResultPair announce(Context context, String key) {
        OperationalVoiceAnnouncer.Result result = OperationalVoiceAnnouncer.announceOperations(
            context,
            "assignment_notice",
            Arrays.asList(new OperationalVoiceAnnouncer.Operation(key, new String[] {"voice"})),
            false,
            "",
            ""
        );
        return new ResultPair(result.announced, result.processed, result.reason);
    }

    private static ResultPair announceBatch(
            Context context, OperationalVoiceAnnouncer.Operation... operations) {
        OperationalVoiceAnnouncer.Result result = OperationalVoiceAnnouncer.announceOperations(
            context, "assignment_notice", Arrays.asList(operations), false, "", ""
        );
        return new ResultPair(result.announced, result.processed, result.reason);
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static final class ResultPair {
        final boolean announced;
        final boolean processed;
        final String reason;
        ResultPair(boolean announced, boolean processed, String reason) {
            this.announced = announced;
            this.processed = processed;
            this.reason = reason;
        }
    }

    private static final class MemoryContext extends Context {
        private final Map<String, MemoryPreferences> stores;
        MemoryContext(Map<String, MemoryPreferences> stores) { this.stores = stores; }
        @Override public SharedPreferences getSharedPreferences(String name, int mode) {
            return stores.computeIfAbsent(name, ignored -> new MemoryPreferences());
        }
        MemoryPreferences preferences() {
            return (MemoryPreferences) getSharedPreferences(ConnectivityForegroundService.PREFS_NAME, MODE_PRIVATE);
        }
    }

    private static final class MemoryPreferences implements SharedPreferences {
        private final Map<String, Object> values = new HashMap<>();
        private boolean failNextHistoryCommit;
        void failNextHistoryCommit() { failNextHistoryCommit = true; }
        void expireClaims() {
            values.replaceAll((key, value) -> key.startsWith("operational_voice_claim_") ? 1L : value);
        }
        @Override public String getString(String key, String fallback) {
            Object value = values.get(key); return value instanceof String ? (String) value : fallback;
        }
        @Override public long getLong(String key, long fallback) {
            Object value = values.get(key); return value instanceof Number ? ((Number) value).longValue() : fallback;
        }
        @Override public Editor edit() {
            return new Editor() {
                private final Map<String, Object> updates = new HashMap<>();
                @Override public Editor putString(String key, String value) { updates.put(key, value); return this; }
                @Override public Editor putLong(String key, long value) { updates.put(key, value); return this; }
                @Override public Editor remove(String key) { updates.put(key, this); return this; }
                @Override public boolean commit() {
                    if (failNextHistoryCommit && updates.containsKey(HISTORY)) {
                        failNextHistoryCommit = false;
                        return false;
                    }
                    updates.forEach((key, value) -> { if (value == this) values.remove(key); else values.put(key, value); });
                    return true;
                }
            };
        }
    }
}
`,
};

test("real native ledger and announcer enforce semantic exactly-once queue ownership", () => {
    const work = mkdtempSync(join(tmpdir(), "sse-native-jvm-"));
    const classes = join(work, "classes");
    mkdirSync(classes, {recursive: true});
    try {
        for (const [relative, content] of Object.entries(sources)) {
            const target = join(work, relative);
            mkdirSync(dirname(target), {recursive: true});
            writeFileSync(target, content.trimStart(), "utf8");
        }
        const javaFiles = [];
        const collect = (folder) => {
            for (const entry of readdirSync(folder, {withFileTypes: true})) {
                const target = join(folder, entry.name);
                if (entry.isDirectory()) collect(target);
                else if (entry.name.endsWith(".java")) javaFiles.push(target);
            }
        };
        collect(work);
        javaFiles.push(
            resolve(javaRoot, "OperationalDeliveryLedger.java"),
            resolve(javaRoot, "OperationalVoiceAnnouncer.java")
        );
        execFileSync("javac", ["-encoding", "UTF-8", "-d", classes, ...javaFiles], {stdio: "pipe"});
        const output = execFileSync(
            "java",
            ["-ea", "-cp", classes, "ru.copperresources.mobile.OperationalVoiceBehaviorHarness"],
            {encoding: "utf8"}
        );
        if (process.env.SSE_NATIVE_REPRO_MODE === "assignment-key") {
            process.stdout.write(output);
            assert.match(output, /REPRO assignment_mixed_keys_queued=2/);
        } else if (process.env.SSE_NATIVE_REPRO_MODE === "double-qualified") {
            process.stdout.write(output);
            assert.match(output, /REPRO duplicate_processed=false reason=in_progress queued=1/);
            assert.match(output, /REPRO stored=(ctx:[^:]+:){2}assign:42/);
        } else if (process.env.SSE_NATIVE_REPRO_MODE === "scope-history") {
            process.stdout.write(output);
            assert.match(output, /REPRO scope_history_after_B=ctx:[^:]+:ctx:[^:]+:assign:42\|ctx:[^:]+:assign:43/);
            assert.match(output, /REPRO scope_A_return_announced=true processed=true queued=3/);
        } else {
            process.stdout.write(output);
            assert.match(output, /PASS first_duplicate_restart_legacy_reset/);
            assert.match(output, /PASS native_web_semantic_assignment_and_concurrency/);
            assert.match(output, /PASS durable_queue_handoff_barrier/);
            assert.match(output, /PASS durable_persistence_failure_retry/);
            assert.match(output, /PASS scoped_history_restart_and_batch_atomicity/);
        }
    } finally {
        rmSync(work, {recursive: true, force: true});
    }
});
