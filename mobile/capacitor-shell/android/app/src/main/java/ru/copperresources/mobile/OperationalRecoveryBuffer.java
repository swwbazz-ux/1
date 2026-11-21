package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Map;
import java.util.TreeMap;

/** Persistent relevant-event buffer used while native heartbeat reads replay pages. */
final class OperationalRecoveryBuffer {
    private static final String SCOPE = "operational_recovery_scope_v1";
    private static final String EVENTS = "operational_recovery_events_v1";
    private static final String VOICE_ELIGIBLE = "operational_recovery_voice_eligible_v1";
    private static final int MAX_EVENTS = 4096;

    private OperationalRecoveryBuffer() {}

    static synchronized void append(Context context, String responseBody, boolean voiceEligible) {
        try {
            ensureScope(context);
            SharedPreferences preferences = preferences(context);
            boolean starting = !preferences.contains(EVENTS);
            JSONArray incoming = new JSONObject(responseBody == null ? "{}" : responseBody)
                .optJSONArray("events");
            TreeMap<Long, JSONObject> events = readEvents(context);
            if (incoming != null) {
                for (int index = 0; index < incoming.length(); index += 1) {
                    JSONObject event = incoming.optJSONObject(index);
                    long version = event == null ? 0L : event.optLong("version", 0L);
                    if (version > 0L) events.put(version, event);
                }
            }
            while (events.size() > MAX_EVENTS) events.pollFirstEntry();
            writeEvents(context, events);
            if (starting) preferences.edit().putBoolean(VOICE_ELIGIBLE, voiceEligible).commit();
        } catch (Exception ignored) {
            // A malformed optional delta must not make the heartbeat service fail.
        }
    }

    static synchronized String merge(Context context, String responseBody) {
        try {
            JSONObject root = new JSONObject(responseBody == null ? "{}" : responseBody);
            TreeMap<Long, JSONObject> events = readEvents(context);
            JSONArray incoming = root.optJSONArray("events");
            if (incoming != null) {
                for (int index = 0; index < incoming.length(); index += 1) {
                    JSONObject event = incoming.optJSONObject(index);
                    long version = event == null ? 0L : event.optLong("version", 0L);
                    if (version > 0L) events.put(version, event);
                }
            }
            JSONArray merged = new JSONArray();
            for (Map.Entry<Long, JSONObject> entry : events.entrySet()) {
                merged.put(entry.getValue());
            }
            root.put("events", merged);
            root.put("relevant", merged.length() > 0);
            root.put("events_truncated", false);
            return root.toString();
        } catch (Exception ignored) {
            return responseBody;
        }
    }

    static synchronized void clear(Context context) {
        preferences(context).edit().remove(SCOPE).remove(EVENTS).remove(VOICE_ELIGIBLE).commit();
    }

    static synchronized boolean isVoiceEligible(Context context) {
        ensureScope(context);
        return preferences(context).getBoolean(VOICE_ELIGIBLE, false);
    }

    static synchronized boolean hasPending(Context context) {
        ensureScope(context);
        return preferences(context).contains(EVENTS);
    }

    private static TreeMap<Long, JSONObject> readEvents(Context context) {
        ensureScope(context);
        TreeMap<Long, JSONObject> result = new TreeMap<>();
        try {
            JSONArray stored = new JSONArray(preferences(context).getString(EVENTS, "[]"));
            for (int index = 0; index < stored.length(); index += 1) {
                JSONObject event = stored.optJSONObject(index);
                long version = event == null ? 0L : event.optLong("version", 0L);
                if (version > 0L) result.put(version, event);
            }
        } catch (Exception ignored) {}
        return result;
    }

    private static void writeEvents(Context context, TreeMap<Long, JSONObject> events) {
        JSONArray stored = new JSONArray();
        for (Map.Entry<Long, JSONObject> entry : events.entrySet()) stored.put(entry.getValue());
        preferences(context).edit()
            .putString(SCOPE, currentScope(context))
            .putString(EVENTS, stored.toString())
            .commit();
    }

    private static void ensureScope(Context context) {
        SharedPreferences preferences = preferences(context);
        String current = currentScope(context);
        String stored = preferences.getString(SCOPE, "");
        if (!stored.isEmpty() && !stored.equals(current)) {
            preferences.edit().remove(EVENTS).remove(VOICE_ELIGIBLE).putString(SCOPE, current).commit();
        } else if (stored.isEmpty()) {
            preferences.edit().putString(SCOPE, current).commit();
        }
    }

    private static String currentScope(Context context) {
        OperationalDeliveryLedger.Snapshot snapshot = OperationalDeliveryLedger.snapshot(context);
        return snapshot.contextKey + "|" + snapshot.journalEpoch;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            ConnectivityForegroundService.PREFS_NAME,
            Context.MODE_PRIVATE
        );
    }
}
