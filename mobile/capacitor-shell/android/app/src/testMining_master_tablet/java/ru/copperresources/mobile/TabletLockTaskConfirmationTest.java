package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;
import static ru.copperresources.mobile.TabletLockTaskConfirmation.SystemState.*;
import static ru.copperresources.mobile.TabletLockTaskConfirmation.Outcome;

public class TabletLockTaskConfirmationTest {
    @Test public void waitsThroughBinderReturnUntilAndroidReportsLocked() {
        TabletLockTaskConfirmation attempt = new TabletLockTaskConfirmation(500, 3000);
        assertEquals(Outcome.PENDING, attempt.observe(NONE, 500));
        assertEquals(Outcome.PENDING, attempt.observe(NONE, 600));
        assertEquals(Outcome.LOCKED, attempt.observe(LOCKED, 700));
    }

    @Test public void timesOutAtMonotonicDeadlineAndCannotLaterSucceed() {
        TabletLockTaskConfirmation attempt = new TabletLockTaskConfirmation(500, 3000);
        assertEquals(Outcome.PENDING, attempt.observe(NONE, 3499));
        assertEquals(Outcome.TIMED_OUT, attempt.observe(NONE, 3500));
        assertEquals(Outcome.TIMED_OUT, attempt.observe(LOCKED, 3600));
    }

    @Test public void lateFirstConfirmationDoesNotExtendTheBound() {
        TabletLockTaskConfirmation attempt = new TabletLockTaskConfirmation(500, 3000);
        assertEquals(Outcome.TIMED_OUT, attempt.observe(LOCKED, 3501));
    }

    @Test public void pinningIsImmediateFailureAndCannotTurnIntoSuccess() {
        TabletLockTaskConfirmation attempt = new TabletLockTaskConfirmation(500, 3000);
        assertEquals(Outcome.PINNED, attempt.observe(PINNED, 550));
        assertEquals(Outcome.PINNED, attempt.observe(LOCKED, 600));
    }

    @Test public void maintenanceOrDestroyCancelsAnyDelayedLockedResult() {
        TabletLockTaskConfirmation attempt = new TabletLockTaskConfirmation(500, 3000);
        assertEquals(Outcome.PENDING, attempt.observe(NONE, 600));
        attempt.cancel();
        assertEquals(Outcome.CANCELLED, attempt.observe(LOCKED, 700));
    }
}
