package ru.copperresources.mobile;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "BackgroundConnection")
public class BackgroundConnectionPlugin extends Plugin {
    @PluginMethod
    public void sync(PluginCall call) {
        boolean required = Boolean.TRUE.equals(call.getBoolean("required", false));
        String shiftId = call.getString("shiftId", "");
        if (required) {
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
            call.getString("confirmationToken", "")
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
        result.put("pendingDriverShiftClose", pending == null ? null : pending.toJsObject());
        return result;
    }
}
