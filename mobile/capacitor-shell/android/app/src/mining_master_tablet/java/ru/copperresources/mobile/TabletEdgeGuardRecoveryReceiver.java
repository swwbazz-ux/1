package ru.copperresources.mobile;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Recovery only: boot/package replacement never enables the edge gate. */
public final class TabletEdgeGuardRecoveryReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (!Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action) && !Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) return;
        boolean restored = TabletEdgeGuardService.recoverPending(context);
        android.util.Log.i("TabletEdgeGuard", "recovery=" + (restored ? "restored" : "pending"));
    }
}
