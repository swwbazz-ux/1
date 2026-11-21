package ru.copperresources.mobile;

import android.app.PendingIntent;
import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.webkit.WebView;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONObject;

/** Installs a signed release of this dedicated device's own package during shift handover. */
public final class TabletAutoUpdate {
    private static final String TAG = "MiningMasterTablet";
    private static final String PROFILE = "mining_master_tablet";
    private static final String RELEASES = "https://driverform.ru/media/apk/";
    private static final String RESULT = "ru.copperresources.mobile.TABLET_UPDATE_RESULT";
    private static final long MAX_APK = 96L * 1024 * 1024;
    private static final long CHECK_MS = 5 * 60_000L;
    private static final long GATE_MS = 10_000L;
    private static final long RETRY_MS = 60_000L;
    private static TabletAutoUpdate current;

    private final MiningMasterTabletActivity activity;
    private final WebView view;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final PackageInstaller installer;
    private boolean resumed, checking, installing, committed;
    private long nextCheck, nextGate;
    private String request, lastDeferral;
    private Release pending;
    private int installSession = -1;

    TabletAutoUpdate(MiningMasterTabletActivity activity, WebView view) {
        this.activity = activity;
        this.view = view;
        installer = activity.getPackageManager().getPackageInstaller();
        current = this;
    }

    void resume() {
        resumed = true;
        main.removeCallbacks(tick);
        main.postDelayed(tick, 2_000);
    }

    void pause() {
        resumed = false;
        main.removeCallbacks(tick);
        clearRequest();
    }

    void pageStarted() {
        clearRequest();
    }

