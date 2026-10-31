package ru.copperresources.mobile;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class TabletEdgeGuardStateTest {
    private static final class Fixture implements TabletEdgeGuardState.Store, TabletEdgeGuardState.Environment {
        int setup = 1;
        boolean pending;
        boolean allowed = true;
        boolean commitAllowed = true;
        boolean restoreAllowed = true;
        boolean ignoreZero;
        boolean ignoreOne;
        boolean failAfterZero;
        long time;
        final List<String> writes = new ArrayList<>();
        TabletEdgeGuardState state = new TabletEdgeGuardState(this, this);
        @Override public boolean pending() { return pending; }
        @Override public boolean savePending(boolean value) {
            writes.add("journal:" + value);
            if (!commitAllowed) return false;
            pending = value;
            return true;
        }
        @Override public int setupComplete() { return setup; }
        @Override public boolean writeSetupComplete(int value) {
            writes.add("setup:" + value);
            if (value == 1 && !restoreAllowed) return false;
            if (value == 0 && ignoreZero) return true;
            if (value == 1 && ignoreOne) return true;
            setup = value;
            if (value == 0 && failAfterZero) throw new IllegalStateException();
            return true;
        }
        @Override public boolean mayArm() { return allowed; }
        @Override public long now() { return time; }
    }

    @Test public void journalMustReachDiskBeforeSystemChange() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        assertEquals(Arrays.asList("journal:true", "setup:0"), f.writes);
        assertTrue(f.pending);
        assertEquals(0, f.setup);
        assertTrue(f.state.active());
    }

    @Test public void failedJournalCommitNeverTouchesAndroidFlag() {
        Fixture f = new Fixture();
        f.commitAllowed = false;
        assertFalse(f.state.arm());
        assertEquals(Arrays.asList("journal:true"), f.writes);
        assertEquals(1, f.setup);
    }

    @Test public void restartRecoversWithoutLockOrFirmwarePrerequisites() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.allowed = false; // e.g. boot before HOME, OTA, or removal of device owner.
        f.state = new TabletEdgeGuardState(f, f);
        assertTrue(f.state.recover());
        assertEquals(1, f.setup);
        assertFalse(f.pending);
        assertFalse(f.state.active());
    }

    @Test public void timeoutRestoresAndLateHeartbeatCannotRearm() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.time = TabletEdgeGuardState.LEASE_MS;
        f.state.checkLease();
        assertEquals(1, f.setup);
        assertFalse(f.state.heartbeat());
        assertFalse(f.state.active());
        assertEquals(1, f.setup);
    }

    @Test public void heartbeatRenewsOnlyAnUnexpiredLease() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.time = TabletEdgeGuardState.LEASE_MS - 1;
        assertTrue(f.state.heartbeat());
        f.time += TabletEdgeGuardState.LEASE_MS;
        assertFalse(f.state.heartbeat());
        assertEquals(1, f.setup);
    }

    @Test public void losingLockOrPermissionRestoresBeforeLeaseDeadline() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.allowed = false;
        f.state.checkLease();
        assertEquals(1, f.setup);
        assertFalse(f.state.active());
    }

    @Test public void failedRestoreKeepsJournalForNextRecoveryAttempt() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.restoreAllowed = false;
        assertFalse(f.state.release());
        assertEquals(0, f.setup);
        assertTrue(f.pending);
        f.restoreAllowed = true;
        f.state.checkLease();
        assertEquals(1, f.setup);
        assertFalse(f.pending);
    }

    @Test public void successfulWriteReturnStillRequiresReadback() {
        Fixture f = new Fixture();
        f.ignoreZero = true;
        assertFalse(f.state.arm());
        assertEquals(1, f.setup);
        assertFalse(f.pending);
    }

    @Test public void exceptionAfterActualWriteStillRestores() {
        Fixture f = new Fixture();
        f.failAfterZero = true;
        assertFalse(f.state.arm());
        assertEquals(1, f.setup);
        assertFalse(f.pending);
    }

    @Test public void doesNotClaimAnUnfinishedSetupWithoutOurJournal() {
        Fixture f = new Fixture();
        f.setup = 0;
        assertFalse(f.state.arm());
        assertFalse(f.state.release());
        assertEquals(0, f.setup);
        assertTrue(f.writes.isEmpty());
    }

    @Test public void externalRecoveryInvalidatesLeaseInsteadOfRewritingZero() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.setup = 1;
        assertFalse(f.state.heartbeat());
        assertFalse(f.state.active());
        assertFalse(f.pending);
        assertEquals(1, f.setup);
    }

    @Test public void restoreSuccessReturnWithoutReadbackCannotClearJournal() {
        Fixture f = new Fixture();
        assertTrue(f.state.arm());
        f.ignoreOne = true;
        assertFalse(f.state.release());
        assertTrue(f.pending);
        assertEquals(0, f.setup);
    }

    @Test public void unsupportedEnvironmentNeverChangesSettings() {
        Fixture f = new Fixture();
        f.allowed = false;
        assertFalse(f.state.arm());
        assertTrue(f.writes.isEmpty());
        assertEquals(1, f.setup);
    }
}
