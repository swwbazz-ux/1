package ru.copperresources.mobile;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Shell/system only: removes exclusively this temporary pilot's notification. */
public class FcmUrgentPilotCleanupReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        manager.cancel("fcm-urgent-pilot", 1299);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.deleteNotificationChannel("fcm_urgent_pilot_v2");
        }
    }
}
