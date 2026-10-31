package ru.copperresources.mobile;

/** Recovery-first state machine; callers serialize all access in the guard process. */
final class TabletEdgeGuardState {
    static final long LEASE_MS = 6_000L;

    interface Store {
        boolean pending();
        boolean savePending(boolean pending);
        int setupComplete();
        boolean writeSetupComplete(int value);
    }
    interface Environment {
        boolean mayArm();
        long now();
    }

    private final Store store;
    private final Environment environment;
    private boolean armed;
    private long deadline;

    TabletEdgeGuardState(Store store, Environment environment) {
        this.store = store;
        this.environment = environment;
    }

    boolean active() { return armed; }

    boolean recover() {
        try { return !store.pending() || release(); }
        catch (RuntimeException failure) { return false; }
    }

    boolean arm() {
        if (armed) return heartbeat();
        try {
            if (!recover() || !environment.mayArm() || store.setupComplete() != 1) return false;
            // This commit must reach disk before changing the persistent Android flag.
            if (!store.savePending(true)) return false;
            if (!store.writeSetupComplete(0) || store.setupComplete() != 0 || !environment.mayArm()) {
                release();
                return false;
            }
            deadline = environment.now() + LEASE_MS;
            armed = true;
            return true;
        } catch (RuntimeException failure) {
            release();
            return false;
        }
    }

    boolean heartbeat() {
        try {
            if (!armed || environment.now() >= deadline || !environment.mayArm()
                    || store.setupComplete() != 0) {
                release();
                return false;
            }
            deadline = environment.now() + LEASE_MS;
            return true;
        } catch (RuntimeException failure) {
            release();
            return false;
        }
    }

    void checkLease() {
        try {
            if ((armed && (environment.now() >= deadline || !environment.mayArm()
                    || store.setupComplete() != 0)) || (!armed && store.pending())) release();
        } catch (RuntimeException failure) { release(); }
    }

    boolean release() {
        armed = false;
        deadline = 0;
        try {
            // Never complete somebody else's setup unless our durable journal owns the change.
            if (store.setupComplete() != 1) {
                if (!store.pending() || !store.writeSetupComplete(1) || store.setupComplete() != 1) return false;
            }
            // If clearing fails, the harmless pending marker will retry recovery at next startup.
            if (store.pending()) store.savePending(false);
            return true;
        } catch (RuntimeException failure) { return false; }
    }
}
