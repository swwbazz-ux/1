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
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URLEncoder;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import org.json.JSONArray;
import org.json.JSONObject;

public class ConnectivityForegroundService extends Service {
    public static final String ACTION_STOP_CONNECTION = "ru.copperresources.mobile.action.STOP_CONNECTION";
    private static final String ACTION_RECONCILE = "ru.copperresources.mobile.action.RECONCILE_CONNECTION";
    private static final String ACTION_ACTIVE_SHIFT = "ru.copperresources.mobile.action.ACTIVE_SHIFT";
    static final String PREFS_NAME = ConnectionState.PREFS_NAME;
    static final String LAST_DRIVER_DUMP_POINT_ALERT_VERSION = "last_driver_dump_point_alert_version";
    private static final String CONNECTION_LOSS_ANNOUNCED = "connection_loss_announced";
    private static final long MAX_BACKOFF_MS = 60_000L;
    private static final int MAX_CAPTURED_RESPONSE_BYTES = 64 * 1024;

    private final Object scheduleLock = new Object();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ScheduledExecutorService executor;
    private ScheduledFuture<?> pendingHeartbeat;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private int consecutiveFailures;
    private boolean foregroundStarted;
    private String publishedStatus = "";

    public static void start(Context context) {
        reconcileFromForeground(context);
    }

    public static void reconcileFromForeground(Context context) {
        if (!ConnectionState.isDesired(context) && !PendingDriverShiftClose.hasPending(context)) {
            return;
        }
        Intent intent = new Intent(context, ConnectivityForegroundService.class)
            .setAction(ACTION_RECONCILE);
        startSafely(context, intent);
    }

    public static void startForActiveShift(Context context) {
        startSafely(
            context,
            new Intent(context, ConnectivityForegroundService.class).setAction(ACTION_ACTIVE_SHIFT)
        );
    }

    private static void startSafely(Context context, Intent intent) {
        try {
            ContextCompat.startForegroundService(context, intent);
        } catch (RuntimeException error) {
            Log.w("ConnectivityForegroundService", "Foreground heartbeat start was rejected", error);
        }
    }

    public static void stop(Context context) {
        context.stopService(new Intent(context, ConnectivityForegroundService.class));
        try {
            NotificationManagerCompat.from(context).cancel(AppNotifications.FOREGROUND_NOTIFICATION_ID);
        } catch (SecurityException ignored) {}
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
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? "" : String.valueOf(intent.getAction());
        if (ACTION_STOP_CONNECTION.equals(action)) {
            if (PendingDriverShiftClose.hasPending(this)) {
                startAsForeground("Закрытие смены сохранено — ждём связь");
                registerNetworkCallback();
                scheduleHeartbeat(0L);
                return START_STICKY;
            }
            ConnectionState.disable(this, "notification_stop");
            stopServiceAndRemoveNotification();
            return START_NOT_STICKY;
        }
        if (!ConnectionState.isDesired(this) && !PendingDriverShiftClose.hasPending(this)) {
            stopServiceAndRemoveNotification();
            return START_NOT_STICKY;
        }
        startAsForeground(currentStatusText());
        registerNetworkCallback();
        scheduleHeartbeat(0L);
        // Если Android освободил процесс под давлением памяти, он должен
        // восстановить рабочую связь без повторного открытия приложения.
        return START_STICKY;
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
        if (PendingDriverShiftClose.hasPending(this)) {
            Log.i("ConnectivityForegroundService", "Task removed; pending shift close keeps sync alive");
            scheduleHeartbeat(0L);
            super.onTaskRemoved(rootIntent);
            return;
        }
        ConnectionState.disable(this, "task_removed");
        Log.i("ConnectivityForegroundService", "Task removed; heartbeat service stopped");
        stopServiceAndRemoveNotification();
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
        foregroundStarted = true;
        publishedStatus = statusText;
    }

