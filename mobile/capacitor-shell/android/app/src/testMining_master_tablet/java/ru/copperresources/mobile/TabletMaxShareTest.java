package ru.copperresources.mobile;

import java.net.URI;
import java.net.URLDecoder;
import org.junit.Test;
import static org.junit.Assert.*;

public class TabletMaxShareTest {
    @Test public void encodesTheCompleteMessageAsOneQueryValue() throws Exception {
        String text = "№3 · гор. 150 · отвал\n12, 14\n\n№САТ-9 · ККД & 50% + ? #\n20, 24";
        URI uri = new URI(TabletMaxShare.url(text));
        assertEquals("https", uri.getScheme());
        assertEquals("max.ru", uri.getHost());
        assertEquals("/:share", uri.getPath());
        assertNull(uri.getFragment());
        assertFalse(uri.getRawQuery().contains("&"));
        assertEquals(text, URLDecoder.decode(uri.getRawQuery().substring("text=".length()), "UTF-8"));
    }

    @Test public void rejectsEmptyOrOversizedMessagesWithoutTruncation() {
        for (String invalid : new String[] {null, "", " \n ", "x".repeat(4001)}) {
            try { TabletMaxShare.url(invalid); fail("Invalid message was accepted"); }
            catch (IllegalArgumentException expected) { /* No launch. */ }
        }
        assertTrue(TabletMaxShare.url("x".repeat(4000)).endsWith("x".repeat(4000)));
    }
}
