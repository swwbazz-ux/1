package ru.copperresources.mobile;

/** One bounded, cancellable confirmation attempt; elapsed time comes from a monotonic clock. */
final class TabletLockTaskConfirmation {
    enum SystemState { NONE, LOCKED, PINNED }
    enum Outcome { PENDING, LOCKED, PINNED, TIMED_OUT, CANCELLED }
    private final long startedAt;
    private final long timeout;
    private Outcome outcome = Outcome.PENDING;

    TabletLockTaskConfirmation(long startedAt, long timeout) {
        if (timeout <= 0) throw new IllegalArgumentException("Timeout must be positive");
        this.startedAt = startedAt;
        this.timeout = timeout;
    }

    Outcome observe(SystemState state, long now) {
        if (outcome != Outcome.PENDING) return outcome;
        if (state == SystemState.PINNED) outcome = Outcome.PINNED;
        else if (now - startedAt >= timeout) outcome = Outcome.TIMED_OUT;
        else if (state == SystemState.LOCKED) outcome = Outcome.LOCKED;
        return outcome;
    }

    void cancel() { outcome = Outcome.CANCELLED; }
}
