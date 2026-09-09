package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import com.getcapacitor.JSObject;

import org.json.JSONObject;

import java.util.regex.Pattern;

/** Durable one-item outbox for the only driver action allowed after the UI goes away. */
final class PendingDriverShiftClose {
    private static final String PREFERENCE_KEY = "pending_driver_shift_close_v1";
    private static final Pattern ACTION_ID = Pattern.compile("[A-Za-z0-9._:-]{1,128}");
    private static final Pattern SHIFT_ID = Pattern.compile("[1-9][0-9]{0,18}");
    private static final Pattern READING = Pattern.compile("[0-9]{1,10}");
    private static final Pattern CONFIRMATION_TOKEN = Pattern.compile("[A-Za-z0-9._:-]{0,4096}");

    final String shiftId;
    final String clientActionId;
    final String endFuel;
    final String endMileage;
    final String endEngineHours;
    final String confirmationToken;
    final long createdAt;
    final boolean requiresAttention;
    final boolean confirmationRequired;
    final boolean hasActiveShift;
    final String attentionMessage;
    final String warningsJson;
    final String fieldErrorsJson;

    private PendingDriverShiftClose(
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours,
            String confirmationToken,
            long createdAt,
            boolean requiresAttention,
            boolean confirmationRequired,
            boolean hasActiveShift,
            String attentionMessage,
            String warningsJson,
            String fieldErrorsJson) {
        this.shiftId = shiftId;
        this.clientActionId = clientActionId;
        this.endFuel = endFuel;
        this.endMileage = endMileage;
        this.endEngineHours = endEngineHours;
        this.confirmationToken = confirmationToken;
        this.createdAt = createdAt;
        this.requiresAttention = requiresAttention;
        this.confirmationRequired = confirmationRequired;
        this.hasActiveShift = hasActiveShift;
        this.attentionMessage = attentionMessage == null ? "" : attentionMessage;
        this.warningsJson = warningsJson == null ? "[]" : warningsJson;
        this.fieldErrorsJson = fieldErrorsJson == null ? "{}" : fieldErrorsJson;
    }

    static boolean enqueue(
            Context context,
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours,
            String confirmationToken) {
        PendingDriverShiftClose pending = validated(
            shiftId,
            clientActionId,
            endFuel,
            endMileage,
            endEngineHours,
            confirmationToken,
            System.currentTimeMillis(),
            false,
            false,
            true,
            "",
            "[]",
            "{}"
        );
        if (pending == null) {
            return false;
        }
        return preferences(context).edit()
            .putString(PREFERENCE_KEY, pending.toJson().toString())
            .commit();
    }

    static PendingDriverShiftClose load(Context context) {
        String encoded = preferences(context).getString(PREFERENCE_KEY, "");
        if (encoded == null || encoded.isBlank()) {
            return null;
        }
        try {
            JSONObject value = new JSONObject(encoded);
            PendingDriverShiftClose pending = validated(
                value.optString("shift_id", ""),
                value.optString("client_action_id", ""),
                value.optString("end_fuel", ""),
                value.optString("end_mileage", ""),
                value.optString("end_engine_hours", ""),
                value.optString("confirmation_token", ""),
                value.optLong("created_at", 0L),
                value.optBoolean("requires_attention", false),
                value.optBoolean("confirmation_required", false),
                value.optBoolean("has_active_shift", true),
                value.optString("attention_message", ""),
                value.optString("warnings", "[]"),
                value.optString("field_errors", "{}")
            );
            if (pending != null) {
                return pending;
            }
        } catch (Exception ignored) {}
        preferences(context).edit().remove(PREFERENCE_KEY).commit();
        return null;
    }

    static boolean hasPending(Context context) {
        return load(context) != null;
    }

    static boolean clear(Context context, String expectedClientActionId) {
        PendingDriverShiftClose pending = load(context);
        if (pending == null) {
            return true;
        }
        if (expectedClientActionId != null
                && !expectedClientActionId.isBlank()
                && !pending.clientActionId.equals(expectedClientActionId)) {
            return false;
        }
        return preferences(context).edit().remove(PREFERENCE_KEY).commit();
    }

