package ru.copperresources.mobile;

import android.content.Context;
import android.content.pm.LauncherApps;
import android.content.pm.ShortcutInfo;
import android.os.Build;
import android.os.Process;
import java.util.Collections;
import java.util.List;

/** The only external recipient app accepted by the placement-sharing bridge. */
final class TabletMaxShare {
    static final String PACKAGE = "ru.oneme.app";
    static final int MAX_LENGTH = 4000;
    // Temporary test destination approved by the owner and verified on the device.
    static final String RECIPIENT_NAME = "Василий Сергеев";
    static final String RECIPIENT_URL = "https://max.ru/u/f9LHodD0cOJRw2-zNOOu2FPOVN5nqsdcCC4xmwGCnKkbDeyeylIoOzDeix0";
    // MAX's published Direct Share identity, verified against this approved chat.
    // Never resolve a recipient by display name alone or use another recent chat.
    static final String RECIPIENT_SHORTCUT_ID = "504342722";
    private static final String SHARE_CATEGORY = "ru.oneme.app.sharing.category.SHORTCUT_SHARE";

    static boolean approvedShortcut(String packageName, String id, CharSequence label,
                                    boolean enabled, boolean shareTarget) {
        return PACKAGE.equals(packageName) && RECIPIENT_SHORTCUT_ID.equals(id)
            && label != null && RECIPIENT_NAME.contentEquals(label) && enabled && shareTarget;
    }

    static String directRecipient(Context context) {
        if (Build.VERSION.SDK_INT < 29) return null;
        try {
            // The dedicated-device HOME app is an Android shortcut host. No additional
            // permissions, contact access or MAX credentials are requested.
            LauncherApps launcher = context.getSystemService(LauncherApps.class);
            if (launcher == null || !launcher.hasShortcutHostPermission()) return null;
            int flags = LauncherApps.ShortcutQuery.FLAG_MATCH_DYNAMIC
                | LauncherApps.ShortcutQuery.FLAG_MATCH_PINNED;
            if (Build.VERSION.SDK_INT >= 30) flags |= LauncherApps.ShortcutQuery.FLAG_MATCH_CACHED;
            LauncherApps.ShortcutQuery query = new LauncherApps.ShortcutQuery()
                .setPackage(PACKAGE).setShortcutIds(Collections.singletonList(RECIPIENT_SHORTCUT_ID))
                .setQueryFlags(flags);
            List<ShortcutInfo> shortcuts = launcher.getShortcuts(query, Process.myUserHandle());
            if (shortcuts == null) return null;
            for (ShortcutInfo shortcut : shortcuts) {
                if (approvedShortcut(shortcut.getPackage(), shortcut.getId(), shortcut.getShortLabel(),
                    shortcut.isEnabled(), shortcut.getCategories() != null
                        && shortcut.getCategories().contains(SHARE_CATEGORY))) return shortcut.getId();
            }
        } catch (RuntimeException unavailable) {
            android.util.Log.w("MiningMasterTablet", "max_share shortcut=unavailable type="
                + unavailable.getClass().getSimpleName());
        }
        return null;
    }

    static String preparedText(String text) {
        if (text == null || text.trim().isEmpty() || text.length() > MAX_LENGTH) {
            throw new IllegalArgumentException("placement_text");
        }
        return text;
    }

    private TabletMaxShare() {}
}
