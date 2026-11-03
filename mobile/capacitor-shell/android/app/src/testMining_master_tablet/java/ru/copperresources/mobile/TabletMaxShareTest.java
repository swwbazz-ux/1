package ru.copperresources.mobile;

import org.junit.Test;
import static org.junit.Assert.*;

public class TabletMaxShareTest {
    @Test public void preservesTheCompleteUnicodeMessage() {
        String text = "№3 · гор. 150 · отвал\n12, 14\n\n№САТ-9 · ККД & 50% + ? #\n20, 24";
        assertEquals(text, TabletMaxShare.preparedText(text));
    }

    @Test public void onlyTheVerifiedEnabledShareIdentityIsAccepted() {
        assertTrue(TabletMaxShare.approvedShortcut("ru.oneme.app", "504342722", "Василий Сергеев", true, true));
        assertFalse(TabletMaxShare.approvedShortcut("other.app", "504342722", "Василий Сергеев", true, true));
        assertFalse(TabletMaxShare.approvedShortcut("ru.oneme.app", "0", "Василий Сергеев", true, true));
        assertFalse(TabletMaxShare.approvedShortcut("ru.oneme.app", "504342722", "Избранное", true, true));
        assertFalse(TabletMaxShare.approvedShortcut("ru.oneme.app", "504342722", null, true, true));
        assertFalse(TabletMaxShare.approvedShortcut("ru.oneme.app", "504342722", "Василий Сергеев", false, true));
        assertFalse(TabletMaxShare.approvedShortcut("ru.oneme.app", "504342722", "Василий Сергеев", true, false));
    }

    @Test public void rejectsEmptyOrOversizedMessagesWithoutTruncation() {
        for (String invalid : new String[] {null, "", " \n ", "x".repeat(4001)}) {
            try { TabletMaxShare.preparedText(invalid); fail("Invalid message was accepted"); }
            catch (IllegalArgumentException expected) { /* No launch. */ }
        }
        assertEquals("x".repeat(4000), TabletMaxShare.preparedText("x".repeat(4000)));
    }
}
