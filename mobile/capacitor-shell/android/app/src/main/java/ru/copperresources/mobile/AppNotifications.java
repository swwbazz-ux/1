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
    private static final int OPERATIONAL_ALERT_NOTIFICATION_ID = 1202;

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
        foreground.enableVibration(false);
        foreground.enableLights(false);
        foreground.setSound(null, null);
        foreground.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
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

        return new NotificationCompat.Builder(context, BuildConfig.FOREGROUND_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_service)
            .setContentTitle(context.getString(R.string.foreground_notification_title))
            .setContentText(statusText)
            .setContentIntent(openPendingIntent)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setLocalOnly(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build();
    }

    public static boolean showOperationalAlert(Context context, String message) {
        return showOperationalAlert(context, BuildConfig.APP_DISPLAY_NAME, message);
    }

    public static boolean showOperationalAlert(Context context, String title, String message) {
        if (!alertsEnabled(context)) {
            return false;
        }
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
            .setContentTitle(title)
            .setContentText(message)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(message))
            .setContentIntent(openPendingIntent)
            .setCategory(NotificationCompat.CATEGORY_NAVIGATION)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setAutoCancel(true)
            .setDefaults(NotificationCompat.DEFAULT_VIBRATE | NotificationCompat.DEFAULT_LIGHTS)
            .build();
        try {
            NotificationManagerCompat.from(context).notify(OPERATIONAL_ALERT_NOTIFICATION_ID, notification);
            return true;
        } catch (SecurityException ignored) {
            // Android 13+: пользователь может явно запретить уведомления.
            return false;
        }
    }

    public static boolean alertsEnabled(Context context) {
        if (!NotificationManagerCompat.from(context).areNotificationsEnabled()) {
            return false;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager manager = context.getSystemService(NotificationManager.class);
            NotificationChannel channel = manager == null
                ? null
                : manager.getNotificationChannel(BuildConfig.ALERT_CHANNEL_ID);
            return channel != null && channel.getImportance() != NotificationManager.IMPORTANCE_NONE;
        }
        return true;
    }
}
