package ru.copperresources.mobile;

import android.content.Context;
import android.content.SharedPreferences;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "BackgroundConnection")
public class BackgroundConnectionPlugin extends Plugin {
    private SharedPreferences transportPreferences;
    private final SharedPreferences.OnSharedPreferenceChangeListener transportListener = (preferences, key) -> {
        if (ConnectionState.TRANSPORT_EVIDENCE.equals(key)) {
            notifyListeners("connectionState", ConnectionState.transportSnapshot(getContext()));
        }
    };

    @Override
    public void load() {
        transportPreferences = getContext().getSharedPreferences(ConnectionState.PREFS_NAME, Context.MODE_PRIVATE);
        transportPreferences.registerOnSharedPreferenceChangeListener(transportListener);
    }

    @Override
    protected void handleOnDestroy() {
        if (transportPreferences != null) {
            transportPreferences.unregisterOnSharedPreferenceChangeListener(transportListener);
        }
        super.handleOnDestroy();
    }

    @PluginMethod
    public void sync(PluginCall call) {
        boolean required = Boolean.TRUE.equals(call.getBoolean("required", false));
        String shiftId = call.getString("shiftId", "");
        String authGeneration = call.getString("authGeneration", "");
        if (required) {
            PendingDriverShiftClose.resumeAfterAuthentication(getContext(), authGeneration);
            if (!ConnectionState.enableFromUi(getContext(), shiftId)) {
                call.reject("Connection state was not saved");
                return;
            }
            ConnectivityForegroundService.startForActiveShift(getContext());
        } else {
            ConnectionState.disable(getContext(), call.getString("reason", "shift_inactive"));
            ConnectivityForegroundService.stop(getContext());
        }
        call.resolve(snapshot());
    }

    @PluginMethod
    public void stop(PluginCall call) {
        ConnectionState.disable(getContext(), call.getString("reason", "user_stop"));
        ConnectivityForegroundService.stop(getContext());
        call.resolve(snapshot());
    }

    @PluginMethod
    public void getState(PluginCall call) {
        call.resolve(snapshot());
    }

    @PluginMethod
    public void syncOperationalState(PluginCall call) {
        String contextKey = call.getString("contextKey", "").trim();
        String journalEpoch = call.getString("journalEpoch", "").trim();
        long pendingVersion = numericLong(call.getData().opt("pendingVersion"));
        boolean authoritativeReset = Boolean.TRUE.equals(call.getBoolean("authoritativeReset", false));
        boolean resetAccepted = false;
        OperationalDeliveryLedger.Snapshot state;
        if (authoritativeReset) {
            // Establishing a new scope already discards its old cursor. Lowering
            // an existing scope is accepted only when both server-bound identity
            // fields match the durable native ledger.
            state = OperationalDeliveryLedger.prepareWebViewHandoff(
                getContext(), contextKey, journalEpoch, pendingVersion
            );
            Object resetValue = call.getData().opt("resetVersion");
            long resetVersion = numericLong(resetValue);
            if (!contextKey.isEmpty()
                    && !journalEpoch.isEmpty()
                    && contextKey.equals(state.contextKey)
                    && journalEpoch.equals(state.journalEpoch)
                    && isNonNegativeInteger(resetValue)
                    && resetVersion >= 0L) {
                state = OperationalDeliveryLedger.resetApplied(getContext(), resetVersion);
                resetAccepted = true;
            }
        } else {
            state = OperationalDeliveryLedger.syncFromWebView(
                getContext(),
                contextKey,
                journalEpoch,
                numericLong(call.getData().opt("appliedVersion")),
                pendingVersion
            );
        }
        call.resolve(state.toJsObject().put("resetAccepted", resetAccepted));
    }

    @PluginMethod
    public void queueDriverShiftClose(PluginCall call) {
        if (!"driver".equals(BuildConfig.APP_PROFILE_ID)) {
            call.reject("Driver shift close is unavailable for this application");
            return;
        }
        String shiftId = call.getString("shiftId", "");
        String clientActionId = call.getString("clientActionId", "");
        boolean stored = PendingDriverShiftClose.enqueue(
            getContext(),
            shiftId,
            clientActionId,
            call.getString("endFuel", ""),
            call.getString("endMileage", ""),
            call.getString("endEngineHours", ""),
            call.getString("confirmationToken", ""),
            numericLong(call.getData().opt("createdAt")),
            call.getString("state", PendingDriverShiftClose.STATE_QUEUED),
            (int) numericLong(call.getData().opt("retryAttempts")),
            numericLong(call.getData().opt("nextAttemptAt")),
            call.getString("authGeneration", ""),
            call.getString("blockedAuthGeneration", "")
        );
        if (!stored) {
            call.reject("Driver shift close was not saved");
            return;
        }
        if (!ConnectionState.enableFromUi(getContext(), shiftId)) {
            PendingDriverShiftClose.clear(getContext(), clientActionId);
            call.reject("Driver shift close background state was not saved");
            return;
        }
        ConnectivityForegroundService.startForActiveShift(getContext());
        call.resolve(snapshot());
    }

    static long numericLong(Object value) {
        if (value instanceof Number) return ((Number) value).longValue();
        if (value instanceof String) {
            try { return Long.parseLong(((String) value).trim()); }
            catch (NumberFormatException ignored) {}
        }
        return 0L;
    }

    static boolean isNonNegativeInteger(Object value) {
        if (value instanceof Byte || value instanceof Short
                || value instanceof Integer || value instanceof Long) {
            return ((Number) value).longValue() >= 0L;
        }
        if (value instanceof Number) {
            double number = ((Number) value).doubleValue();
            return Double.isFinite(number)
                && number >= 0D
                && number <= Long.MAX_VALUE
                && number == Math.rint(number);
        }
        if (value instanceof String) {
            try {
                return Long.parseLong(((String) value).trim()) >= 0L;
            } catch (NumberFormatException ignored) {}
        }
        return false;
    }

    @PluginMethod
    public void acknowledgeDriverShiftClose(PluginCall call) {
        if (!PendingDriverShiftClose.clear(
                getContext(),
                call.getString("clientActionId", ""))) {
            call.reject("Another driver shift close is pending");
            return;
        }
        call.resolve(snapshot());
    }

    private JSObject snapshot() {
        PendingDriverShiftClose pending = PendingDriverShiftClose.load(getContext());
        JSObject result = new JSObject()
            .put("desired", ConnectionState.isDesired(getContext()))
            .put("shiftActive", ConnectionState.isShiftActive(getContext()))
            .put("shiftId", ConnectionState.activeShiftId(getContext()))
            .put("lastAliveAt", ConnectionState.lastAliveAt(getContext()))
            .put("lastStopReason", ConnectionState.lastStopReason(getContext()));
        result.put("transport", ConnectionState.transportSnapshot(getContext()));
        result.put("operational", OperationalDeliveryLedger.snapshot(getContext()).toJsObject());
        result.put("pendingDriverShiftClose", pending == null ? null : pending.toJsObject());
        return result;
    }
}