    void destroy() {
        pause();
        if (current == this) current = null;
        worker.shutdown();
    }

    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (!resumed) return;
            long now = SystemClock.elapsedRealtime();
            if (isOwner() && !installing) {
                if (!checking && request == null && now >= nextCheck) check();
                else if (!checking && pending != null && request == null && now >= nextGate
                        && activity.hasWindowFocus() && TabletUrlPolicy.workplaceNavigation(view.getUrl())) {
                    String token = UUID.randomUUID().toString();
                    request = token;
                    view.evaluateJavascript("window.FieldIdentity?.reserveUpdate(" + JSONObject.quote(token) + ")", null);
                    main.postDelayed(() -> { if (token.equals(request) && !installing) clearRequest(); }, 4_000);
                }
            }
            main.postDelayed(this, GATE_MS);
        }
    };

    private boolean isOwner() {
        DevicePolicyManager policy = activity.getSystemService(DevicePolicyManager.class);
        return Build.VERSION.SDK_INT >= 28 && policy != null && policy.isDeviceOwnerApp(activity.getPackageName());
    }

    private void check() {
        checking = true;
        worker.execute(() -> {
            try {
                Release found = fetchRelease();
                main.post(() -> {
                    checking = false;
                    nextCheck = SystemClock.elapsedRealtime() + CHECK_MS;
                    if (found != null && (pending == null || found.code != pending.code))
                        Log.i(TAG, "tablet_update event=download_verified version=" + found.code);
                    pending = found;
                });
            } catch (Exception failure) {
                main.post(() -> {
                    checking = false;
                    pending = null;
                    nextCheck = SystemClock.elapsedRealtime() + RETRY_MS;
                    Log.w(TAG, "tablet_update event=check_retry type=" + failure.getClass().getSimpleName());
                });
            }
        });
    }

    private Release fetchRelease() throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        download(RELEASES + PROFILE + "-update.json", bytes, 8192);
        JSONObject feed = new JSONObject(bytes.toString(StandardCharsets.UTF_8.name()));
        int code = feed.getInt("versionCode");
        String name = feed.getString("versionName");
        String hash = feed.getString("sha256");
        if (feed.getInt("schemaVersion") != 1 || !PROFILE.equals(feed.getString("profile"))
                || code <= 0 || !name.matches("[0-9]{1,6}(\\.[0-9]{1,6}){2}") || !hash.matches("[a-f0-9]{64}"))
            throw new IOException("update manifest");
        String apkUrl = RELEASES + PROFILE + "-" + name + ".apk";
        if (!apkUrl.equals(feed.getString("apkUrl"))) throw new IOException("update APK URL");

        File apk = new File(activity.getNoBackupFilesDir(), "tablet-update.apk");
        if (code <= BuildConfig.VERSION_CODE) {
            if (apk.isFile()) apk.delete();
            return null;
        }
        Release release = new Release(code, name, hash, apk);
        if (apk.isFile() && hash.equals(sha256(apk))) {
            verifyPackage(release, apk);
            return release;
        }
        File partial = new File(apk.getParentFile(), "tablet-update.part");
        try {
            try (FileOutputStream output = new FileOutputStream(partial)) {
                download(apkUrl, output, MAX_APK);
                output.getFD().sync();
            }
            if (!hash.equals(sha256(partial))) throw new IOException("update APK digest");
            verifyPackage(release, partial);
            if (!partial.renameTo(apk)) throw new IOException("update APK move");
        } finally {
            if (partial.exists()) partial.delete();
        }
        return release;
    }

    private void verifyPackage(Release release, File file) throws Exception {
        PackageManager packages = activity.getPackageManager();
        PackageInfo candidate = packages.getPackageArchiveInfo(file.getPath(), PackageManager.GET_SIGNING_CERTIFICATES);
        PackageInfo installed = packages.getPackageInfo(activity.getPackageName(), PackageManager.GET_SIGNING_CERTIFICATES);
        if (candidate == null || candidate.signingInfo == null || installed.signingInfo == null
                || !installed.packageName.equals(candidate.packageName) || candidate.getLongVersionCode() != release.code
                || !release.name.equals(candidate.versionName) || candidate.getLongVersionCode() <= installed.getLongVersionCode())
            throw new SecurityException("update APK identity");
        Signature[] oldSigners = installed.signingInfo.getApkContentsSigners();
        Signature[] newSigners = candidate.signingInfo.getApkContentsSigners();
        if (oldSigners.length != 1 || newSigners.length != 1 || !oldSigners[0].equals(newSigners[0]))
            throw new SecurityException("update APK signer");
    }

    void answer(WebView sender, String token, String reason) {
        if (sender != view || pending == null || request == null || !request.equals(token) || installing) return;
        if (!"ready".equals(reason) || !safeWindow()) {
            if (!reason.equals(lastDeferral)) {
                lastDeferral = reason;
                Log.i(TAG, "tablet_update event=deferred version=" + pending.code + " reason="
                    + ("shift".equals(reason) ? "shift" : "waiting_for_handover"));
            }
            clearRequest();
            return;
        }
        Release release = pending;
        installing = true;
        worker.execute(() -> stage(release, token));
    }

    private boolean safeWindow() {
        return resumed && pending != null && isOwner() && !activity.isFinishing()
            && activity.hasWindowFocus() && "login".equals(TabletUrlPolicy.diagnosticRoute(view.getUrl()));
    }

    private void stage(Release release, String token) {
        int id = -1;
        try {
            PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            params.setAppPackageName(activity.getPackageName());
            params.setSize(release.apk.length());
            id = installer.createSession(params);
            try (PackageInstaller.Session session = installer.openSession(id);
                    InputStream input = new FileInputStream(release.apk);
                    OutputStream output = session.openWrite("base.apk", 0, release.apk.length())) {
                copy(input, output, MAX_APK, null);
                session.fsync(output);
            }
            int prepared = id;
            main.post(() -> commit(prepared, release, token));
        } catch (Exception failure) {
            int failed = id;
            main.post(() -> failInstall(failed, "stage"));
        }
    }

    private void commit(int id, Release release, String token) {
        if (current != this || !token.equals(request) || !safeWindow()) {
            failInstall(id, "handover_ended");
            return;
        }
        installSession = id;
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
        Intent result = new Intent(activity, Result.class).setAction(RESULT).putExtra("version", release.code);
        PendingIntent callback = PendingIntent.getBroadcast(activity, id, result, flags);
        try (PackageInstaller.Session session = installer.openSession(id)) {
            session.commit(callback.getIntentSender());
            committed = true;
            Log.i(TAG, "tablet_update event=committed version=" + release.code);
        } catch (Exception failure) {
            failInstall(id, "commit");
        }
    }

    private void failInstall(int id, String reason) {
        if (id >= 0) {
            try { installer.abandonSession(id); } catch (RuntimeException finished) { /* Already finalized. */ }
        }
        Log.w(TAG, "tablet_update event=install_retry reason=" + reason);
        installing = false;
        committed = false;
        installSession = -1;
        clearRequest();
        nextGate = SystemClock.elapsedRealtime() + RETRY_MS;
    }

    private void clearRequest() {
        if (request == null || committed) return;
        String token = request;
        request = null;
        if (!activity.isDestroyed())
            view.evaluateJavascript("window.FieldIdentity?.releaseUpdate(" + JSONObject.quote(token) + ")", null);
    }

    public static final class Result extends BroadcastReceiver {
        @Override public void onReceive(Context context, Intent intent) {
            if (!RESULT.equals(intent.getAction())) return;
            int id = intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1);
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
            Log.i(TAG, "tablet_update event=install_result status=" + status + " version=" + intent.getIntExtra("version", -1));
            if (status != PackageInstaller.STATUS_SUCCESS && current != null && current.installSession == id)
                current.failInstall(id, "status_" + status);
        }
    }

    private static void download(String url, OutputStream output, long limit) throws IOException {
        HttpsURLConnection connection = (HttpsURLConnection) new URL(url).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(12_000);
            connection.setReadTimeout(15_000);
            connection.setUseCaches(false);
            connection.setRequestProperty("Cache-Control", "no-cache");
            if (connection.getResponseCode() != 200 || connection.getContentLengthLong() > limit)
                throw new IOException("update download");
            try (InputStream input = connection.getInputStream()) { copy(input, output, limit, null); }
        } finally { connection.disconnect(); }
    }

    private static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new FileInputStream(file)) { copy(input, null, MAX_APK, digest); }
        StringBuilder hex = new StringBuilder();
        for (byte b : digest.digest()) hex.append(String.format(Locale.ROOT, "%02x", b & 255));
        return hex.toString();
    }

    private static void copy(InputStream input, OutputStream output, long limit, MessageDigest digest) throws IOException {
        byte[] buffer = new byte[64 * 1024];
        long total = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > limit) throw new IOException("update exceeds size limit");
            if (output != null) output.write(buffer, 0, count);
            if (digest != null) digest.update(buffer, 0, count);
        }
    }

    private static final class Release {
        final int code;
        final String name, hash;
        final File apk;
        Release(int code, String name, String hash, File apk) {
            this.code = code;
            this.name = name;
            this.hash = hash;
            this.apk = apk;
        }
    }
}
