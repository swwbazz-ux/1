package ru.copperresources.mobile;

/** The only external recipient app accepted by the placement-sharing bridge. */
final class TabletMaxShare {
    static final String PACKAGE = "ru.oneme.app";
    static final int MAX_LENGTH = 4000;
    // Temporary test destination approved by the owner and verified on the device.
    static final String RECIPIENT_NAME = "Василий Сергеев";
    static final String RECIPIENT_URL = "https://max.ru/u/f9LHodD0cOJRw2-zNOOu2FPOVN5nqsdcCC4xmwGCnKkbDeyeylIoOzDeix0";

    static String preparedText(String text) {
        if (text == null || text.trim().isEmpty() || text.length() > MAX_LENGTH) {
            throw new IllegalArgumentException("placement_text");
        }
        return text;
    }

    private TabletMaxShare() {}
}