    static boolean markAttention(Context context, String expectedClientActionId, String responseBody) {
        PendingDriverShiftClose pending = load(context);
        if (pending == null || !pending.clientActionId.equals(expectedClientActionId)) {
            return false;
        }
        String message = "Проверьте показания на конец смены.";
        String confirmationToken = pending.confirmationToken;
        boolean confirmationRequired = false;
        boolean hasActiveShift = true;
        String warningsJson = "[]";
        String fieldErrorsJson = "{}";
        try {
            JSONObject response = new JSONObject(responseBody == null ? "{}" : responseBody);
            message = response.optString("error", message);
            confirmationToken = response.optString("confirmation_token", confirmationToken);
            confirmationRequired = response.optBoolean("confirmation_required", false);
            hasActiveShift = response.optBoolean("has_active_shift", true);
            warningsJson = response.optJSONArray("warnings") == null
                ? "[]"
                : response.optJSONArray("warnings").toString();
            fieldErrorsJson = response.optJSONObject("field_errors") == null
                ? "{}"
                : response.optJSONObject("field_errors").toString();
        } catch (Exception ignored) {}
        PendingDriverShiftClose updated = validated(
            pending.shiftId,
            pending.clientActionId,
            pending.endFuel,
            pending.endMileage,
            pending.endEngineHours,
            confirmationToken,
            pending.createdAt,
            true,
            confirmationRequired,
            hasActiveShift,
            message,
            warningsJson,
            fieldErrorsJson
        );
        return updated != null && preferences(context).edit()
            .putString(PREFERENCE_KEY, updated.toJson().toString())
            .commit();
    }

    JSObject toJsObject() {
        JSObject value = new JSObject();
        value.put("shiftId", shiftId);
        value.put("clientActionId", clientActionId);
        value.put("endFuel", endFuel);
        value.put("endMileage", endMileage);
        value.put("endEngineHours", endEngineHours);
        value.put("confirmationToken", confirmationToken);
        value.put("createdAt", createdAt);
        value.put("requiresAttention", requiresAttention);
        value.put("confirmationRequired", confirmationRequired);
        value.put("hasActiveShift", hasActiveShift);
        value.put("attentionMessage", attentionMessage);
        try {
            value.put("warnings", new org.json.JSONArray(warningsJson));
            value.put("field_errors", new JSONObject(fieldErrorsJson));
        } catch (Exception ignored) {
            value.put("warnings", new org.json.JSONArray());
            value.put("field_errors", new JSONObject());
        }
        return value;
    }

    private JSONObject toJson() {
        JSONObject value = new JSONObject();
        try {
            value.put("shift_id", shiftId);
            value.put("client_action_id", clientActionId);
            value.put("end_fuel", endFuel);
            value.put("end_mileage", endMileage);
            value.put("end_engine_hours", endEngineHours);
            value.put("confirmation_token", confirmationToken);
            value.put("created_at", createdAt);
            value.put("requires_attention", requiresAttention);
            value.put("confirmation_required", confirmationRequired);
            value.put("has_active_shift", hasActiveShift);
            value.put("attention_message", attentionMessage);
            value.put("warnings", warningsJson);
            value.put("field_errors", fieldErrorsJson);
        } catch (Exception ignored) {}
        return value;
    }

    private static PendingDriverShiftClose validated(
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours,
            String confirmationToken,
            long createdAt,
            boolean requiresAttention,
            boolean confirmationRequired,
            boolean hasActiveShift,
            String attentionMessage,
            String warningsJson,
            String fieldErrorsJson) {
        String safeShiftId = clean(shiftId);
        String safeActionId = clean(clientActionId);
        String safeFuel = clean(endFuel);
        String safeMileage = clean(endMileage);
        String safeHours = clean(endEngineHours);
        String safeConfirmationToken = clean(confirmationToken);
        if (!SHIFT_ID.matcher(safeShiftId).matches()
                || !ACTION_ID.matcher(safeActionId).matches()
                || !READING.matcher(safeFuel).matches()
                || !READING.matcher(safeMileage).matches()
                || !READING.matcher(safeHours).matches()
                || !CONFIRMATION_TOKEN.matcher(safeConfirmationToken).matches()) {
            return null;
        }
        return new PendingDriverShiftClose(
            safeShiftId,
            safeActionId,
            safeFuel,
            safeMileage,
            safeHours,
            safeConfirmationToken,
            Math.max(1L, createdAt),
            requiresAttention,
            confirmationRequired,
            hasActiveShift,
            cleanLimited(attentionMessage, 4096, "Проверьте показания на конец смены."),
            cleanJson(warningsJson, true),
            cleanJson(fieldErrorsJson, false)
        );
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    private static String cleanLimited(String value, int limit, String fallback) {
        String cleaned = clean(value);
        if (cleaned.isEmpty()) cleaned = fallback;
        return cleaned.length() <= limit ? cleaned : cleaned.substring(0, limit);
    }

    private static String cleanJson(String value, boolean array) {
        String fallback = array ? "[]" : "{}";
        String cleaned = clean(value);
        if (cleaned.length() > 32768) return fallback;
        try {
            if (array) new org.json.JSONArray(cleaned);
            else new JSONObject(cleaned);
            return cleaned;
        } catch (Exception ignored) {
            return fallback;
        }
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(ConnectionState.PREFS_NAME, Context.MODE_PRIVATE);
    }
}
