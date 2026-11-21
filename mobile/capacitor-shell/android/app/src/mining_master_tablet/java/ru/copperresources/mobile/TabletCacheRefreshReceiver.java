package ru.copperresources.mobile;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Reopen the dedicated HOME activity after an in-place package update. */
public final class TabletCacheRefreshReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent broadcast) {
        if (!Intent.ACTION_MY_PACKAGE_REPLACED.equals(broadcast.getAction())) return;
        android.util.Log.i("MiningMasterTablet", "tablet_update event=package_replaced version=" + BuildConfig.VERSION_CODE);
        context.startActivity(new Intent(context, MiningMasterTabletActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }
}
