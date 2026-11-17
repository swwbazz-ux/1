package ru.copperresources.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.media.AudioAttributes;
import android.os.Build;
import android.util.Log;
import androidx.annotation.NonNull;
import androidx.core.app.NotificationCompat;
import com.google.firebase.messaging.NotificationResponse;
import com.google.firebase.messaging.RemoteMessage;

/** Только временная сборка -PfcmUrgentPilot; рабочих действий не создаёт. */
public class FcmUrgentPilotMessagingService extends CopperFirebaseMessagingService {
    private static final String CHANNEL = "fcm_urgent_pilot_v2";
    private static final String TAG = "fcm-urgent-pilot";
    private static final int ID = 1299;

    private boolean isProbe(RemoteMessage message) {
        return "fcm_latency_probe".equals(message.getData().get("kind"));
    }

    private void accept(RemoteMessage message, String transport) {
        long received = System.currentTimeMillis();
        String probe = message.getData().get("probe_id");
        if (probe == null || !probe.matches("[a-zA-Z0-9_-]{1,100}")) probe = "invalid";
        Log.i("FieldDeliveryPilot", "probe_received transport=" + transport
            + " probe_id=" + probe + " sent_at_ms=" + message.getSentTime()
            + " received_at_ms=" + received + " priority=" + message.getPriority()
            + " original_priority=" + message.getOriginalPriority()
            + " foreground=" + AppVisibility.isForeground());
        FieldDeliveryPolicy.recordPush(android.os.SystemClock.elapsedRealtime());
        ConnectivityForegroundService.reconcileFromForeground(this);
    }

    private Notification notification() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "Временная проверка FCM", NotificationManager.IMPORTANCE_HIGH);
            channel.enableVibration(false);
            channel.setSound(android.provider.Settings.System.DEFAULT_NOTIFICATION_URI,
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION).build());
            manager.createNotificationChannel(channel);
        }
        return new NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_alert)
            .setContentTitle("Проверка связи")
            .setContentText("Получен тестовый сигнал")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build();
    }

    @Override public void onMessageReceived(@NonNull RemoteMessage message) {
        if (!isProbe(message)) { super.onMessageReceived(message); return; }
        accept(message, "legacy");
        getSystemService(NotificationManager.class).notify(TAG, ID, notification());
    }

    @Override public NotificationResponse onNotificationReceived(@NonNull RemoteMessage message) {
        if (!isProbe(message)) {
            super.onMessageReceived(message);
            return NotificationResponse.create(TAG, ID, notification());
        }
        accept(message, "urgent_v2");
        return NotificationResponse.create(TAG, ID, notification());
    }

}
