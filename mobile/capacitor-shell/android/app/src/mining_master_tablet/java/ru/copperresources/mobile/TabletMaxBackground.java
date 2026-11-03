package ru.copperresources.mobile;

import android.app.Activity;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONArray;
import org.json.JSONObject;

/** Encrypted durable outbox. Networking never starts an Activity or controls another application's UI. */
final class TabletMaxBackground {
    interface Result { void done(JSONObject value); void failed(String message); }
    private static final int JOB = 84350;
    private static TabletMaxBackground instance;
    static synchronized TabletMaxBackground get(Context context) {
        if (instance == null) instance = new TabletMaxBackground(context.getApplicationContext());
        return instance;
    }
    private final Context context;
    private final TabletMaxSession store;
    private final ExecutorService control = Executors.newSingleThreadExecutor(), network = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private WeakReference<Activity> activity = new WeakReference<>(null);
    private volatile boolean cancelPairing;
    private TabletMaxBackground(Context context) {
        this.context = context; store = new TabletMaxSession(context);
        ConnectivityManager manager = context.getSystemService(ConnectivityManager.class);
        if (manager != null) manager.registerDefaultNetworkCallback(new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network available) { kick(); }
        });
    }
    void attach(Activity current) { activity = new WeakReference<>(current); kick(); }
    private void notice(String message) {
        main.post(() -> { Activity current = activity.get();
            if (current != null && !current.isFinishing() && current.hasWindowFocus()) Toast.makeText(current, message, Toast.LENGTH_LONG).show(); });
    }
    private static boolean ready(JSONObject s) { return !s.optString("token").isEmpty() && !s.optBoolean("auth_required"); }
    void cancelPairing() { cancelPairing = true; }
    void startPairing(Result result) {
        cancelPairing = false;
        control.execute(() -> {
            try {
                JSONObject s = store.read();
                try (TabletMaxWire wire = new TabletMaxWire(s.getString("device_id"))) {
                    if (!s.optString("token").isEmpty()) {
                        try {
                            JSONObject login = wire.login(s.getString("token"));
                            long self = login.getJSONObject("profile").getJSONObject("contact").getLong("id");
                            if (self != s.getLong("self_id")) throw new IllegalStateException("max_session_owner");
                            TabletMaxWire.Recipient recipient = wire.verifyRecipient(login, self);
                            long chat = recipient.chat;
                            store.change(state -> state.put("chat_id", chat).put("recipient_id", recipient.user).put("auth_required", false));
                            android.util.Log.i("MiningMasterTablet", "max_background event=paired");
                            main.post(() -> result.done(new JSONObject())); kick(); return;
                        } catch (TabletMaxWire.Rejected invalid) {
                            store.change(state -> { state.remove("token"); state.put("auth_required", true); });
                        }
                    }
                    long pairingDeadline = System.currentTimeMillis() + 15 * 60_000L;
                    while (!cancelPairing && System.currentTimeMillis() < pairingDeadline) {
                    JSONObject qr = wire.requestQr();
                    java.net.URI link = java.net.URI.create(qr.getString("qrLink"));
                    if (!"https".equals(link.getScheme()) || !"max.ru".equals(link.getHost()) || link.getUserInfo() != null)
                        throw new IllegalStateException("max_qr_origin");
                    main.post(() -> result.done(qr));
                    String track = qr.getString("trackId");
                    long expires = qr.getLong("expiresAt"), interval = Math.max(750, Math.min(5_000, qr.optLong("pollingInterval", 1_000)));
                    while (!cancelPairing && System.currentTimeMillis() < expires) {
                        if (wire.qrConfirmed(track)) {
                            String token = wire.confirmQr(track);
                            JSONObject login = wire.login(token), contact = login.getJSONObject("profile").getJSONObject("contact");
                            long self = contact.getLong("id");
                            if (!"Горный мастер".equalsIgnoreCase(TabletMaxWire.name(contact)) || (s.optLong("self_id") != 0 && s.getLong("self_id") != self))
                                throw new IllegalStateException("max_service_account_identity");
                            // Preserve the verified own-account authorization even if
                            // recipient binding requires another compatibility check.
                            store.change(state -> state.put("token", login.optString("token", token)).put("self_id", self).put("auth_required", true));
                            TabletMaxWire.Recipient recipient = wire.verifyRecipient(login, self);
                            long chat = recipient.chat;
                            if (cancelPairing) return;
                            store.change(state -> state.put("token", login.optString("token", token)).put("self_id", self)
                                .put("chat_id", chat).put("recipient_id", recipient.user).put("auth_required", false));
                            android.util.Log.i("MiningMasterTablet", "max_background event=paired");
                            main.post(() -> result.done(new JSONObject())); kick(); return;
                        }
                        Thread.sleep(interval);
                    }
                    }
                    if (!cancelPairing) main.post(() -> result.failed("Подключение остановлено. Откройте его ещё раз, когда будете готовы сканировать QR."));
                }
            } catch (Exception failure) { diagnostic("pairing_failed", failure); if (!cancelPairing) main.post(() -> result.failed("Не удалось подтвердить служебный аккаунт MAX. Попробуйте новое подключение.")); }
        });
    }
    void share(Activity current, String raw) {
        String text = TabletMaxShare.preparedText(raw); activity = new WeakReference<>(current);
        control.execute(() -> {
            try {
                if (!ready(store.read())) {
                    main.post(() -> {
                        if (current.isFinishing() || !current.hasWindowFocus()) return;
                        try { TabletMaxSetup.show(current, this); }
                        catch (RuntimeException failure) { diagnostic("setup_failed", failure); notice("Не удалось открыть подключение MAX."); }
                    }); return;
                }
                final boolean[] added = {false}; final String[] id = {""};
                store.change(s -> {
                    JSONArray jobs = s.getJSONArray("jobs"); long now = System.currentTimeMillis(); String hash = hash(text);
                    for (int i = 0; i < jobs.length(); i++) {
                        JSONObject old = jobs.getJSONObject(i); String status = old.optString("state");
                        if (hash.equals(old.optString("hash")) && !"FAILED".equals(status) && (!"SENT".equals(status) || now - old.optLong("created") < 5_000)) {
                            id[0] = old.getString("id"); return;
                        }
                    }
                    // Retain recent receipts but keep the encrypted journal bounded.
                    JSONArray kept = new JSONArray();
                    for (int i = 0; i < jobs.length(); i++) {
                        JSONObject old = jobs.getJSONObject(i);
                        if (!"SENT".equals(old.optString("state")) || now - old.optLong("created") < 24 * 60 * 60_000L) kept.put(old);
                    }
                    if (kept.length() >= 50) throw new IllegalStateException("max_outbox_full");
                    long cid = Math.max(now, s.optLong("last_cid") + 1); s.put("last_cid", cid);
                    id[0] = UUID.randomUUID().toString();
                    kept.put(new JSONObject().put("id", id[0]).put("cid", cid).put("created", now).put("hash", hash)
                        .put("text", text).put("owner", s.getLong("self_id")).put("recipient", s.getLong("recipient_id")).put("state", "QUEUED"));
                    s.put("jobs", kept); added[0] = true;
                });
                android.util.Log.i("MiningMasterTablet", "max_background event=" + (added[0] ? "queued" : "existing") + " id=" + id[0]);
                notice(added[0] ? "Расстановка сохранена для отправки в MAX." : "Эта расстановка уже отправляется или отправлена.");
                schedule(); kick();
            } catch (Exception failure) { diagnostic("enqueue_failed", failure); notice("Не удалось сохранить отправку в MAX. Расстановка осталась на планшете."); }
        });
    }
    void kick() { network.execute(() -> flush(null)); }
    void runJob(Runnable finished) { network.execute(() -> flush(finished)); }
    private void schedule() {
        JobScheduler scheduler = context.getSystemService(JobScheduler.class);
        if (scheduler != null) scheduler.schedule(new JobInfo.Builder(JOB, new ComponentName(context, TabletMaxJobService.class))
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setMinimumLatency(30_000).setPersisted(true)
            .setBackoffCriteria(30_000, JobInfo.BACKOFF_POLICY_EXPONENTIAL).build());
    }
    private static boolean pending(JSONObject s) throws Exception {
        JSONArray jobs = s.getJSONArray("jobs");
        for (int i = 0; i < jobs.length(); i++) if (!"SENT".equals(jobs.getJSONObject(i).optString("state")) && !"FAILED".equals(jobs.getJSONObject(i).optString("state"))) return true;
        return false;
    }
    private void update(String id, String state, JSONObject receipt) throws Exception {
        store.change(s -> { JSONArray jobs = s.getJSONArray("jobs");
            for (int i = 0; i < jobs.length(); i++) { JSONObject job = jobs.getJSONObject(i);
                if (id.equals(job.getString("id"))) {
                    job.put("state", state);
                    if (receipt != null) job.put("message_id", receipt.optString("id")).put("server_time", receipt.optLong("time"));
                }
            }
        });
    }
    private void flush(Runnable finished) {
        try {
            JSONObject s = store.read(); if (!ready(s) || !pending(s)) return;
            try (TabletMaxWire wire = new TabletMaxWire(s.getString("device_id"))) {
                JSONObject login;
                try { login = wire.login(s.getString("token")); }
                catch (TabletMaxWire.Rejected invalid) { store.change(state -> state.put("auth_required", true)); notice("Для отправки нужно переподключить служебный MAX."); return; }
                long self = login.getJSONObject("profile").getJSONObject("contact").getLong("id");
                if (self != s.getLong("self_id")) throw new IllegalStateException("max_session_owner");
                if (!login.optString("token").isEmpty()) store.change(state -> state.put("token", login.getString("token")));
                TabletMaxWire.Recipient recipient = wire.verifyRecipient(login, self);
                long chat = recipient.chat;
                if (chat != s.getLong("chat_id") || recipient.user != s.getLong("recipient_id")) throw new IllegalStateException("max_chat_changed");
                JSONArray jobs = s.getJSONArray("jobs");
                for (int i = 0; i < jobs.length(); i++) {
                    JSONObject job = jobs.getJSONObject(i); String state = job.getString("state"), id = job.getString("id");
                    if ("SENT".equals(state) || "FAILED".equals(state)) continue;
                    if (job.getLong("owner") != self || job.getLong("recipient") != recipient.user) { update(id, "FAILED", null); continue; }
                    if (!"QUEUED".equals(state)) {
                        JSONObject receipt = findReceipt(wire.history(chat), job, chat, self);
                        if (receipt != null) { update(id, "SENT", receipt); logReceipt(id, receipt, "reconciled"); continue; }
                        // No blind resend after losing the acknowledgement. Query again on reconnect.
                        update(id, "UNCERTAIN", null); notice("Проверяем подтверждение отправки в MAX."); return;
                    }
                    update(id, "INFLIGHT", null);
                    try {
                        JSONObject receipt = wire.send(chat, job.getLong("cid"), job.getString("text"));
                        if (!receiptMatches(receipt, job, chat, self)) throw new IllegalStateException("max_receipt_mismatch");
                        update(id, "SENT", receipt); logReceipt(id, receipt, "acknowledged"); notice("Расстановка отправлена в MAX.");
                    } catch (TabletMaxWire.Rejected rejected) { update(id, "QUEUED", null); throw rejected; }
                    catch (Exception ambiguous) { update(id, "UNCERTAIN", null); throw ambiguous; }
                }
            }
        } catch (Exception failure) { diagnostic("retry_pending", failure); }
        finally {
            try { JSONObject state = store.read(); if (ready(state) && pending(state)) schedule(); }
            catch (Exception ignored) { /* Never replace an unreadable encrypted outbox. */ }
            if (finished != null) main.post(finished);
        }
    }
    static boolean receiptMatches(JSONObject receipt, JSONObject job, long chat, long self) {
        return !receipt.optString("id").isEmpty() && job.optString("text").equals(receipt.optString("text"))
            && (!receipt.has("cid") || receipt.optLong("cid") == job.optLong("cid"))
            && (!receipt.has("sender") || receipt.optLong("sender") == self)
            && (!receipt.has("chatId") || receipt.optLong("chatId") == chat);
    }
    private static JSONObject findReceipt(JSONArray history, JSONObject job, long chat, long self) throws Exception {
        if (history != null) for (int i = 0; i < history.length(); i++) {
            JSONObject item = TabletMaxWire.message(history.getJSONObject(i));
            if (item.optLong("cid", -1) == job.getLong("cid") && receiptMatches(item, job, chat, self)) return item;
        }
        return null;
    }
    private static void logReceipt(String id, JSONObject receipt, String kind) {
        android.util.Log.i("MiningMasterTablet", "max_background event=" + kind + " id=" + id + " message_id=" + receipt.optString("id")
            + " server_time=" + receipt.optLong("time"));
    }
    private static String hash(String value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder result = new StringBuilder(); for (byte b : digest) result.append(String.format(java.util.Locale.ROOT, "%02x", b & 255)); return result.toString();
    }
    private static void diagnostic(String stage, Exception failure) {
        String detail = failure instanceof TabletMaxWire.Rejected ? ((TabletMaxWire.Rejected) failure).code
            : failure.getMessage() != null && failure.getMessage().matches("max_[a-z_]+") ? failure.getMessage() : "";
        android.util.Log.w("MiningMasterTablet", "max_background event=" + stage + " type=" + failure.getClass().getSimpleName()
            + (detail.isEmpty() ? "" : " code=" + detail));
    }
}
