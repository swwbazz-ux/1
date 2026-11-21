package ru.copperresources.mobile;

import java.util.Arrays;
import java.util.Collections;
import org.junit.Test;
import static org.junit.Assert.*;

public class TabletTvGuardTest {
    private final TabletTvGuard.Peer target = new TabletTvGuard.Peer("aa:bb:cc:dd:ee:ff", "Samsung Зал", true, true, true);
    @Test public void selectsEnrolledReceiverOnceWithinDeadline() {
        TabletTvGuard guard = new TabletTvGuard(target.address, 100);
        assertEquals(target.name, guard.label(Collections.singletonList(target), 99));
        assertTrue(guard.consume(target.name, 1, Collections.singletonList(target), 99));
        assertFalse(guard.consume(target.name, 1, Collections.singletonList(target), 99));
    }
    @Test public void neverSelectsAnotherReceiverWithTheSameName() {
        TabletTvGuard guard = new TabletTvGuard(target.address, 100);
        TabletTvGuard.Peer impostor = new TabletTvGuard.Peer("00:11:22:33:44:55", target.name, true, true, true);
        assertNull(guard.label(Collections.singletonList(impostor), 1));
        assertNull(guard.label(Arrays.asList(target, impostor), 1));
        assertFalse(guard.consume(target.name, 2, Collections.singletonList(target), 1));
    }
    @Test public void rejectsExpiredUnavailableUnpairedAndNonConnectableTargets() {
        TabletTvGuard guard = new TabletTvGuard(target.address, 100);
        assertNull(guard.label(Collections.singletonList(target), 100));
        for (boolean[] flags : new boolean[][]{{false,true,true},{true,false,true},{true,true,false}})
            assertNull(guard.label(Collections.singletonList(new TabletTvGuard.Peer(target.address,target.name,flags[0],flags[1],flags[2])),1));
        assertNull(guard.label(Collections.emptyList(),1));
    }
}
