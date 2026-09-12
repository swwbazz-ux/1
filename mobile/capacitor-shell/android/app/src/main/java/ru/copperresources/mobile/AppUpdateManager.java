package ru.copperresources.mobile;

import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.appcompat.app.AlertDialog;
import androidx.core.content.FileProvider;
import androidx.core.content.pm.PackageInfoCompat;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

final class AppUpdateManager {
    private static final String PREFS_NAME = "native_app_updates";
    private static final String DEFERRED_VERSION_CODE = "deferred_version_code";
    private static final String CACHED_VERSION_CODE = "cached_version_code";
    private static final String CACHED_VERSION_NAME = "cached_version_name";
    private static final String CACHED_APK_URL = "cached_apk_url";
    private static final String CACHED_SHA256 = "cached_sha256";
    private static final String CACHED_RELEASE_NOTES = "cached_release_notes";
    private static final String LAST_CHECK_COMPLETED_AT = "last_check_completed_at";
    private static final String LAST_CHECK_SUCCEEDED = "last_check_succeeded";
    private static final int MAX_MANIFEST_BYTES = 64 * 1024;
    private static final int MAX_APK_BYTES = 200 * 1024 * 1024;
    static final long SUCCESS_RESUME_MIN_INTERVAL_MS = 5L * 60L * 1000L;
    static final long FAILURE_RETRY_INTERVAL_MS = 60L * 1000L;
    private static final long PAGE_INDICATOR_RETRY_MS = 1_500L;

