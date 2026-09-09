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

    final String shiftId;
    final String clientActionId;
    final String endFuel;
    final String endMileage;
    final String endEngineHours;
    final long createdAt;
    final boolean requiresAttention;
    final String attentionMessage;

    private PendingDriverShiftClose(
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours,
            long createdAt,
            boolean requiresAttention,
            String attentionMessage) {
        this.shiftId = shiftId;
        this.clientActionId = clientActionId;
        this.endFuel = endFuel;
        this.endMileage = endMileage;
        this.endEngineHours = endEngineHours;
        this.createdAt = createdAt;
        this.requiresAttention = requiresAttention;
        this.attentionMessage = attentionMessage == null ? "" : attentionMessage;
    }

    static boolean enqueue(
            Context context,
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours) {
        PendingDriverShiftClose pending = validated(
            shiftId,
            clientActionId,
            endFuel,
            endMileage,
            endEngineHours,
            System.currentTimeMillis(),
            false,
            ""
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
                value.optLong("created_at", 0L),
                value.optBoolean("requires_attention", false),
                value.optString("attention_message", "")
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

    static boolean markAttention(Context context, String expectedClientActionId, String message) {
        PendingDriverShiftClose pending = load(context);
        if (pending == null || !pending.clientActionId.equals(expectedClientActionId)) {
            return false;
        }
        PendingDriverShiftClose updated = validated(
            pending.shiftId,
            pending.clientActionId,
            pending.endFuel,
            pending.endMileage,
            pending.endEngineHours,
            pending.createdAt,
            true,
            message == null ? "Проверьте показания на конец смены." : message
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
        value.put("createdAt", createdAt);
        value.put("requiresAttention", requiresAttention);
        value.put("attentionMessage", attentionMessage);
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
            value.put("created_at", createdAt);
            value.put("requires_attention", requiresAttention);
            value.put("attention_message", attentionMessage);
        } catch (Exception ignored) {}
        return value;
    }

    private static PendingDriverShiftClose validated(
            String shiftId,
            String clientActionId,
            String endFuel,
            String endMileage,
            String endEngineHours,
            long createdAt,
            boolean requiresAttention,
            String attentionMessage) {
        String safeShiftId = clean(shiftId);
        String safeActionId = clean(clientActionId);
        String safeFuel = clean(endFuel);
        String safeMileage = clean(endMileage);
        String safeHours = clean(endEngineHours);
        if (!SHIFT_ID.matcher(safeShiftId).matches()
                || !ACTION_ID.matcher(safeActionId).matches()
                || !READING.matcher(safeFuel).matches()
                || !READING.matcher(safeMileage).matches()
                || !READING.matcher(safeHours).matches()) {
            return null;
        }
        return new PendingDriverShiftClose(
            safeShiftId,
            safeActionId,
            safeFuel,
            safeMileage,
            safeHours,
            Math.max(1L, createdAt),
            requiresAttention,
            attentionMessage
        );
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(ConnectionState.PREFS_NAME, Context.MODE_PRIVATE);
    }
}
