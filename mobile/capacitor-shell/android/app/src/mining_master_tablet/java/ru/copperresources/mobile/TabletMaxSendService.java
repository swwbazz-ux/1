package ru.copperresources.mobile;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Intent;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Toast;
import java.util.ArrayList;
import java.util.List;

/** Confined to MAX by Android metadata, idle unless our trusted native bridge arms one request. */
public final class TabletMaxSendService extends AccessibilityService {
    private static TabletMaxSendService connected;
    private static final String ID = "ru.oneme.app:id/";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TabletMaxSendGuard pending;
    private long lastArmed = -10_000;
    private String lastObservation = "";
    private String lastGeometry = "";
    private final Runnable poll = this::inspect;

    static boolean available() { return connected != null; }

    static boolean arm(String text) {
        TabletMaxSendService service = connected;
        long now = SystemClock.elapsedRealtime();
        if (service == null || service.pending != null || now - service.lastArmed < 3_000) return false;
        service.lastArmed = now;
        service.lastObservation = "";
        service.lastGeometry = "";
        service.pending = new TabletMaxSendGuard(TabletMaxShare.preparedText(text), now);
        service.handler.postDelayed(service.poll, 250);
        android.util.Log.i("MiningMasterTablet", "max_auto state=armed");
        return true;
    }

    static void cancelPending() {
        if (connected != null) connected.cancel(false);
    }

    @Override protected void onServiceConnected() {
        connected = this;
        android.util.Log.i("MiningMasterTablet", "max_auto service=connected");
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if (pending == null || event == null || !TabletMaxShare.PACKAGE.contentEquals(event.getPackageName() == null ? "" : event.getPackageName())) return;
        // Any manual interaction while the handoff is pending cancels automation.
        if (event.getEventType() == AccessibilityEvent.TYPE_VIEW_CLICKED) { cancel(true); return; }
        if (event.getEventType() == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED) {
            AccessibilityNodeInfo source = event.getSource();
            if (source != null) {
                try {
                    if ((ID + "oneme_message_input_edit_text").equals(source.getViewIdResourceName())
                        && !string(source.getText()).isEmpty() && !"Сообщение".contentEquals(source.getText())) cancel(true);
                } finally { source.recycle(); }
            }
        }
        // The fixed poll cannot be postponed indefinitely by MAX animations/events.
    }

    private void cancel(boolean notify) {
        boolean active = pending != null;
        pending = null;
        handler.removeCallbacks(poll);
        if (active) {
            android.util.Log.i("MiningMasterTablet", "max_auto state=cancelled");
            if (notify) Toast.makeText(this, "Автоотправка отменена. При необходимости отправьте расстановку в MAX вручную.", Toast.LENGTH_LONG).show();
        }
    }

