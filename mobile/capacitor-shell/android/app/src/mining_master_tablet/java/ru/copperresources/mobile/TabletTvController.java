package ru.copperresources.mobile;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.widget.Toast;
import androidx.appcompat.app.AlertDialog;
import java.lang.ref.WeakReference;
import java.util.Collections;
import org.json.JSONObject;

/** One enrollment, then bounded reconnect to that exact receiver. No screen capture/server required. */
final class TabletTvController {
    private static TabletTvController instance;
    static synchronized TabletTvController get(Context context) {
        if (instance == null) instance = new TabletTvController(context.getApplicationContext());
        return instance;
    }
    private final Context context;
    private final SharedPreferences saved;
    private final Handler main = new Handler(Looper.getMainLooper());
    private WeakReference<MiningMasterTabletActivity> activity = new WeakReference<>(null);
    private TabletTvStatus status;
    private TabletTvGuard reconnect;
    private boolean pending, pairing;
    private int priorEnabled;
    private String phase = "idle";
    private final Runnable timeout = () -> cancel(pairing
        ? "Телевизор не подключён. На работе включите ТВ и выберите его в списке."
        : "Телевизор не найден. Включите его и повторите подключение.");

    private TabletTvController(Context context) {
        this.context = context; saved = context.getSharedPreferences("mining_master_tv", Context.MODE_PRIVATE);
        BroadcastReceiver receiver = new BroadcastReceiver() {
            @Override public void onReceive(Context ignored, Intent intent) {
                android.os.Parcelable value = intent.getParcelableExtra("android.hardware.display.extra.WIFI_DISPLAY_STATUS");
                TabletTvStatus next = TabletTvStatus.read(value);
                if (next == null) {
                    android.util.Log.w("MiningMasterTablet", "tv_route event=status_unreadable parcel=" + (value == null ? "missing" : value.getClass().getName()));
                    return;
                }
                status = next;
                android.util.Log.i("MiningMasterTablet", "tv_route event=status feature=" + next.feature + " active=" + next.activeState + " peers=" + next.peers.size());
                if (next.activeState == 2 && next.active != null) {
                    if (pending && pairing && !saved.edit().putString("address", next.active.address).putString("name", next.active.name).commit()) {
                        cancel("Не удалось сохранить телевизор. Повторите подключение."); return;
                    }
                    if (next.active.address.equals(saved.getString("address", ""))) {
                        phase = "connected";
                        if (pending) { finishRouting(); notice("Экран приложения показывается на ТВ."); }
                    }
                } else if (!pending) phase = "idle";
                publish(); TabletTvService.refresh();
            }
        };
        IntentFilter filter = new IntentFilter("android.hardware.display.action.WIFI_DISPLAY_STATUS_CHANGED");
        if (android.os.Build.VERSION.SDK_INT >= 33) context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        else context.registerReceiver(receiver, filter);
    }
    void attach(MiningMasterTabletActivity current) { activity = new WeakReference<>(current); publish(); }
    void pageReady() { publish(); }
    boolean pending() { return pending; }
    boolean reconnecting() { return pending && !pairing && reconnect != null; }
    String reconnectLabel() { return reconnect == null ? null : reconnect.label(status == null ? Collections.emptyList() : status.peers, SystemClock.elapsedRealtime()); }
    boolean consumeReconnect(String label, int count) { return reconnecting() && reconnect.consume(label, count, status == null ? Collections.emptyList() : status.peers, SystemClock.elapsedRealtime()); }
    private boolean connected() { return status != null && status.activeState == 2 && status.active != null && status.active.address.equals(saved.getString("address", "")); }
    void action(String op) {
        if ("settings".equals(op)) { settings(); return; }
        if (pending) { cancel("Подключение к ТВ отменено."); return; }
        if (connected()) { stop(); return; }
        begin(saved.getString("address", "").isEmpty());
    }
    private void begin(boolean first) {
        MiningMasterTabletActivity current = activity.get(); if (current == null || current.isFinishing()) return;
        if (android.os.Build.VERSION.SDK_INT != 30) { notice("Подключение ТВ подготовлено для Android 11 этого планшета."); return; }
        if (current.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            notice("Для поиска ТВ нужно разрешение Android на ближайшие устройства. Включите его при подготовке планшета."); return;
        }
        if (!TabletTvService.available()) { notice("Помощник ТВ выключен. Включите его через обслуживание планшета."); return; }
        pairing = first; priorEnabled = Settings.Global.getInt(context.getContentResolver(), "wifi_display_on", 0);
        pending = true; phase = "connecting";
        long duration = first ? 120_000 : 30_000;
        reconnect = first ? null : new TabletTvGuard(saved.getString("address", ""), SystemClock.elapsedRealtime() + duration);
        try {
            if (!current.beginTvRoute(duration)) throw new IllegalStateException("tv_route_lease");
            if (!Settings.Global.putInt(context.getContentResolver(), "wifi_display_on", 1)) throw new IllegalStateException("tv_route_enable");
            main.postDelayed(timeout, duration); publish();
            Intent intent = new Intent(Settings.ACTION_CAST_SETTINGS).setClassName("com.android.settings", "com.android.settings.Settings$WifiDisplaySettingsActivity");
            current.startActivity(intent);
            if (first) notice("Выберите телевизор. После подключения приложение запомнит его для кнопки «На ТВ».");
            android.util.Log.i("MiningMasterTablet", "tv_route event=begin mode=" + (first ? "pair" : "reconnect"));
        } catch (RuntimeException failure) { cancel("Не удалось открыть подключение ТВ. Повторите попытку."); }
    }
    void returnedToApp() {
        if (pending) cancel("Подключение к ТВ отменено.");
        else publish();
    }
    void cancel(String message) {
        if (!pending) return;
        phase = connected() ? "connected" : "idle";
        if (!connected()) try { Settings.Global.putInt(context.getContentResolver(), "wifi_display_on", priorEnabled); } catch (RuntimeException ignored) {}
        finishRouting(); publish(); notice(message);
        android.util.Log.i("MiningMasterTablet", "tv_route event=cancel");
    }
    private void finishRouting() {
        pending = false; reconnect = null; main.removeCallbacks(timeout);
        MiningMasterTabletActivity current = activity.get();
        if (current != null) {
            current.finishTvRoute();
            context.startActivity(new Intent(context, MiningMasterTabletActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        }
    }
    private void stop() {
        try {
            if (!Settings.Global.putInt(context.getContentResolver(), "wifi_display_on", 0)) throw new IllegalStateException();
            phase = "stopping"; publish();
        } catch (RuntimeException failure) { notice("Не удалось остановить показ. Повторите попытку."); }
    }
    private void settings() {
        MiningMasterTabletActivity current = activity.get(); if (current == null) return;
        new AlertDialog.Builder(current).setTitle("Телевизор раскомандировки")
            .setMessage(saved.getString("name", "Телевизор ещё не выбран") + "\n\nПосле первого подключения кнопка «На ТВ» будет подключать этот телевизор.")
            .setPositiveButton("Выбрать другой", (dialog, which) -> { if (pending) cancel("Подключение отменено."); begin(true); })
            .setNegativeButton("Закрыть", null).show();
    }
    private void notice(String text) { main.post(() -> Toast.makeText(context, text, Toast.LENGTH_LONG).show()); }
    private void publish() {
        MiningMasterTabletActivity current = activity.get(); if (current == null) return;
        try { current.publishTv(new JSONObject().put("phase", phase).put("name", saved.getString("name", "")).put("helper", TabletTvService.available())); }
        catch (org.json.JSONException ignored) {}
    }
}
