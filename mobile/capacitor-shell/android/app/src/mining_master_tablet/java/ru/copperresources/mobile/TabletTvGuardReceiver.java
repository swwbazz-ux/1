package ru.copperresources.mobile;

import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;

/** A separate-process deadline restores the kiosk even if the UI/helper process dies. */
public final class TabletTvGuardReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (!"ru.copperresources.mobile.TV_ROUTE_TIMEOUT".equals(intent.getAction())) return;
        try {
            DevicePolicyManager manager = context.getSystemService(DevicePolicyManager.class);
            if (manager != null && manager.isDeviceOwnerApp(context.getPackageName())) {
                String[] prior = intent.getStringArrayExtra("priorAllowlist");
                manager.setLockTaskPackages(new ComponentName(context, MiningMasterDeviceAdminReceiver.class), prior == null ? new String[]{context.getPackageName()} : prior);
            }
        } catch (RuntimeException failure) { android.util.Log.e("MiningMasterTablet", "tv_route event=guard_restore_failed type=" + failure.getClass().getSimpleName()); }
        try {
            context.startActivity(new Intent(context, MiningMasterTabletActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        } catch (RuntimeException failure) { android.util.Log.e("MiningMasterTablet", "tv_route event=guard_return_failed type=" + failure.getClass().getSimpleName()); }
    }
}
