package ru.copperresources.mobile;

/** One queued/running owner. Wake bursts never cancel a request already running. */
final class HeartbeatSchedule {
    private static final long MIN_START_INTERVAL_MS = 1_000L;
    private boolean running;
    private boolean stopped;
    private long pendingAt = -1L;
    private long lastStartedAt = -1L;
    private long nextDelay = -1L;
    private long generation;
    private boolean wakeRequested;
    private boolean responseSucceeded;

    long request(long delay, long now) {
        if (stopped) return -1L;
        if (running) {
            // A successful in-flight result serves every wake reason. If the old route
            // fails, retain just one prompt probe of the newly available route.
            if (delay > 0L) nextDelay = delay;
            else wakeRequested = true;
            return -1L;
        }
        long due = now + Math.max(0L, delay);
        if (lastStartedAt >= 0L) due = Math.max(due, lastStartedAt + MIN_START_INTERVAL_MS);
        if (pendingAt >= 0L && pendingAt <= due) return -1L;
        pendingAt = due;
        generation += 1;
        return Math.max(0L, due - now);
    }

    long generation() { return generation; }

    boolean start(long now, long ownerGeneration) {
        if (stopped || running || ownerGeneration != generation || pendingAt < 0L) return false;
        running = true;
        pendingAt = -1L;
        lastStartedAt = now;
        nextDelay = -1L;
        wakeRequested = false;
        responseSucceeded = false;
        return true;
    }

    void responseSucceeded() { responseSucceeded = true; }

    long finish() {
        running = false;
        long delay = stopped ? -1L : nextDelay;
        if (delay >= 0L && wakeRequested && !responseSucceeded) delay = Math.min(delay, MIN_START_INTERVAL_MS);
        wakeRequested = false;
        nextDelay = -1L;
        return delay;
    }

    void stop() {
        stopped = true;
        pendingAt = -1L;
        nextDelay = -1L;
    }
}