    private void registerNetworkCallback() {
        if (networkCallback != null) {
            return;
        }
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
            FlushResult shiftCloseFlush = flushPendingDriverShiftClose();
            HeartbeatResult result = requestHeartbeat();
            if (result.statusCode == 401 || result.statusCode == 403) {
                stopBecauseConnectionIsNotRequired("authentication_ended");
                return;
            }
            if (result.statusCode < 200 || result.statusCode >= 300) {
                throw new IllegalStateException("Heartbeat HTTP " + result.statusCode);
            }
            JSONObject response = new JSONObject(result.body);
            if (!response.optBoolean("authenticated", true)) {
                stopBecauseConnectionIsNotRequired("authentication_ended");
                return;
            }
            if (response.has("background_connection_required")) {
                boolean connectionRequired = response.optBoolean("background_connection_required", false);
                String shiftId = response.optString("active_shift_id", "");
                ConnectionState.applyServerRequirement(this, connectionRequired, shiftId);
                if (!connectionRequired) {
                    if (!response.optBoolean("has_active_shift", false)) {
                        PendingDriverShiftClose.clear(this, null);
                    }
                    stopBecauseConnectionIsNotRequired(
                        response.optBoolean("has_active_shift", false)
                            ? "role_inactive"
                            : "shift_inactive"
                    );
                    return;
                }
            } else if (!ConnectionState.isDesired(this)) {
                stopBecauseConnectionIsNotRequired("server_contract_missing");
                return;
            }
            consecutiveFailures = 0;
            SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            boolean connectionLossWasAnnounced = preferences.getBoolean(CONNECTION_LOSS_ANNOUNCED, false);
            long previousVersion = preferences.getLong("last_server_version", 0L);
            long serverVersion = readServerVersion(result.body);
            boolean relevant = readRelevantFlag(result.body);
            preferences.edit()
                .putLong("last_transport_success_at", System.currentTimeMillis())
                .putInt("last_http_status", result.statusCode)
                .putString("last_response", result.body)
                .putLong("last_server_version", serverVersion > 0L ? serverVersion : previousVersion)
                .putBoolean(CONNECTION_LOSS_ANNOUNCED, false)
                .apply();
            ConnectionState.recordAlive(this, System.currentTimeMillis());
            if (connectionLossWasAnnounced && !AppVisibility.isForeground()) {
                OperationalVoicePlayer.play(
                    this,
                    OperationalCueCatalog.CONNECTION_RESTORED,
                    "voice_connection_restored",
                    true,
                    0L
                );
            }
            if (previousVersion > 0L && serverVersion > previousVersion && relevant) {
                boolean appIsForeground = AppVisibility.isForeground();
                boolean dumpPointAnnounced = showLatestDriverDumpPointAlert(
                    result.body,
                    preferences,
                    !appIsForeground
                );
                if (!dumpPointAnnounced) {
                    showLatestAssignmentAlert(
                        result.body,
                        !appIsForeground
                    );
                }
            }
            if (shiftCloseFlush == FlushResult.ATTENTION) {
                publishStatus("Откройте приложение — проверьте закрытие смены");
            } else {
                publishStatus("Связь работает во время смены");
            }
            scheduleHeartbeat(BuildConfig.HEARTBEAT_INTERVAL_MS);
        } catch (Exception error) {
            consecutiveFailures += 1;
            SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            boolean shouldAnnounceConnectionLoss = consecutiveFailures >= 2
                && !preferences.getBoolean(CONNECTION_LOSS_ANNOUNCED, false);
            preferences.edit()
                .putLong("last_failure_at", System.currentTimeMillis())
                .putString("last_error", error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage()))
                .putBoolean(CONNECTION_LOSS_ANNOUNCED, shouldAnnounceConnectionLoss
                    || preferences.getBoolean(CONNECTION_LOSS_ANNOUNCED, false))
                .apply();
            if (shouldAnnounceConnectionLoss && !AppVisibility.isForeground()) {
                OperationalVoicePlayer.play(
                    this,
                    OperationalCueCatalog.CONNECTION_LOST,
                    "voice_connection_lost",
                    true,
                    0L
                );
            }
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
            connection.setRequestProperty(
                "User-Agent",
                "CopperResourcesNative/" + BuildConfig.APP_PROFILE_ID
                    + "/" + BuildConfig.VERSION_NAME
            );

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

