package ru.copperresources.mobile;

/** Native transport evidence only. A live heartbeat does not acknowledge WebView DOM/outbox. */
final class NativeTransportState {
    static final int LOSS_FAILURE_COUNT = 3;
    static final long SILENCE_MS = 15_000L;
    int failureCount;
    long lastSuccessAtMs;
    long occurredAtMs;
    long serverVersion;
    private long firstFailureAtMs;
    String status = "unknown";
    String transportState = "unknown";
    String reason = "initial";

    NativeTransportState(long previousSuccessAtMs) {
        lastSuccessAtMs = Math.max(0L, previousSuccessAtMs);
    }

    static boolean isValidServerVersion(Object value) {
        if (!(value instanceof Number)) return false;
        double version = ((Number) value).doubleValue();
        return Double.isFinite(version) && version >= 0D && version <= 9_007_199_254_740_991D
            && version == Math.floor(version);
    }

    String failureStatusText(boolean foreground) {
        // While the screen is active, its WebView may be healthy even if this native
        // channel failed. The notification must identify which channel is retrying.
        if (foreground) return "Фоновое подключение: повторяем…";
        return "lost".equals(transportState)
            ? "Нет связи с сервером — повторяем подключение"
            : "Переподключение…";
    }

    void success(long now, long version) {
        failureCount = 0;
        firstFailureAtMs = 0L;
        lastSuccessAtMs = occurredAtMs = now;
        serverVersion = Math.max(0L, version);
        status = "success";
        transportState = "ok";
        reason = "heartbeat_success";
    }

    void failure(long now, String classification) {
        if (failureCount == 0) firstFailureAtMs = now;
        failureCount += 1;
        occurredAtMs = now;
        status = "failure";
        reason = classification;
        // An isolated exception is always weak, including after a suspended process.
        boolean sustainedSilence = failureCount > 1
            && now - firstFailureAtMs >= SILENCE_MS
            && now - lastSuccessAtMs >= SILENCE_MS;
        transportState = failureCount >= LOSS_FAILURE_COUNT || sustainedSilence ? "lost" : "weak";
    }

    void localFailure(long now, String classification) {
        occurredAtMs = now;
        status = "failure";
        reason = classification;
        // Cookie/UI timeouts and auth failures are not production/network failures.
        if ("authentication_ended".equals(classification)) {
            transportState = "auth_required";
        } else if (!"lost".equals(transportState)) {
            transportState = "weak";
        }
    }

    long retryDelay(long normalInterval, long maximum) {
        if (failureCount <= 1) return 2_000L;
        if (failureCount == 2) return 4_000L;
        long multiplier = 1L << Math.min(failureCount - 3, 3);
        return Math.min(maximum, normalInterval * multiplier);
    }
}