    private void inspect() {
        TabletMaxSendGuard request = pending;
        if (request == null) return;
        if (request.expired(SystemClock.elapsedRealtime())) { cancel(true); return; }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            try {
                if (TabletMaxShare.PACKAGE.contentEquals(root.getPackageName() == null ? "" : root.getPackageName())) inspectMax(root, request);
                else request.resetReadiness();
            } catch (RuntimeException failure) {
                android.util.Log.w("MiningMasterTablet", "max_auto state=inspection_failed type=" + failure.getClass().getSimpleName());
                cancel(true);
            } finally { root.recycle(); }
        } else request.resetReadiness();
        if (pending != null) handler.postDelayed(poll, 200);
    }

    private void inspectMax(AccessibilityNodeInfo root, TabletMaxSendGuard request) {
        List<AccessibilityNodeInfo> titles = root.findAccessibilityNodeInfosByViewId(ID + "oneme_toolbar_title");
        List<AccessibilityNodeInfo> inputs = root.findAccessibilityNodeInfosByViewId(ID + "oneme_message_input_edit_text");
        List<AccessibilityNodeInfo> sends = root.findAccessibilityNodeInfosByViewId(ID + "oneme_message_input_right_outer_icon");
        try {
            if (titles.size() != 1 || inputs.size() != 1 || sends.size() != 1) {
                observe("nodes=" + titles.size() + "," + inputs.size() + "," + sends.size());
                request.resetReadiness(); return;
            }
            String title = string(titles.get(0).getText()), comment = string(inputs.get(0).getText());
            Snapshot snapshot = new Snapshot(request.text);
            scan(root, snapshot, 0);
            AccessibilityNodeInfo send = sends.get(0);
            Rect bounds = new Rect(), window = new Rect();
            send.getBoundsInScreen(bounds);
            root.getBoundsInScreen(window);
            boolean button = send.isEnabled() && send.isVisibleToUser() && !bounds.isEmpty()
                && window.contains(bounds)
                && "Отправить".contentEquals(send.getContentDescription() == null ? "" : send.getContentDescription());
            observe("screen=" + "Поделиться сообщением".equals(title) + " recipients=" + snapshot.recipients.size()
                + " recipient_match=" + (snapshot.recipients.size() == 1 && TabletMaxShare.RECIPIENT_NAME.equals(snapshot.recipients.get(0)))
                + " preview=" + snapshot.exactPreview + " complete=" + snapshot.complete
                + " empty_comment=" + (comment.isEmpty() || "Сообщение".equals(comment)) + " send=" + button);
            // Opening MAX can rotate the display and animate its keyboard. A matching
            // text snapshot alone is insufficient while the Send control is moving.
            String geometry = window.toShortString() + bounds.toShortString();
            if (!geometry.equals(lastGeometry)) { request.resetReadiness(); lastGeometry = geometry; }
            if (SystemClock.elapsedRealtime() - lastArmed < 1_500) return;
            if (!request.consume(SystemClock.elapsedRealtime(), string(root.getPackageName()), title,
                snapshot.recipients, snapshot.exactPreview && snapshot.complete, comment, button)) return;
            pending = null;
            handler.removeCallbacks(poll);
            android.util.Log.i("MiningMasterTablet", "max_auto target=" + bounds.toShortString());
            // Never retry a click: a false/late acknowledgement must not cause a duplicate message.
            boolean accepted;
            if (send.isClickable()) accepted = send.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            else {
                // MAX 26.38.2 handles touch directly: its labelled Send node has no
                // ACTION_CLICK. One bounded tap is dispatched ONLY at the inspected node.
                Path path = new Path();
                path.moveTo(bounds.exactCenterX(), bounds.exactCenterY());
                GestureDescription gesture = new GestureDescription.Builder()
                    .addStroke(new GestureDescription.StrokeDescription(path, 0, 60)).build();
                accepted = dispatchGesture(gesture, new GestureResultCallback() {
                    @Override public void onCompleted(GestureDescription description) {
                        android.util.Log.i("MiningMasterTablet", "max_auto state=gesture_completed");
                    }
                    @Override public void onCancelled(GestureDescription description) {
                        android.util.Log.w("MiningMasterTablet", "max_auto state=gesture_cancelled");
                    }
                }, handler);
            }
            android.util.Log.i("MiningMasterTablet", "max_auto state=click_attempted accepted=" + accepted);
            if (!accepted) Toast.makeText(this, "MAX не подтвердил нажатие. Проверьте диалог перед повторной отправкой.", Toast.LENGTH_LONG).show();
        } finally {
            for (AccessibilityNodeInfo node : titles) node.recycle();
            for (AccessibilityNodeInfo node : inputs) node.recycle();
            for (AccessibilityNodeInfo node : sends) node.recycle();
        }
    }

    private static final class Snapshot {
        final String expected;
        final List<String> recipients = new ArrayList<>();
        boolean exactPreview;
        boolean complete = true;
        int visited;
        Snapshot(String text) { expected = text; }
    }

    private static void scan(AccessibilityNodeInfo node, Snapshot snapshot, int depth) {
        if (++snapshot.visited > 600 || depth > 40) { snapshot.complete = false; return; }
        if (node.isVisibleToUser()) {
            String text = string(node.getText());
            if ("android.widget.RadioButton".contentEquals(node.getClassName() == null ? "" : node.getClassName())) snapshot.recipients.add(text);
            if (snapshot.expected.equals(text)) snapshot.exactPreview = true;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) { try { scan(child, snapshot, depth + 1); } finally { child.recycle(); } }
        }
    }

    private static String string(CharSequence value) { return value == null ? "" : value.toString(); }

    private void observe(String value) {
        if (value.equals(lastObservation)) return;
        lastObservation = value;
        android.util.Log.i("MiningMasterTablet", "max_auto check " + value);
    }

    @Override public void onInterrupt() { cancel(false); }
    @Override public boolean onUnbind(Intent intent) {
        cancel(false);
        if (connected == this) connected = null;
        return super.onUnbind(intent);
    }
    @Override public void onDestroy() {
        cancel(false);
        if (connected == this) connected = null;
        super.onDestroy();
    }
}
