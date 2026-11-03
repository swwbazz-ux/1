package ru.copperresources.mobile;

import java.net.URLEncoder;
import java.io.UnsupportedEncodingException;

/** The only external recipient app accepted by the placement-sharing bridge. */
final class TabletMaxShare {
    static final String PACKAGE = "ru.oneme.app";
    static final int MAX_LENGTH = 4000;

    static String url(String text) {
        if (text == null || text.trim().isEmpty() || text.length() > MAX_LENGTH) {
            throw new IllegalArgumentException("placement_text");
        }
        try {
            return "https://max.ru/:share?text=" + URLEncoder.encode(text, "UTF-8").replace("+", "%20");
        } catch (UnsupportedEncodingException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private TabletMaxShare() {}
}
