package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import com.getcapacitor.JSObject;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Persistent processed-position ledger shared by the native heartbeat and WebView.
 *
 * <p>The server high-water mark and FCM version are wake hints. Only a caller that
 * has finished local processing may call {@link #commitApplied}. Context includes
 * user/access, role, equipment/shift and journal epoch, so a cursor can never leak
 * across login or journal reset.</p>
 */
final class OperationalDeliveryLedger {
    private static final String CONTEXT_KEY = "operational_context_v1";
    private static final String JOURNAL_EPOCH = "operational_journal_epoch_v1";
    private static final String APPLIED_VERSION = "operational_applied_version_v1";
    private static final String PENDING_VERSION = "operational_pending_version_v1";
    private static final String PENDING_SINCE = "operational_pending_since_v1";

    private OperationalDeliveryLedger() {}

    static synchronized Snapshot snapshot(Context context) {
        SharedPreferences preferences = preferences(context);
        return new Snapshot(
            preferences.getString(CONTEXT_KEY, ""),
            preferences.getString(JOURNAL_EPOCH, ""),
            preferences.getLong(APPLIED_VERSION, 0L),
            preferences.getLong(PENDING_VERSION, 0L),
            preferences.getLong(PENDING_SINCE, 0L)
        );
    }

    static synchronized Snapshot syncFromWebView(
            Context context,
            String contextKey,
            String journalEpoch,
            long appliedVersion,
            long pendingVersion) {
        Snapshot current = snapshot(context);
        String normalizedContext = normalize(contextKey);
        String normalizedEpoch = normalize(journalEpoch);
        boolean contextChanged = !normalizedContext.isEmpty()
            && !normalizedContext.equals(current.contextKey);
        boolean epochChanged = !normalizedEpoch.isEmpty()
            && !current.journalEpoch.isEmpty()
            && !normalizedEpoch.equals(current.journalEpoch);
        if (contextChanged || epochChanged) {
            current = new Snapshot(normalizedContext, normalizedEpoch, 0L, 0L, 0L);
        } else {
            if (current.contextKey.isEmpty() && !normalizedContext.isEmpty()) {
                current = current.withContext(normalizedContext);
            }
            if (!normalizedEpoch.isEmpty()) current = current.withEpoch(normalizedEpoch);
        }
        long applied = Math.max(current.appliedVersion, Math.max(0L, appliedVersion));
        long pending = Math.max(Math.max(0L, pendingVersion), current.pendingVersion);
        if (pending <= applied) pending = 0L;
        return save(context, new Snapshot(
            current.contextKey,
            current.journalEpoch,
            applied,
            pending,
            pending > 0L ? System.currentTimeMillis() : 0L
        ));
    }

    /**
     * Records the scope and the fact that WebView is attempting to hand off a
     * response, without accepting its processed cursor. NativeSound calls this
     * before queue arbitration. The WebView may commit {@code appliedVersion}
     * through {@link #syncFromWebView} only after every addressed operation has
     * reached a durable owner.
     */
    static synchronized Snapshot prepareWebViewHandoff(
            Context context,
            String contextKey,
            String journalEpoch,
            long pendingVersion) {
        Snapshot current = snapshot(context);
        String normalizedContext = normalize(contextKey);
        String normalizedEpoch = normalize(journalEpoch);
        boolean contextChanged = !normalizedContext.isEmpty()
            && !normalizedContext.equals(current.contextKey);
        boolean epochChanged = !normalizedEpoch.isEmpty()
            && !current.journalEpoch.isEmpty()
            && !normalizedEpoch.equals(current.journalEpoch);
        if (contextChanged || epochChanged) {
            current = new Snapshot(normalizedContext, normalizedEpoch, 0L, 0L, 0L);
        } else {
            if (current.contextKey.isEmpty() && !normalizedContext.isEmpty()) {
                current = current.withContext(normalizedContext);
            }
            if (!normalizedEpoch.isEmpty()) current = current.withEpoch(normalizedEpoch);
        }
        long pending = Math.max(current.pendingVersion, Math.max(0L, pendingVersion));
        if (pending <= current.appliedVersion) pending = 0L;
        return save(context, new Snapshot(
            current.contextKey,
            current.journalEpoch,
            current.appliedVersion,
            pending,
            pending > 0L ? System.currentTimeMillis() : 0L
        ));
    }

    static synchronized Snapshot prepareServerResponse(Context context, String responseBody, long version) {
        Snapshot current = snapshot(context);
        ServerContext server = parseServerContext(responseBody);
        boolean contextChanged = !server.contextKey.isEmpty()
            && !server.contextKey.equals(current.contextKey);
        boolean epochChanged = !server.journalEpoch.isEmpty()
            && !current.journalEpoch.isEmpty()
            && !server.journalEpoch.equals(current.journalEpoch);
        if (contextChanged || epochChanged) {
            current = new Snapshot(server.contextKey, server.journalEpoch, 0L, 0L, 0L);
        } else {
            if (current.contextKey.isEmpty() && !server.contextKey.isEmpty()) {
                current = current.withContext(server.contextKey);
            }
            if (!server.journalEpoch.isEmpty()) current = current.withEpoch(server.journalEpoch);
        }
        long pending = Math.max(current.pendingVersion, Math.max(0L, version));
        return save(context, new Snapshot(
            current.contextKey,
            current.journalEpoch,
            current.appliedVersion,
            pending,
            pending > 0L ? System.currentTimeMillis() : 0L
        ));
    }

    static synchronized Snapshot commitApplied(Context context, long version) {
        Snapshot current = snapshot(context);
        long applied = Math.max(current.appliedVersion, Math.max(0L, version));
        long pending = current.pendingVersion <= applied ? 0L : current.pendingVersion;
        return save(context, new Snapshot(
            current.contextKey,
            current.journalEpoch,
            applied,
            pending,
            pending > 0L ? current.pendingSince : 0L
        ));
    }

    /** Exact authoritative baseline used only after a validated reset truth snapshot. */
    static synchronized Snapshot resetApplied(Context context, long version) {
        Snapshot current = snapshot(context);
        return save(context, new Snapshot(
            current.contextKey,
            current.journalEpoch,
            Math.max(0L, version),
            0L,
            0L
        ));
    }

    static synchronized long appliedVersion(Context context) {
        return snapshot(context).appliedVersion;
    }

    static synchronized String qualifyOperationKey(Context context, String operationKey) {
        String normalized = normalize(operationKey);
        if (normalized.isEmpty()) return "";
        Snapshot state = snapshot(context);
        String scope = state.contextKey + "|" + state.journalEpoch;
        if (scope.equals("|")) scope = BuildConfig.APP_PROFILE_ID + "|unscoped";
        String prefix = "ctx:" + sha256(scope).substring(0, 20) + ":";
        // Idempotence is part of the persistent key contract. Early pilot
        // builds accidentally stored prefix + prefix + operation; collapse
        // that legacy form so it remains recognizable after the fix.
        while (normalized.startsWith(prefix)) {
            normalized = normalized.substring(prefix.length());
        }
        return normalized.isEmpty() ? "" : prefix + normalized;
    }

    /**
     * Reads a persisted operation without moving ownership into the active
     * identity scope. Unscoped records from the first pilot build are migrated
     * into the current scope. A repeated identical scope prefix is the known
     * legacy double-qualification form and is collapsed. A different scope is
     * preserved byte-for-byte so switching A -> B -> A cannot rewrite A as B:A.
     */
    static synchronized String normalizeStoredOperationKey(Context context, String operationKey) {
        String normalized = normalize(operationKey);
        if (normalized.isEmpty()) return "";
        String storedPrefix = scopedOperationPrefix(normalized);
        if (storedPrefix.isEmpty()) return qualifyOperationKey(context, normalized);
        String operation = normalized.substring(storedPrefix.length());
        while (operation.startsWith(storedPrefix)) {
            operation = operation.substring(storedPrefix.length());
        }
        return operation.isEmpty() ? "" : storedPrefix + operation;
    }

    private static String scopedOperationPrefix(String operationKey) {
        if (operationKey == null || !operationKey.startsWith("ctx:")) return "";
        int separator = operationKey.indexOf(':', 4);
        if (separator != 24) return "";
        for (int index = 4; index < separator; index += 1) {
            char item = operationKey.charAt(index);
            if (!((item >= '0' && item <= '9') || (item >= 'a' && item <= 'f'))) return "";
        }
        return operationKey.substring(0, separator + 1);
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
    }

    private static Snapshot save(Context context, Snapshot state) {
        preferences(context).edit()
            .putString(CONTEXT_KEY, state.contextKey)
            .putString(JOURNAL_EPOCH, state.journalEpoch)
            .putLong(APPLIED_VERSION, state.appliedVersion)
            .putLong(PENDING_VERSION, state.pendingVersion)
            .putLong(PENDING_SINCE, state.pendingSince)
            .commit();
        return state;
    }

    private static ServerContext parseServerContext(String responseBody) {
        try {
            JSONObject root = new JSONObject(responseBody == null ? "{}" : responseBody);
            JSONObject context = root.optJSONObject("context");
            if (context == null) context = new JSONObject();
            String role = firstNonBlank(
                context.optString("role_app_code", ""),
                root.optString("role_app_code", BuildConfig.APP_PROFILE_ID)
            );
            String cursorScope = context.optString("cursor_scope", "").trim();
            if (cursorScope.isEmpty()) {
                List<String> equipment = readSortedIds(
                    context.optJSONArray("worker_equipment_ids") != null
                        ? context.optJSONArray("worker_equipment_ids")
                        : root.optJSONArray("worker_equipment_ids")
                );
                cursorScope = String.join("|",
                    context.optString("access_id", ""),
                    context.optString("employee_id", ""),
                    role,
                    String.join(",", equipment),
                    context.optString("active_shift_id", root.optString("active_shift_id", "")),
                    context.optString("session_revision", root.optString("session_revision", ""))
                );
            }
            JSONObject journal = root.optJSONObject("journal");
            String epoch = firstNonBlank(
                root.optString("journal_epoch", ""),
                journal == null ? "" : journal.optString("epoch", ""),
                journal == null ? "" : journal.optString("generation", "")
            );
            return new ServerContext(cursorScope, epoch);
        } catch (Exception ignored) {
            return new ServerContext("", "");
        }
    }

    private static List<String> readSortedIds(JSONArray values) {
        List<String> result = new ArrayList<>();
        if (values != null) {
            for (int index = 0; index < values.length(); index += 1) {
                String value = String.valueOf(values.opt(index)).trim();
                if (!value.isEmpty() && !"null".equals(value)) result.add(value);
            }
        }
        Collections.sort(result);
        return result;
    }

    private static String firstNonBlank(String... values) {
        for (String value : values) {
            String normalized = normalize(value);
            if (!normalized.isEmpty()) return normalized;
        }
        return "";
    }

    private static String normalize(String value) {
        return value == null ? "" : value.trim();
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte item : digest) result.append(String.format("%02x", item));
            return result.toString();
        } catch (Exception ignored) {
            return Integer.toHexString(value.hashCode()) + "00000000000000000000";
        }
    }

    static final class Snapshot {
        final String contextKey;
        final String journalEpoch;
        final long appliedVersion;
        final long pendingVersion;
        final long pendingSince;

        Snapshot(String contextKey, String journalEpoch, long appliedVersion, long pendingVersion, long pendingSince) {
            this.contextKey = normalize(contextKey);
            this.journalEpoch = normalize(journalEpoch);
            this.appliedVersion = Math.max(0L, appliedVersion);
            this.pendingVersion = Math.max(0L, pendingVersion);
            this.pendingSince = Math.max(0L, pendingSince);
        }

        Snapshot withContext(String value) {
            return new Snapshot(value, journalEpoch, appliedVersion, pendingVersion, pendingSince);
        }

        Snapshot withEpoch(String value) {
            return new Snapshot(contextKey, value, appliedVersion, pendingVersion, pendingSince);
        }

        JSObject toJsObject() {
            return new JSObject()
                .put("contextKey", contextKey)
                .put("journalEpoch", journalEpoch)
                .put("appliedVersion", appliedVersion)
                .put("pendingVersion", pendingVersion)
                .put("pendingSince", pendingSince);
        }
    }

    private static final class ServerContext {
        final String contextKey;
        final String journalEpoch;

        ServerContext(String contextKey, String journalEpoch) {
            this.contextKey = normalize(contextKey);
            this.journalEpoch = normalize(journalEpoch);
        }
    }
}