    private FlushResult flushPendingDriverShiftClose() throws Exception {
        PendingDriverShiftClose pending = PendingDriverShiftClose.load(this);
        if (pending == null) {
            return FlushResult.NONE;
        }
        if (pending.requiresAttention) {
            return FlushResult.ATTENTION;
        }
        HeartbeatResult result = requestDriverShiftClose(pending);
        if (result.statusCode >= 200 && result.statusCode < 300) {
            JSONObject response = new JSONObject(result.body);
            if (!response.optBoolean("ok", false)) {
                PendingDriverShiftClose.markAttention(this, pending.clientActionId, result.body);
                return FlushResult.ATTENTION;
            }
            if (!PendingDriverShiftClose.clear(this, pending.clientActionId)) {
                throw new IllegalStateException("Shift close acknowledgement was not saved");
            }
            publishStatus("Закрытие смены отправлено");
            return FlushResult.APPLIED;
        }
        // Any HTTP response proves that transport worked. Never present a server
        // rejection (including 5xx/redirects) as an offline close or retry it forever.
        PendingDriverShiftClose.markAttention(this, pending.clientActionId, result.body);
        return FlushResult.ATTENTION;
    }

    private HeartbeatResult requestDriverShiftClose(PendingDriverShiftClose pending) throws Exception {
        URL url = new URL(BuildConfig.APP_SERVER_URL + "driver/shift/close/");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        try {
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(10_000);
            connection.setRequestMethod("POST");
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setDoOutput(true);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
            connection.setRequestProperty("Cache-Control", "no-store");
            connection.setRequestProperty("X-Requested-With", "XMLHttpRequest");
            connection.setRequestProperty(
                "User-Agent",
                "CopperResourcesNative/" + BuildConfig.APP_PROFILE_ID
                    + "/" + BuildConfig.VERSION_NAME
            );
            String cookie = readWebViewCookie();
            if (cookie == null || cookie.isBlank()) {
                throw new IllegalStateException("WebView session cookie is unavailable");
            }
            String csrfToken = cookieValue(cookie, "csrftoken");
            if (csrfToken.isBlank()) {
                throw new IllegalStateException("CSRF cookie is unavailable");
            }
            connection.setRequestProperty("Cookie", cookie);
            connection.setRequestProperty("X-CSRFToken", csrfToken);
            connection.setRequestProperty("Origin", Uri.parse(BuildConfig.APP_SERVER_URL).buildUpon().path(null).build().toString());
            connection.setRequestProperty("Referer", BuildConfig.APP_START_URL);
            if (!BuildConfig.SYNC_AUTH_TOKEN.isBlank()) {
                connection.setRequestProperty(BuildConfig.SYNC_AUTH_HEADER, BuildConfig.SYNC_AUTH_TOKEN);
            }

            String encodedBody = formField("client_action_id", pending.clientActionId)
                + "&" + formField("shift_id", pending.shiftId)
                + "&" + formField("end_fuel", pending.endFuel)
                + "&" + formField("end_mileage", pending.endMileage)
                + "&" + formField("end_engine_hours", pending.endEngineHours)
                + "&" + formField("reading_confirmation_token", pending.confirmationToken);
            byte[] body = encodedBody.getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int statusCode = connection.getResponseCode();
            InputStream stream = statusCode >= 400 ? connection.getErrorStream() : connection.getInputStream();
            return new HeartbeatResult(statusCode, readResponse(stream));
        } finally {
            connection.disconnect();
        }
    }

    private static String formField(String name, String value) throws Exception {
        return URLEncoder.encode(name, StandardCharsets.UTF_8.name())
            + "=" + URLEncoder.encode(value, StandardCharsets.UTF_8.name());
    }

