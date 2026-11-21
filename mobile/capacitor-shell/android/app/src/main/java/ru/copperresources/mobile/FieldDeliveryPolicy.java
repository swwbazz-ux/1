package ru.copperresources.mobile;

/** A recent proved event channel permits a slower independent safety check. */
final class FieldDeliveryPolicy {
    static final long SAFETY_CHECK_MS = 60_000L;
    static final long SSE_LEASE_MS = 35_000L;
    static final long PUSH_LEASE_MS = 24 * 60 * 60_000L;
    private static volatile long lastSseAt = -1L;
    private static volatile long lastPushAt = -1L;

    private FieldDeliveryPolicy() {}

    static void recordSse(long elapsedNow) { lastSseAt = elapsedNow; }
    static void recordPush(long elapsedNow) { lastPushAt = elapsedNow; }

    static long nextCheck(long elapsedNow, boolean foreground, boolean pendingWrite, long fallbackMs) {
        if (pendingWrite) return fallbackMs;
        boolean primaryFresh = foreground
            ? fresh(elapsedNow, lastSseAt, SSE_LEASE_MS)
            : fresh(elapsedNow, lastPushAt, PUSH_LEASE_MS);
        return primaryFresh ? Math.max(fallbackMs, SAFETY_CHECK_MS) : fallbackMs;
    }

    static boolean fresh(long now, long previous, long leaseMs) {
        return previous >= 0L && now >= previous && now - previous <= leaseMs;
    }
}
