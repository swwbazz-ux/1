package ru.copperresources.mobile;

import androidx.annotation.NonNull;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

public class CopperFirebaseMessagingService extends FirebaseMessagingService {
    private static String safeKind(String value) {
        return value != null && value.matches("[a-zA-Z0-9_.-]{1,64}") ? value : "unknown";
    }

    @Override
    public void onNewToken(@NonNull String token) {
        super.onNewToken(token);
        NativePushPlugin.publishToken(this, token);
    }

    @Override
    public void onMessageReceived(@NonNull RemoteMessage message) {
        super.onMessageReceived(message);
        if (!BuildConfig.NATIVE_PUSH_ENABLED || !NativeFieldProfile.supportsPushAndHaptics()) {
            return;
        }
        String kind = message.getData().get("kind");
        android.util.Log.i("FieldDelivery", "push_received kind=" + safeKind(kind)
            + " version=" + safeKind(message.getData().get("version"))
            + " priority=" + message.getPriority()
            + " foreground=" + AppVisibility.isForeground());
        FieldDeliveryPolicy.recordPush(android.os.SystemClock.elapsedRealtime());
        if ("presence_probe".equals(kind)) {
            String probeId = message.getData().get("probe_id");
            if (PresenceProbeState.remember(this, probeId, System.currentTimeMillis())) {
                ConnectivityForegroundService.reconcilePresenceProbe(this);
            }
            return;
        }
        ConnectivityForegroundService.reconcileFromForeground(this);
    }
}