    private static String cookieValue(String cookieHeader, String name) {
        if (cookieHeader == null || name == null) {
            return "";
        }
        for (String part : cookieHeader.split(";")) {
            String value = part.trim();
            int separator = value.indexOf('=');
            if (separator > 0 && name.equals(value.substring(0, separator).trim())) {
                return value.substring(separator + 1).trim();
            }
        }
        return "";
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
        if (text.equals(publishedStatus)) {
            return;
        }
        try {
            NotificationManagerCompat.from(this).notify(
                AppNotifications.FOREGROUND_NOTIFICATION_ID,
                AppNotifications.foregroundNotification(this, text)
            );
            publishedStatus = text;
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

    private boolean showLatestDriverDumpPointAlert(
            String body,
            SharedPreferences preferences,
            boolean showNotification) {
        if (!BuildConfig.DRIVER_VOICE_ALERTS_ENABLED) {
            return false;
        }
        try {
            JSONArray events = new JSONObject(body).optJSONArray("events");
            if (events == null) {
                return false;
            }
            long lastAnnouncedVersion = preferences.getLong(LAST_DRIVER_DUMP_POINT_ALERT_VERSION, 0L);
            long selectedVersion = lastAnnouncedVersion;
            long selectedTripId = 0L;
            long selectedDumpPointId = 0L;
            String selectedDumpPointName = "";

            for (int index = 0; index < events.length(); index += 1) {
                JSONObject event = events.optJSONObject(index);
                if (event == null
                        || !"trip_changed".equals(event.optString("type"))
                        || event.optLong("version", 0L) <= selectedVersion) {
                    continue;
                }
                JSONObject payload = event.optJSONObject("payload");
                if (payload == null || !"truck_loaded".equals(payload.optString("action"))) {
                    continue;
                }
                long tripId = payload.optLong("trip_id", 0L);
                long dumpPointId = payload.optLong("assigned_dump_point_id", 0L);
                if (dumpPointId <= 0L) {
                    dumpPointId = payload.optLong("dump_point_id", 0L);
                }
                String dumpPointName = payload.optString("dump_point_name", "");
                if (tripId <= 0L || (dumpPointId <= 0L && dumpPointName.isBlank())) {
                    continue;
                }
                selectedVersion = event.optLong("version", 0L);
                selectedTripId = tripId;
                selectedDumpPointId = dumpPointId;
                selectedDumpPointName = dumpPointName;
            }

            if (selectedVersion <= lastAnnouncedVersion || selectedTripId <= 0L) {
                return false;
            }
            String displayName = DriverVoiceCatalog.displayNameFor(
                selectedDumpPointId,
                selectedDumpPointName
            );
            if (displayName.isEmpty()) {
                return false;
            }

            DriverDumpPointAnnouncer.Result result = DriverDumpPointAnnouncer.announce(
                this,
                selectedVersion,
                selectedTripId,
                selectedDumpPointId,
                displayName,
                showNotification,
                true
            );
            return result.announced;
        } catch (Exception error) {
            Log.w("ConnectivityForegroundService", "Driver dump-point alert was not parsed", error);
            return false;
        }
    }

    private boolean showLatestAssignmentAlert(String body, boolean showNotification) {
        try {
            JSONObject root = new JSONObject(body);
            JSONArray events = root.optJSONArray("events");
            if (events == null) {
                return false;
            }
            String roleCode = root.optString("role_app_code", BuildConfig.APP_PROFILE_ID);
            JSONArray workerEquipmentIds = root.optJSONArray("worker_equipment_ids");
            if ("excavator_operator".equals(roleCode)) {
                return showLatestExcavatorAssignmentAlerts(
                    events,
                    workerEquipmentIds,
                    showNotification
                );
            }
            long selectedVersion = 0L;
            String[] selectedVoices = new String[0];
            String selectedCue = OperationalCueCatalog.ASSIGNMENT;
            String selectedTitle = "";
            String selectedBody = "";

            for (int index = 0; index < events.length(); index += 1) {
                JSONObject event = events.optJSONObject(index);
                if (event == null || !"assignment_changed".equals(event.optString("type"))) {
                    continue;
                }
                long version = event.optLong("version", 0L);
                if (version <= selectedVersion) {
                    continue;
                }
                JSONObject payload = event.optJSONObject("payload");
                if (payload == null) {
                    continue;
                }
                String action = payload.optString("action", "");
                String[] voices = new String[0];
                String title = "";
                String message = "";

                if ("driver".equals(roleCode)) {
                    if ("assignment_pending".equals(action)) {
                        selectedCue = OperationalCueCatalog.ASSIGNMENT;
                        String assignmentVoice = EquipmentVoiceCatalog.driverExcavatorAssignmentVoice(
                            payload.optString("target_excavator_number", "")
                        );
                        voices = new String[] {
                            assignmentVoice.isEmpty() ? "voice_excavator_assigned" : assignmentVoice
                        };
                        title = "Новое назначение";
                        String excavatorNumber = payload.optString("target_excavator_number", "");
                        message = excavatorNumber.isBlank()
                            ? "Проверьте назначенный экскаватор."
                            : "Экскаватор № " + excavatorNumber;
                    } else if ("release_applied".equals(action)) {
                        selectedCue = OperationalCueCatalog.ASSIGNMENT_REMOVED;
                        voices = new String[] {"voice_assignment_removed"};
                        title = "Назначение снято";
                        message = "Ожидайте нового экскаватора.";
                    }
                }

                if (voices.length > 0) {
                    selectedVersion = version;
                    selectedVoices = voices;
                    selectedTitle = title;
                    selectedBody = message;
                }
            }

            if (selectedVersion <= 0L || selectedVoices.length == 0) {
                return false;
            }
            OperationalVoiceAnnouncer.Result result = OperationalVoiceAnnouncer.announceSequence(
                this,
                selectedCue,
                selectedVoices,
                selectedVersion,
                roleCode + "_assignment",
                showNotification,
                selectedTitle,
                selectedBody
            );
            return result.announced;
        } catch (Exception error) {
            Log.w("ConnectivityForegroundService", "Assignment voice was not parsed", error);
            return false;
        }
    }

    private boolean showLatestExcavatorAssignmentAlerts(
            JSONArray events,
            JSONArray workerEquipmentIds,
            boolean showNotification) {
        Map<String, ExcavatorAssignmentVoice> latestByTruck = new LinkedHashMap<>();
        for (int index = 0; index < events.length(); index += 1) {
            JSONObject event = events.optJSONObject(index);
            if (event == null || !"assignment_changed".equals(event.optString("type"))) {
                continue;
            }
            if (!"HaulAssignment".equals(event.optString("object_type", ""))) {
                continue;
            }
            long version = event.optLong("version", 0L);
            JSONObject payload = event.optJSONObject("payload");
            String assignmentId = event.optString("object_id", "").trim();
            if (version <= 0L || payload == null || assignmentId.isEmpty()) {
                continue;
            }
            String action = payload.optString("action", "");
            long targetExcavatorId = payload.optLong("target_excavator_id", 0L);
            long relatedExcavatorId = firstCommonId(
                workerEquipmentIds,
                payload.optJSONArray("excavator_ids")
            );
            String kind = "";
            long currentExcavatorId = 0L;
            if ("assignment_applied".equals(action)
                    && targetExcavatorId > 0L
                    && jsonArrayContains(workerEquipmentIds, targetExcavatorId)) {
                kind = "assign";
                currentExcavatorId = targetExcavatorId;
            } else if (("assignment_applied".equals(action) || "release_applied".equals(action))
                    && relatedExcavatorId > 0L) {
                kind = "remove";
                currentExcavatorId = relatedExcavatorId;
            }
            if (kind.isEmpty()) {
                continue;
            }

            long truckIdValue = payload.optLong("truck_id", 0L);
            JSONArray truckIds = payload.optJSONArray("truck_ids");
            if (truckIdValue <= 0L && truckIds != null) {
                truckIdValue = truckIds.optLong(0, 0L);
            }
            String truckId = String.valueOf(truckIdValue);
            String truckNumber = payload.optString("truck_number", "");
            if ("0".equals(truckId) && truckNumber.isBlank()) {
                continue;
            }
            String truckKey = !"0".equals(truckId)
                ? truckId
                : "number:" + truckNumber;
            String numberVoice = EquipmentVoiceCatalog.truckNumberVoice(truckNumber);
            String[] voices;
            if ("assign".equals(kind)) {
                voices = numberVoice.isEmpty()
                    ? new String[] {"voice_truck_assigned"}
                    : new String[] {"voice_truck_assigned_prefix", numberVoice};
            } else {
                voices = numberVoice.isEmpty()
                    ? new String[] {"voice_truck_removed"}
                    : new String[] {"voice_truck_removed_prefix", numberVoice};
            }
            ExcavatorAssignmentVoice previous = latestByTruck.get(truckKey);
            if (previous == null || version > previous.eventVersion) {
                latestByTruck.put(
                    truckKey,
                    new ExcavatorAssignmentVoice(
                        version,
                        "excavator-assignment:" + kind + ":" + assignmentId + ":" + currentExcavatorId,
                        voices,
                        kind,
                        truckNumber
                    )
                );
            }
        }
        if (latestByTruck.isEmpty()) {
            return false;
        }

        List<ExcavatorAssignmentVoice> selected = new ArrayList<>(latestByTruck.values());
        selected.sort((left, right) -> Long.compare(left.eventVersion, right.eventVersion));
        List<OperationalVoiceAnnouncer.Operation> operations = new ArrayList<>();
        boolean onlyRemovals = true;
        for (ExcavatorAssignmentVoice item : selected) {
            operations.add(new OperationalVoiceAnnouncer.Operation(item.operationKey, item.voiceNames));
            if (!"remove".equals(item.kind)) {
                onlyRemovals = false;
            }
        }
        ExcavatorAssignmentVoice latest = selected.get(selected.size() - 1);
        String notificationTitle = selected.size() > 1
            ? "Изменены назначения"
            : ("assign".equals(latest.kind) ? "Назначен самосвал" : "Самосвал снят");
        String notificationBody = selected.size() > 1
            ? "Изменений: " + selected.size()
            : (latest.truckNumber.isBlank()
                ? "Проверьте назначение на экране."
                : "Самосвал № " + latest.truckNumber);
        OperationalVoiceAnnouncer.Result result = OperationalVoiceAnnouncer.announceOperations(
            this,
            onlyRemovals
                ? OperationalCueCatalog.ASSIGNMENT_REMOVED
                : OperationalCueCatalog.ASSIGNMENT,
            operations,
            showNotification,
            notificationTitle,
            notificationBody
        );
        return result.announced;
    }

    private static long firstCommonId(JSONArray left, JSONArray right) {
        if (left == null || right == null) {
            return 0L;
        }
        for (int index = 0; index < left.length(); index += 1) {
            long candidate = left.optLong(index, 0L);
            if (candidate > 0L && jsonArrayContains(right, candidate)) {
                return candidate;
            }
        }
        return 0L;
    }

    private static final class ExcavatorAssignmentVoice {
        final long eventVersion;
        final String operationKey;
        final String[] voiceNames;
        final String kind;
        final String truckNumber;

        ExcavatorAssignmentVoice(
                long eventVersion,
                String operationKey,
                String[] voiceNames,
                String kind,
                String truckNumber) {
            this.eventVersion = eventVersion;
            this.operationKey = operationKey;
            this.voiceNames = voiceNames;
            this.kind = kind;
            this.truckNumber = truckNumber == null ? "" : truckNumber;
        }
    }

    private static boolean jsonArrayContains(JSONArray values, long expected) {
        if (values == null || expected <= 0L) {
            return false;
        }
        for (int index = 0; index < values.length(); index += 1) {
            if (values.optLong(index, 0L) == expected) {
                return true;
            }
        }
        return false;
    }

    private String currentStatusText() {
        PendingDriverShiftClose pending = PendingDriverShiftClose.load(this);
        if (pending != null) {
            return pending.requiresAttention
                ? "Откройте приложение — проверьте закрытие смены"
                : "Закрытие смены сохранено — ждём связь";
        }
        return ConnectionState.lastAliveAt(this) > 0L
            ? "Связь работает во время смены"
            : "Проверяем связь с сервером…";
    }

    private void stopBecauseConnectionIsNotRequired(String reason) {
        ConnectionState.disable(this, reason);
        stopServiceAndRemoveNotification();
    }

    private void stopServiceAndRemoveNotification() {
        synchronized (scheduleLock) {
            if (pendingHeartbeat != null) {
                pendingHeartbeat.cancel(true);
                pendingHeartbeat = null;
            }
        }
        if (foregroundStarted) {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
            foregroundStarted = false;
        }
        try {
            NotificationManagerCompat.from(this).cancel(AppNotifications.FOREGROUND_NOTIFICATION_ID);
        } catch (SecurityException ignored) {}
        stopSelf();
    }

    private static final class HeartbeatResult {
        final int statusCode;
        final String body;

        HeartbeatResult(int statusCode, String body) {
            this.statusCode = statusCode;
            this.body = body;
        }
    }

    private enum FlushResult {
        NONE,
        APPLIED,
        ATTENTION
    }
}
