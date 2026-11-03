package ru.copperresources.mobile;

import java.util.List;

/** A short-lived, single-use request. Never persisted or retried after a click/process death. */
final class TabletMaxSendGuard {
    static final long LIFETIME_MS = 15_000;
    static final long STABLE_MS = 400;
    final String text;
    private final long created;
    private long readySince = -1;
    private boolean consumed;

    TabletMaxSendGuard(String text, long now) {
        this.text = text;
        created = now;
    }

    boolean expired(long now) { return now < created || now - created >= LIFETIME_MS; }
    void resetReadiness() { readySince = -1; }

    boolean consume(long now, String packageName, String title, List<String> recipients,
                    boolean exactPreview, String comment, boolean sendAvailable) {
        if (consumed || expired(now)) return false;
        boolean matches = TabletMaxShare.PACKAGE.equals(packageName)
            && "Поделиться сообщением".equals(title)
            && recipients.size() == 1 && TabletMaxShare.RECIPIENT_NAME.equals(recipients.get(0))
            && exactPreview && (comment.isEmpty() || "Сообщение".equals(comment)) && sendAvailable;
        if (!matches) { readySince = -1; return false; }
        if (readySince < 0) { readySince = now; return false; }
        if (now - readySince < STABLE_MS) return false;
        consumed = true; // Consume BEFORE ACTION_CLICK, even if Android cannot confirm the click.
        return true;
    }
}