    private final MainActivity activity;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "native-app-update");
        thread.setDaemon(true);
        return thread;
    });
    private final Runnable periodicCheck = () -> checkForUpdates(true);
    private final Runnable delayedIndicatorRefresh = this::refreshPageIndicator;

    private WebView webView;
    private UpdateInfo latestUpdate;
    private File pendingInstallerFile;
    private boolean checkRunning;
    private boolean downloadRunning;
    private boolean promptVisible;
    private boolean hostResumed;
    private boolean pageLoaded;
    private boolean destroyed;
    private long lastCheckCompletedAtMs;
    private boolean lastCheckSucceeded;

    AppUpdateManager(MainActivity activity) {
        this.activity = activity;
        SharedPreferences preferences = updatePreferences();
        latestUpdate = restoreCachedUpdate(preferences, BuildConfig.VERSION_CODE);
        lastCheckCompletedAtMs = preferences.getLong(LAST_CHECK_COMPLETED_AT, 0L);
        lastCheckSucceeded = preferences.getBoolean(LAST_CHECK_SUCCEEDED, false);
        long nowMs = System.currentTimeMillis();
        if (lastCheckCompletedAtMs < 0L || lastCheckCompletedAtMs > nowMs) {
            lastCheckCompletedAtMs = 0L;
            lastCheckSucceeded = false;
            preferences.edit()
                .remove(LAST_CHECK_COMPLETED_AT)
                .remove(LAST_CHECK_SUCCEEDED)
                .apply();
        }
    }

    void attach(WebView attachedWebView) {
        webView = attachedWebView;
        webView.addJavascriptInterface(new UpdateBridge(), "CopperResourcesUpdate");
        refreshPageIndicator();
    }

    void onPageStarted(WebView loadingWebView) {
        webView = loadingWebView;
        pageLoaded = false;
        mainHandler.removeCallbacks(delayedIndicatorRefresh);
    }

    void onPageLoaded(WebView loadedWebView) {
        webView = loadedWebView;
        pageLoaded = true;
        refreshPageIndicator();
        mainHandler.removeCallbacks(delayedIndicatorRefresh);
        mainHandler.postDelayed(delayedIndicatorRefresh, PAGE_INDICATOR_RETRY_MS);
        if (latestUpdate != null && shouldPrompt(latestUpdate)) {
            showUpdatePrompt(false);
        }
    }

    void onHostResumed() {
        hostResumed = true;
        refreshPageIndicator();
        if (pendingInstallerFile != null && canInstallPackages()) {
            File readyFile = pendingInstallerFile;
            pendingInstallerFile = null;
            launchSystemInstaller(readyFile);
        }
        long nowMs = System.currentTimeMillis();
        if (shouldCheckOnResume(lastCheckCompletedAtMs, lastCheckSucceeded, nowMs)) {
            checkForUpdates(true);
        } else {
            scheduleNextCheck(nowMs);
        }
    }

    void onHostPaused() {
        hostResumed = false;
        mainHandler.removeCallbacks(periodicCheck);
    }

    void destroy() {
        destroyed = true;
        mainHandler.removeCallbacks(periodicCheck);
        mainHandler.removeCallbacks(delayedIndicatorRefresh);
        executor.shutdownNow();
        if (webView != null) {
            webView.removeJavascriptInterface("CopperResourcesUpdate");
        }
        webView = null;
    }

    private void checkForUpdates(boolean allowPrompt) {
        if (destroyed || checkRunning) {
            return;
        }
        checkRunning = true;
        executor.execute(() -> {
            UpdateInfo result = null;
            boolean manifestReadSucceeded = false;
            try {
                result = readUpdateManifest();
                manifestReadSucceeded = true;
            } catch (Exception ignored) {
                // Сбой сети не отменяет последнее подтверждённое обновление.
            }
            UpdateInfo finalResult = result;
            boolean finalManifestReadSucceeded = manifestReadSucceeded;
            mainHandler.post(() -> {
                checkRunning = false;
                if (destroyed) {
                    return;
                }
                long completedAtMs = System.currentTimeMillis();
                recordCheckResult(finalManifestReadSucceeded, completedAtMs);
                latestUpdate = resolveLatestUpdate(
                    latestUpdate,
                    finalManifestReadSucceeded,
                    finalResult,
                    BuildConfig.VERSION_CODE
                );
                if (finalManifestReadSucceeded) {
                    if (latestUpdate != null) {
                        persistCachedUpdate(updatePreferences(), latestUpdate);
                    } else {
                        clearCachedUpdate(updatePreferences());
                    }
                }
                refreshPageIndicator();
                if (allowPrompt && latestUpdate != null && shouldPrompt(latestUpdate)) {
                    showUpdatePrompt(false);
                }
                scheduleNextCheck();
            });
        });
    }

    private void scheduleNextCheck() {
        scheduleNextCheck(System.currentTimeMillis());
    }

    private void scheduleNextCheck(long nowMs) {
        mainHandler.removeCallbacks(periodicCheck);
        if (hostResumed && !destroyed) {
            mainHandler.postDelayed(
                periodicCheck,
                nextScheduledDelay(
                    lastCheckCompletedAtMs,
                    lastCheckSucceeded,
                    nowMs,
                    BuildConfig.UPDATE_CHECK_INTERVAL_MS
                )
            );
        }
    }

    private void recordCheckResult(boolean succeeded, long completedAtMs) {
        lastCheckCompletedAtMs = completedAtMs;
        lastCheckSucceeded = succeeded;
        updatePreferences().edit()
            .putLong(LAST_CHECK_COMPLETED_AT, completedAtMs)
            .putBoolean(LAST_CHECK_SUCCEEDED, succeeded)
            .apply();
    }

    private SharedPreferences updatePreferences() {
        return activity.getSharedPreferences(PREFS_NAME, MainActivity.MODE_PRIVATE);
    }

    static UpdateInfo resolveLatestUpdate(
        UpdateInfo previous,
        boolean manifestReadSucceeded,
        UpdateInfo manifest,
        int currentVersionCode
    ) {
        if (!manifestReadSucceeded) {
            return previous;
        }
        return manifest != null && manifest.versionCode > currentVersionCode ? manifest : null;
    }

    static boolean shouldCheckOnResume(
        long lastCompletedAtMs,
        boolean lastSucceeded,
        long nowMs
    ) {
        if (lastCompletedAtMs <= 0L || nowMs < lastCompletedAtMs) {
            return true;
        }
        long minimumIntervalMs = lastSucceeded
            ? SUCCESS_RESUME_MIN_INTERVAL_MS
            : FAILURE_RETRY_INTERVAL_MS;
        return nowMs - lastCompletedAtMs >= minimumIntervalMs;
    }

    static long nextScheduledDelay(
        long lastCompletedAtMs,
        boolean lastSucceeded,
        long nowMs,
        long successIntervalMs
    ) {
        if (lastCompletedAtMs <= 0L || nowMs < lastCompletedAtMs) {
            return 0L;
        }
        long intervalMs = lastSucceeded ? successIntervalMs : FAILURE_RETRY_INTERVAL_MS;
        return Math.max(0L, intervalMs - (nowMs - lastCompletedAtMs));
    }

    static void persistCachedUpdate(SharedPreferences preferences, UpdateInfo update) {
        preferences.edit()
            .putInt(CACHED_VERSION_CODE, update.versionCode)
            .putString(CACHED_VERSION_NAME, update.versionName)
            .putString(CACHED_APK_URL, update.apkUrl)
            .putString(CACHED_SHA256, update.sha256)
            .putString(CACHED_RELEASE_NOTES, update.releaseNotes)
            .apply();
    }

    static UpdateInfo restoreCachedUpdate(SharedPreferences preferences, int currentVersionCode) {
        int versionCode = preferences.getInt(CACHED_VERSION_CODE, 0);
        String versionName = preferences.getString(CACHED_VERSION_NAME, "");
        String apkUrl = preferences.getString(CACHED_APK_URL, "");
        String sha256 = preferences.getString(CACHED_SHA256, "");
        String releaseNotes = preferences.getString(CACHED_RELEASE_NOTES, "");
        versionName = versionName == null ? "" : versionName.trim();
        apkUrl = apkUrl == null ? "" : apkUrl.trim();
        sha256 = sha256 == null ? "" : sha256.trim().toLowerCase(Locale.ROOT);
        releaseNotes = releaseNotes == null ? "" : releaseNotes.trim();
        if (versionCode <= currentVersionCode
                || versionName.isEmpty()
                || !apkUrl.startsWith("https://")
                || !sha256.matches("[0-9a-f]{64}")) {
            clearCachedUpdate(preferences);
            return null;
        }
        return new UpdateInfo(versionCode, versionName, apkUrl, sha256, releaseNotes);
    }

    static void clearCachedUpdate(SharedPreferences preferences) {
        preferences.edit()
            .remove(CACHED_VERSION_CODE)
            .remove(CACHED_VERSION_NAME)
            .remove(CACHED_APK_URL)
            .remove(CACHED_SHA256)
            .remove(CACHED_RELEASE_NOTES)
            .apply();
    }

    private UpdateInfo readUpdateManifest() throws Exception {
        URL url = new URL(BuildConfig.UPDATE_MANIFEST_URL);
        if (!"https".equalsIgnoreCase(url.getProtocol())) {
            throw new SecurityException("Update manifest must use HTTPS");
        }
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        try {
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(15_000);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Cache-Control", "no-store");
            connection.setRequestProperty(
                "User-Agent",
                "CopperResourcesNative/" + BuildConfig.APP_PROFILE_ID + "/" + BuildConfig.VERSION_NAME
            );
            if (connection.getResponseCode() != HttpURLConnection.HTTP_OK) {
                throw new IllegalStateException("Manifest HTTP " + connection.getResponseCode());
            }
            JSONObject json = new JSONObject(readLimited(connection.getInputStream(), MAX_MANIFEST_BYTES));
            if (json.optInt("schemaVersion", 0) != 1) {
                throw new IllegalArgumentException("Unsupported update manifest schema");
            }
            if (!BuildConfig.APP_PROFILE_ID.equals(json.optString("profile"))) {
                throw new SecurityException("Update profile mismatch");
            }
            int versionCode = json.optInt("versionCode", 0);
            String versionName = json.optString("versionName", "").trim();
            String apkUrl = json.optString("apkUrl", "").trim();
            String sha256 = json.optString("sha256", "").trim().toLowerCase(Locale.ROOT);
            String releaseNotes = json.optString("releaseNotes", "").trim();
            if (versionCode < 1 || versionName.isEmpty()) {
                throw new IllegalArgumentException("Invalid update version");
            }
            URL parsedApkUrl = new URL(apkUrl);
            if (!"https".equalsIgnoreCase(parsedApkUrl.getProtocol())) {
                throw new SecurityException("Update APK must use HTTPS");
            }
            if (!sha256.matches("[0-9a-f]{64}")) {
                throw new SecurityException("Invalid update checksum");
            }
            return new UpdateInfo(versionCode, versionName, apkUrl, sha256, releaseNotes);
        } finally {
            connection.disconnect();
        }
    }

    private boolean shouldPrompt(UpdateInfo update) {
        if (!hostResumed || !pageLoaded || promptVisible || downloadRunning) {
            return false;
        }
        SharedPreferences preferences = activity.getSharedPreferences(PREFS_NAME, MainActivity.MODE_PRIVATE);
        return preferences.getInt(DEFERRED_VERSION_CODE, 0) != update.versionCode;
    }

    private void showUpdatePrompt(boolean openedFromIndicator) {
        UpdateInfo update = latestUpdate;
        if (update == null || promptVisible || downloadRunning || activity.isFinishing()) {
            if (openedFromIndicator && update == null) {
                Toast.makeText(activity, "Установлена актуальная версия", Toast.LENGTH_SHORT).show();
            }
            return;
        }
        promptVisible = true;
        StringBuilder message = new StringBuilder()
            .append("Доступна версия ").append(update.versionName).append(".");
        if (!update.releaseNotes.isEmpty()) {
            message.append("\n\n").append(update.releaseNotes);
        }
        message.append("\n\nМожно продолжить работу и обновить позже со вкладки «Смена».");

        AlertDialog dialog = new AlertDialog.Builder(activity)
            .setTitle("Обновление приложения")
            .setMessage(message.toString())
            .setNegativeButton("Позже", (ignored, which) -> defer(update))
            .setPositiveButton("Обновить сейчас", (ignored, which) -> downloadAndInstall(update))
            .setOnDismissListener(ignored -> promptVisible = false)
            .create();
        dialog.setCancelable(false);
        dialog.show();
    }

    private void defer(UpdateInfo update) {
        activity.getSharedPreferences(PREFS_NAME, MainActivity.MODE_PRIVATE)
            .edit()
            .putInt(DEFERRED_VERSION_CODE, update.versionCode)
            .apply();
        refreshPageIndicator();
    }

    private void downloadAndInstall(UpdateInfo update) {
        if (downloadRunning || destroyed) {
            return;
        }
        downloadRunning = true;
        refreshPageIndicator();
        Toast.makeText(
            activity,
            "Обновление скачивается. Можно продолжать работу.",
            Toast.LENGTH_LONG
        ).show();
        executor.execute(() -> {
            try {
                File apk = downloadApk(update);
                verifyApk(apk, update);
                mainHandler.post(() -> {
                    downloadRunning = false;
                    refreshPageIndicator();
                    requestInstall(apk);
                });
            } catch (Exception error) {
                mainHandler.post(() -> {
                    downloadRunning = false;
                    refreshPageIndicator();
                    Toast.makeText(
                        activity,
                        "Не удалось скачать обновление. Попробуйте позже.",
                        Toast.LENGTH_LONG
                    ).show();
                });
            }
        });
    }

    private File downloadApk(UpdateInfo update) throws Exception {
        File updateDirectory = new File(activity.getCacheDir(), "app-updates");
        if (!updateDirectory.exists() && !updateDirectory.mkdirs()) {
            throw new IllegalStateException("Cannot create update directory");
        }
        File destination = new File(updateDirectory, "update-" + update.versionCode + ".apk");
        if (destination.isFile()) {
            try {
                verifyApk(destination, update);
                return destination;
            } catch (Exception ignored) {
                if (!destination.delete()) {
                    throw new IllegalStateException("Cannot replace invalid update");
                }
            }
        }
        File temporary = new File(updateDirectory, "update-" + update.versionCode + ".part");
        if (temporary.exists() && !temporary.delete()) {
            throw new IllegalStateException("Cannot reset partial update");
        }

        HttpURLConnection connection = (HttpURLConnection) new URL(update.apkUrl).openConnection();
        try {
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(60_000);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/vnd.android.package-archive");
            connection.setRequestProperty("Cache-Control", "no-store");
            if (connection.getResponseCode() != HttpURLConnection.HTTP_OK) {
                throw new IllegalStateException("APK HTTP " + connection.getResponseCode());
            }
            int total = 0;
            byte[] buffer = new byte[32 * 1024];
            try (InputStream input = connection.getInputStream();
                 FileOutputStream output = new FileOutputStream(temporary)) {
                int count;
                while ((count = input.read(buffer)) != -1) {
                    total += count;
                    if (total > MAX_APK_BYTES) {
                        throw new IllegalStateException("APK is too large");
                    }
                    output.write(buffer, 0, count);
                }
                output.getFD().sync();
            }
        } finally {
            connection.disconnect();
        }
        if (!temporary.renameTo(destination)) {
            throw new IllegalStateException("Cannot finalize downloaded update");
        }
        return destination;
    }

    private void verifyApk(File apk, UpdateInfo update) throws Exception {
        if (!update.sha256.equals(sha256(apk))) {
            throw new SecurityException("Update checksum mismatch");
        }
        PackageManager packageManager = activity.getPackageManager();
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
            ? PackageManager.GET_SIGNING_CERTIFICATES
            : PackageManager.GET_SIGNATURES;
        PackageInfo archive = packageManager.getPackageArchiveInfo(apk.getAbsolutePath(), flags);
        PackageInfo installed = packageManager.getPackageInfo(activity.getPackageName(), flags);
        if (archive == null || !activity.getPackageName().equals(archive.packageName)) {
            throw new SecurityException("Update package mismatch");
        }
        if (PackageInfoCompat.getLongVersionCode(archive) != update.versionCode) {
            throw new SecurityException("Update version mismatch");
        }
        if (!signers(installed).equals(signers(archive))) {
            throw new SecurityException("Update signing certificate mismatch");
        }
    }

    private Set<String> signers(PackageInfo packageInfo) throws Exception {
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P && packageInfo.signingInfo != null) {
            signatures = packageInfo.signingInfo.getApkContentsSigners();
        } else {
            signatures = packageInfo.signatures;
        }
        Set<String> result = new HashSet<>();
        if (signatures != null) {
            for (Signature signature : signatures) {
                result.add(bytesToHex(MessageDigest.getInstance("SHA-256").digest(signature.toByteArray())));
            }
        }
        if (result.isEmpty()) {
            throw new SecurityException("APK signer is missing");
        }
        return result;
    }

    private String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buffer = new byte[32 * 1024];
        try (FileInputStream input = new FileInputStream(file)) {
            int count;
            while ((count = input.read(buffer)) != -1) {
                digest.update(buffer, 0, count);
            }
        }
        return bytesToHex(digest.digest());
    }

    private String bytesToHex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        }
        return result.toString();
    }

    private void requestInstall(File apk) {
        if (!canInstallPackages()) {
            pendingInstallerFile = apk;
            Intent permission = new Intent(
                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:" + activity.getPackageName())
            );
            activity.startActivity(permission);
            Toast.makeText(
                activity,
                "Разрешите приложению устанавливать свои обновления",
                Toast.LENGTH_LONG
            ).show();
            return;
        }
        launchSystemInstaller(apk);
    }

    private boolean canInstallPackages() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.O
            || activity.getPackageManager().canRequestPackageInstalls();
    }

    private void launchSystemInstaller(File apk) {
        Uri apkUri = FileProvider.getUriForFile(
            activity,
            activity.getPackageName() + ".fileprovider",
            apk
        );
        Intent install = new Intent(Intent.ACTION_VIEW)
            .setDataAndType(apkUri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        activity.startActivity(install);
    }

    private void refreshPageIndicator() {
        WebView target = webView;
        if (target == null || destroyed) {
            return;
        }
        boolean available = latestUpdate != null;
        String currentVersion = JSONObject.quote(BuildConfig.VERSION_NAME);
        String latestVersion = JSONObject.quote(available ? latestUpdate.versionName : "");
        String script = "(function(){" +
            /* The <body> carries the client version for session telemetry too.
               Restrict mutations to the dedicated visible badge: assigning
               textContent to <body> destroys the entire rendered application. */
            "var nodes=document.querySelectorAll('.native-app-version[data-native-app-version]');" +
            "var available=" + available + ",downloading=" + downloadRunning + ";" +
            "var current=" + currentVersion + ",latest=" + latestVersion + ";" +
            "nodes.forEach(function(node){" +
                "node.hidden=false;node.textContent='Версия '+current;" +
                "node.classList.toggle('is-update-available',available);" +
                "node.classList.toggle('is-update-downloading',downloading);" +
                "if(available){" +
                    "node.setAttribute('role','button');node.tabIndex=0;" +
                    "node.setAttribute('aria-label',downloading?'Обновление скачивается':'Версия '+current+'. Доступна версия '+latest+'. Нажмите, чтобы обновить');" +
                    "node.onclick=function(){if(!downloading&&window.CopperResourcesUpdate){window.CopperResourcesUpdate.requestUpdate();}};" +
                    "node.onkeydown=function(event){if((event.key==='Enter'||event.key===' ')&&node.onclick){event.preventDefault();node.onclick();}};" +
                "}else{" +
                    "node.removeAttribute('role');node.removeAttribute('tabindex');node.onclick=null;node.onkeydown=null;" +
                    "node.setAttribute('aria-label','Текущая версия приложения '+current);" +
                "}" +
            "});" +
        "})();";
        target.post(() -> target.evaluateJavascript(script, null));
    }

    private String readLimited(InputStream input, int limit) throws Exception {
        if (input == null) {
            return "";
        }
        try (InputStream stream = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int total = 0;
            int count;
            while ((count = stream.read(buffer)) != -1) {
                total += count;
                if (total > limit) {
                    throw new IllegalStateException("Response is too large");
                }
                output.write(buffer, 0, count);
            }
            return output.toString("UTF-8");
        }
    }

    private final class UpdateBridge {
        @JavascriptInterface
        public void requestUpdate() {
            mainHandler.post(() -> showUpdatePrompt(true));
        }

        @JavascriptInterface
        public void refreshIndicator() {
            mainHandler.post(AppUpdateManager.this::refreshPageIndicator);
        }
    }

    static final class UpdateInfo {
        final int versionCode;
        final String versionName;
        final String apkUrl;
        final String sha256;
        final String releaseNotes;

        UpdateInfo(int versionCode, String versionName, String apkUrl, String sha256, String releaseNotes) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.apkUrl = apkUrl;
            this.sha256 = sha256;
            this.releaseNotes = releaseNotes;
        }
    }
}
