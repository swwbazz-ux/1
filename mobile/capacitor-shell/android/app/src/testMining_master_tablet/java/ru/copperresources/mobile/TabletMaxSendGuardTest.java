package ru.copperresources.mobile;

import java.util.Arrays;
import java.util.Collections;
import org.junit.Test;
import static org.junit.Assert.*;

public class TabletMaxSendGuardTest {
    private boolean ready(TabletMaxSendGuard request, long now) {
        return request.consume(now, "ru.oneme.app", "Поделиться сообщением",
            Collections.singletonList("Василий Сергеев"), true, "Сообщение", true);
    }

    @Test public void requiresStableScreenAndConsumesBeforeFirstClick() {
        TabletMaxSendGuard request = new TabletMaxSendGuard("расстановка", 100);
        assertFalse(ready(request, 200));
        assertFalse(ready(request, 599));
        assertTrue(ready(request, 600));
        assertFalse(ready(request, 1000));
    }

    @Test public void rejectsWrongRecipientPackageScreenTextAndManualComment() {
        for (int change = 0; change < 7; change++) {
            TabletMaxSendGuard request = new TabletMaxSendGuard("расстановка", 100);
            for (long now : new long[] {200, 600, 1000}) {
                assertFalse(request.consume(now, change == 0 ? "other.app" : "ru.oneme.app",
                    change == 1 ? "Василий Сергеев" : "Поделиться сообщением",
                    change == 2 ? Collections.singletonList("Избранное") : change == 3
                        ? Arrays.asList("Василий Сергеев", "Избранное") : Collections.singletonList("Василий Сергеев"),
                    change != 4, change == 5 ? "Чужой черновик" : "", change != 6));
            }
        }
    }

    @Test public void expiryAndProcessRecreationCannotReplayAnOldRequest() {
        TabletMaxSendGuard request = new TabletMaxSendGuard("расстановка", 100);
        assertFalse(ready(request, 14_900));
        assertFalse(ready(request, 15_100));
        assertTrue(request.expired(15_100));
        assertFalse(ready(new TabletMaxSendGuard("расстановка", 1000), 999));
    }

    @Test public void changedScreenRestartsTheStabilityDelay() {
        TabletMaxSendGuard request = new TabletMaxSendGuard("расстановка", 100);
        assertFalse(ready(request, 200));
        assertFalse(request.consume(500, "ru.oneme.app", "Поделиться сообщением", Collections.emptyList(), true, "", true));
        assertFalse(ready(request, 600));
        assertFalse(ready(request, 999));
        assertTrue(ready(request, 1000));
    }
}
