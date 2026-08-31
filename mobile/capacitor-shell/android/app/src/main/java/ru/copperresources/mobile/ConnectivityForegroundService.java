package ru.copperresources.mobile;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.Uri;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;
import android.webkit.CookieManager;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.Date;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import org.json.JSONObject;

public class ConnectivityForegroundService extends Service {
    public static final String ACTION_TEST_ALERT = "ru.copperresources.mobile.action.TEST_ALERT";
    private static final String PREFS_NAME = "native_connectivity";
    private static final long MAX_BACKOFF_MS = 120_000L;
    private static final int MAX_CAPTURED_RESPONSE_BYTES = 64 * 1024;

    private final Object scheduleLock = new Object();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ScheduledExecutorService executor;
    private ScheduledFuture<?> pendingHeartbeat;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private int consecutiveFailures;

    public static void start(Context context) {
        ContextCompat.startForegroundService(
            context,
            new Intent(context, ConnectivityForegroundService.class)
        );
    }

    public static void stop(Context context) {
        context.stopService(new Intent(context, ConnectivityForegroundService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        AppNotifications.createChannels(this);
        executor = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "native-server-heartbeat");
            thread.setDaemon(true);
            return thread;
        });
        startAsForeground("Подключаемся к серверу…");
        registerNetworkCallback();
        scheduleHeartbeat(0L);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_TEST_ALERT.equals(intent.getAction())) {
            AppNotifications.showTestAlert(this);
        }
        startAsForeground(currentStatusText());
        scheduleHeartbeat(0L);
        return START_NOT_STICKY;
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {}
        }
        synchronized (scheduleLock) {
            if (pendingHeartbeat != null) {
                pendingHeartbeat.cancel(true);
            }
        }
        if (executor != null) {
            executor.shutdownNow();
        }
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        Log.i("ConnectivityForegroundService", "Task removed; stopping service");
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
        super.onTaskRemoved(rootIntent);
    }

    private void startAsForeground(String statusText) {
        int serviceType = android.os.Build.VERSION.SDK_INT >= 34
            ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            : 0;
        ServiceCompat.startForeground(
            this,
            AppNotifications.FOREGROUND_NOTIFICATION_ID,
            AppNotifications.foregroundNotification(this, statusText),
            serviceType
        );
    }

    private void registerNetworkCallback() {
        connectivityManager = getSystemService(ConnectivityManager.class);
        if (connectivityManager == null) {
            return;
        }
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(@NonNull Network network) {
                scheduleHeartbeat(0L);
            }

            @Override
            public void onLost(@NonNull Network network) {
                publishStatus("Сеть недоступна — ожидаем восстановление");
            }
        };
        try {
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException ignored) {}
    }

    private void scheduleHeartbeat(long delayMs) {
        synchronized (scheduleLock) {
            if (executor == null || executor.isShutdown()) {
                return;
            }
            if (pendingHeartbeat != null && !pendingHeartbeat.isDone()) {
                pendingHeartbeat.cancel(false);
            }
            pendingHeartbeat = executor.schedule(this::runHeartbeat, Math.max(0L, delayMs), TimeUnit.MILLISECONDS);
        }
    }

    private void runHeartbeat() {
        PowerManager.WakeLock wakeLock = null;
        try {
            PowerManager powerManager = getSystemService(PowerManager.class);
            if (powerManager != null) {
                wakeLock = powerManager.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    getPackageName() + ":server-heartbeat"
                );
                wakeLock.acquire(20_000L);
            }
            HeartbeatResult result = requestHeartbeat();
            consecutiveFailures = 0;
            SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            long previousVersion = preferences.getLong("last_server_version", 0L);
            long serverVersion = readServerVersion(result.body);
            boolean relevant = readRelevantFlag(result.body);
            preferences.edit()
                .putLong("last_transport_success_at", System.currentTimeMillis())
                .putInt("last_http_status", result.statusCode)
                .putString("last_response", result.body)
                .putLong("last_server_version", serverVersion > 0L ? serverVersion : previousVersion)
                .apply();
            if (previousVersion > 0L
                    && serverVersion > previousVersion
                    && relevant
                    && !AppVisibility.isForeground()) {
                AppNotifications.showOperationalAlert(this, "На сервере появились новые данные смены");
            }
            publishStatus("Сервер доступен • " + DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date()));
            scheduleHeartbeat(BuildConfig.HEARTBEAT_INTERVAL_MS);
        } catch (Exception error) {
            consecutiveFailures += 1;
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
                .putLong("last_failure_at", System.currentTimeMillis())
                .putString("last_error", error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage()))
                .apply();
            publishStatus("Связь восстанавливается…");
            long multiplier = 1L << Math.min(consecutiveFailures, 3);
            scheduleHeartbeat(Math.min(MAX_BACKOFF_MS, BuildConfig.HEARTBEAT_INTERVAL_MS * multiplier));
        } finally {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        }
    }

    private HeartbeatResult requestHeartbeat() throws Exception {
        SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        long lastServerVersion = preferences.getLong("last_server_version", 0L);
        Uri.Builder heartbeatUri = Uri.parse(BuildConfig.HEARTBEAT_URL).buildUpon();
        if (lastServerVersion > 0L) {
            heartbeatUri.appendQueryParameter("after", Long.toString(lastServerVersion));
        }
        URL url = new URL(heartbeatUri.build().toString());
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        try {
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(10_000);
            connection.setRequestMethod("GET");
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Cache-Control", "no-store");
            connection.setRequestProperty("Connection", "keep-alive");
            connection.setRequestProperty("User-Agent", "CopperResourcesNative/" + BuildConfig.APP_PROFILE_ID);

            String cookie = readWebViewCookie();
            if (cookie != null && !cookie.isBlank()) {
                connection.setRequestProperty("Cookie", cookie);
            }
            if (!BuildConfig.SYNC_AUTH_TOKEN.isBlank()) {
                connection.setRequestProperty(BuildConfig.SYNC_AUTH_HEADER, BuildConfig.SYNC_AUTH_TOKEN);
            }

            int statusCode = connection.getResponseCode();
            InputStream stream = statusCode >= 400 ? connection.getErrorStream() : connection.getInputStream();
            String body = readResponse(stream);
            return new HeartbeatResult(statusCode, body);
        } finally {
            connection.disconnect();
        }
    }

    private String readWebViewCookie() {
        AtomicReference<String> cookie = new AtomicReference<>();
        CountDownLatch latch = new CountDownLatch(1);
        mainHandler.post(() -> {
            try {
                cookie.set(CookieManager.getInstance().getCookie(BuildConfig.APP_SERVER_URL));
            } finally {
                latch.countDown();
            }
        });
        try {
            latch.await(2, TimeUnit.SECONDS);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
        return cookie.get();
    }

    private static String readResponse(InputStream stream) throws Exception {
        if (stream == null) {
            return "";
        }
        try (InputStream input = stream; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int total = 0;
            int count;
            while ((count = input.read(buffer)) != -1 && total < MAX_CAPTURED_RESPONSE_BYTES) {
                int accepted = Math.min(count, MAX_CAPTURED_RESPONSE_BYTES - total);
                output.write(buffer, 0, accepted);
                total += accepted;
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private void publishStatus(String text) {
        try {
            NotificationManagerCompat.from(this).notify(
                AppNotifications.FOREGROUND_NOTIFICATION_ID,
                AppNotifications.foregroundNotification(this, text)
            );
        } catch (SecurityException ignored) {
            // Foreground service remains active even if Android 13+ notification permission is denied.
        }
    }

    private static long readServerVersion(String body) {
        try {
            return new JSONObject(body).optLong("version", 0L);
        } catch (Exception ignored) {
            return 0L;
        }
    }

    private static boolean readRelevantFlag(String body) {
        try {
            return new JSONObject(body).optBoolean("relevant", false);
        } catch (Exception ignored) {
            return false;
        }
    }

    private String currentStatusText() {
        SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        long lastSuccessAt = preferences.getLong("last_transport_success_at", 0L);
        if (lastSuccessAt <= 0L) {
            return "Подключаемся к серверу…";
        }
        return "Сервер доступен • " + DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date(lastSuccessAt));
    }

    private static final class HeartbeatResult {
        final int statusCode;
        final String body;

        HeartbeatResult(int statusCode, String body) {
            this.statusCode = statusCode;
            this.body = body;
        }
    }
}
