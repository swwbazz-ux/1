package ru.copperresources.mobile;

import android.app.Notification;
import android.annotation.SuppressLint;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.net.Uri;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

public final class AppNotifications {
    public static final int FOREGROUND_NOTIFICATION_ID = 1201;
    private static final int TEST_ALERT_NOTIFICATION_ID = 1202;

    private AppNotifications() {}

    @SuppressLint("DiscouragedApi")
    public static void createChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) {
            return;
        }

        NotificationChannel foreground = new NotificationChannel(
            BuildConfig.FOREGROUND_CHANNEL_ID,
            context.getString(R.string.foreground_channel_name),
            NotificationManager.IMPORTANCE_LOW
        );
        foreground.setDescription("Постоянная связь рабочего приложения с сервером");
        foreground.setShowBadge(false);
        foreground.setSound(null, null);
        manager.createNotificationChannel(foreground);

        int soundId = context.getResources().getIdentifier(
            BuildConfig.ALERT_SOUND_RESOURCE,
            "raw",
            context.getPackageName()
        );
        Uri soundUri = soundId == 0
            ? android.provider.Settings.System.DEFAULT_NOTIFICATION_URI
            : Uri.parse("android.resource://" + context.getPackageName() + "/" + soundId);
        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_NOTIFICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build();

        NotificationChannel alerts = new NotificationChannel(
            BuildConfig.ALERT_CHANNEL_ID,
            context.getString(R.string.alert_channel_name),
            NotificationManager.IMPORTANCE_HIGH
        );
        alerts.setDescription("Звуковые производственные оповещения приложения");
        alerts.enableVibration(true);
        alerts.enableLights(true);
        alerts.setSound(soundUri, attributes);
        manager.createNotificationChannel(alerts);
    }

    public static Notification foregroundNotification(Context context, String statusText) {
        Intent openIntent = new Intent(context, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openPendingIntent = PendingIntent.getActivity(
            context,
            0,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Intent testIntent = new Intent(context, ConnectivityForegroundService.class)
            .setAction(ConnectivityForegroundService.ACTION_TEST_ALERT);
        PendingIntent testPendingIntent = PendingIntent.getService(
            context,
            1,
            testIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        return new NotificationCompat.Builder(context, BuildConfig.FOREGROUND_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_service)
            .setContentTitle(context.getString(R.string.foreground_notification_title))
            .setContentText(statusText)
            .setContentIntent(openPendingIntent)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .addAction(R.drawable.ic_stat_alert, "Тест сигнала", testPendingIntent)
            .build();
    }

    public static void showTestAlert(Context context) {
        showOperationalAlert(context, "Тестовое производственное оповещение");
    }

    public static void showOperationalAlert(Context context, String message) {
        Intent openIntent = new Intent(context, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openPendingIntent = PendingIntent.getActivity(
            context,
            2,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Notification notification = new NotificationCompat.Builder(context, BuildConfig.ALERT_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_alert)
            .setContentTitle(BuildConfig.APP_DISPLAY_NAME)
            .setContentText(message)
            .setContentIntent(openPendingIntent)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setAutoCancel(true)
            .setDefaults(NotificationCompat.DEFAULT_VIBRATE | NotificationCompat.DEFAULT_LIGHTS)
            .build();
        try {
            NotificationManagerCompat.from(context).notify(TEST_ALERT_NOTIFICATION_ID, notification);
        } catch (SecurityException ignored) {
            // Android 13+: пользователь может явно запретить уведомления.
        }
    }
}
