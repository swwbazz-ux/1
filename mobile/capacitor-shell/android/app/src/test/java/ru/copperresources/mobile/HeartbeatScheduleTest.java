package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public final class HeartbeatScheduleTest {
    @Test public void simultaneousResumeAndNetworkWakeHaveOneQueuedOwner() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        assertEquals(0L, schedule.request(0L, 10_000L));
        for (int n = 0; n < 8; n++) assertEquals(-1L, schedule.request(0L, 10_000L));
        assertTrue(schedule.start(10_000L, schedule.generation()));
        assertFalse(schedule.start(10_000L, schedule.generation()));
    }

    @Test public void wakeWhileHttpInFlightUsesThatResponseAndKeepsNormalInterval() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        schedule.request(0L, 10_000L);
        schedule.start(10_000L, schedule.generation());
        for (int n = 0; n < 8; n++) assertEquals(-1L, schedule.request(0L, 10_500L));
        assertEquals(-1L, schedule.request(10_000L, 11_000L));
        schedule.responseSucceeded();
        assertEquals(10_000L, schedule.finish());
        assertEquals(10_000L, schedule.request(10_000L, 11_000L));
    }

    @Test public void changedNetworkDuringFailedRequestGetsOnePromptProbe() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        schedule.request(0L, 10_000L);
        schedule.start(10_000L, schedule.generation());
        for (int n = 0; n < 8; n++) schedule.request(0L, 10_500L);
        schedule.request(60_000L, 11_000L);
        assertEquals(1_000L, schedule.finish());
        assertEquals(1_000L, schedule.request(1_000L, 11_000L));
        assertEquals(-1L, schedule.request(1_000L, 11_000L));
    }

    @Test public void wakeCanAdvancePendingBackoffButNotPostponeEarlierProbe() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        assertEquals(60_000L, schedule.request(60_000L, 10_000L));
        assertEquals(0L, schedule.request(0L, 12_000L));
        assertEquals(-1L, schedule.request(60_000L, 12_000L));
    }

    @Test public void callbacksAfterQuickResponseRespectMinimumStartGap() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        schedule.request(0L, 10_000L);
        schedule.start(10_000L, schedule.generation());
        schedule.request(10_000L, 10_100L);
        assertEquals(10_000L, schedule.finish());
        assertEquals(900L, schedule.request(0L, 10_100L));
    }

    @Test public void cancelledPendingTaskCannotStealNewProbeOwnership() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        schedule.request(60_000L, 10_000L);
        long oldGeneration = schedule.generation();
        schedule.request(0L, 12_000L);
        assertFalse(schedule.start(12_000L, oldGeneration));
        assertTrue(schedule.start(12_000L, schedule.generation()));
    }

    @Test public void shutdownAndTaskRemovalCannotResurrectHeartbeatLoop() {
        HeartbeatSchedule schedule = new HeartbeatSchedule();
        schedule.request(0L, 10_000L);
        schedule.start(10_000L, schedule.generation());
        schedule.request(10_000L, 10_100L);
        schedule.stop();
        assertEquals(-1L, schedule.finish());
        assertEquals(-1L, schedule.request(0L, 11_000L));
        assertFalse(schedule.start(11_000L, schedule.generation()));
    }
}
