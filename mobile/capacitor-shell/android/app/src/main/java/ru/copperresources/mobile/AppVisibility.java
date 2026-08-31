package ru.copperresources.mobile;

import java.util.concurrent.atomic.AtomicBoolean;

public final class AppVisibility {
    private static final AtomicBoolean FOREGROUND = new AtomicBoolean(false);

    private AppVisibility() {}

    public static void setForeground(boolean foreground) {
        FOREGROUND.set(foreground);
    }

    public static boolean isForeground() {
        return FOREGROUND.get();
    }
}
