package ru.copperresources.mobile;

/** Keeps connection speech behind sustained transport evidence; visual state stays immediate. */
final class ConnectionVoiceStability {
    static final long LOSS_STABLE_MS = 30_000L;
    static final long RECOVERY_STABLE_MS = 20_000L;
    static final int RECOVERY_SUCCESS_COUNT = 3;

    enum Action { NONE, LOSS_READY, RECOVERY_READY }

    private long lostSinceAtMs;
    private long recoverySinceAtMs;
    private int recoverySuccessCount;
    private boolean lossHandled;

    Action onFailure(long now, String transportState) {
        recoverySinceAtMs = 0L;
        recoverySuccessCount = 0;
        if (!"lost".equals(transportState)) {
            lostSinceAtMs = 0L;
            lossHandled = false;
            return Action.NONE;
        }
        if (lostSinceAtMs <= 0L) {
            lostSinceAtMs = now;
        }
        return !lossHandled && now - lostSinceAtMs >= LOSS_STABLE_MS
            ? Action.LOSS_READY
            : Action.NONE;
    }

    Action onSuccess(long now, boolean lossWasAnnounced) {
        lostSinceAtMs = 0L;
        lossHandled = false;
        if (!lossWasAnnounced) {
            recoverySinceAtMs = 0L;
            recoverySuccessCount = 0;
            return Action.NONE;
        }
        if (recoverySinceAtMs <= 0L) {
            recoverySinceAtMs = now;
            recoverySuccessCount = 1;
            return Action.NONE;
        }
        recoverySuccessCount += 1;
        return recoverySuccessCount >= RECOVERY_SUCCESS_COUNT
                && now - recoverySinceAtMs >= RECOVERY_STABLE_MS
            ? Action.RECOVERY_READY
            : Action.NONE;
    }

    void markLossHandled() {
        lossHandled = true;
    }

    void markRecoveryHandled() {
        recoverySinceAtMs = 0L;
        recoverySuccessCount = 0;
    }
}
