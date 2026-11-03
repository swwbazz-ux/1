package ru.copperresources.mobile;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.ArrayList;
import java.util.List;

/** Acts only on the cast settings page, during an explicit user-initiated reconnect. */
public final class TabletTvService extends AccessibilityService {
    private static TabletTvService connected;
    private boolean castPage;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable poll = () -> inspect();
    static boolean available() { return connected != null; }
    static void refresh() { if (connected != null) connected.main.post(connected.poll); }
    @Override protected void onServiceConnected() { connected = this; }
    @Override public void onInterrupt() { castPage = false; }
    @Override public void onDestroy() { main.removeCallbacksAndMessages(null); if (connected == this) connected = null; super.onDestroy(); }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getPackageName() == null || !"com.android.settings".contentEquals(event.getPackageName())) return;
        String name = event.getClassName() == null ? "" : event.getClassName().toString();
        if (event.getEventType() == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && name.startsWith("com.android.settings.") && name.endsWith("Activity"))
            castPage = name.endsWith("$WifiDisplaySettingsActivity");
        main.removeCallbacks(poll); main.postDelayed(poll, 150);
    }
    private void inspect() {
        TabletTvController controller = TabletTvController.get(this);
        if (!controller.reconnecting()) { castPage = false; return; }
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (!castPage || root == null || !"com.android.settings".contentEquals(root.getPackageName())) return;
            String label = controller.reconnectLabel(); if (label == null) return;
            List<AccessibilityNodeInfo> titles = new ArrayList<>();
            for (AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText(label)) {
                String id = node.getViewIdResourceName();
                if (label.contentEquals(node.getText() == null ? "" : node.getText()) && node.isVisibleToUser()
                        && id != null && id.endsWith("/title")) titles.add(node);
            }
            if (titles.size() != 1) return;
            AccessibilityNodeInfo row = titles.get(0);
            for (int i = 0; i < 5 && row != null && !row.isClickable(); i++) row = row.getParent();
            if (row == null || !row.isEnabled() || !row.isClickable()) return;
            Rect bounds = new Rect(); row.getBoundsInScreen(bounds);
            if (bounds.width() < 150 || bounds.height() < 25 || bounds.height() > 260) return;
            if (!controller.consumeReconnect(label, titles.size())) return;
            boolean clicked = row.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            android.util.Log.i("MiningMasterTablet", "tv_route event=select_saved_display clicked=" + clicked);
            if (!clicked) controller.cancel("Не удалось подключить телевизор. Повторите попытку.");
        } catch (RuntimeException failure) {
            android.util.Log.w("MiningMasterTablet", "tv_route event=helper_failed type=" + failure.getClass().getSimpleName());
            controller.cancel("Подключение к ТВ остановлено. Повторите попытку.");
        } finally {
            if (controller.reconnecting()) { main.removeCallbacks(poll); main.postDelayed(poll, 500); }
        }
    }
}
