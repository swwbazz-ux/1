package ru.copperresources.mobile;

import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

/** Network acceptance must refer to our exact text, client identity and recipient. */
public class TabletMaxReceiptTest {
    private JSONObject job() throws Exception { return new JSONObject().put("cid", 1710000000123L).put("text", "№3 · гор. 150 · ККД\n12, 14"); }
    private JSONObject receipt() throws Exception { return new JSONObject().put("id", "1234567890123456789").put("cid", 1710000000123L)
        .put("text", "№3 · гор. 150 · ККД\n12, 14").put("chatId", 700L).put("sender", 123L); }

    @Test public void acceptsOnlyMatchingServerReceipt() throws Exception {
        assertTrue(TabletMaxBackground.receiptMatches(receipt(), job(), 700L, 123L));
        assertFalse(TabletMaxBackground.receiptMatches(receipt().put("text", "Другой текст"), job(), 700L, 123L));
        assertFalse(TabletMaxBackground.receiptMatches(receipt().put("cid", 1710000000124L), job(), 700L, 123L));
        assertFalse(TabletMaxBackground.receiptMatches(receipt().put("sender", 124L), job(), 700L, 123L));
        assertFalse(TabletMaxBackground.receiptMatches(receipt().put("chatId", 701L), job(), 700L, 123L));
        JSONObject missing = receipt(); missing.remove("id");
        assertFalse(TabletMaxBackground.receiptMatches(missing, job(), 700L, 123L));
    }
    @Test public void unwrapsServerMessageWithoutDroppingRecipient() throws Exception {
        JSONObject response = new JSONObject().put("message", receipt()).put("chatId", 700L);
        assertTrue(TabletMaxBackground.receiptMatches(TabletMaxWire.message(response), job(), 700L, 123L));
        response.put("chatId", 701L);
        assertFalse(TabletMaxBackground.receiptMatches(TabletMaxWire.message(response), job(), 700L, 123L));
        assertTrue(TabletMaxBackground.receiptMatches(TabletMaxWire.message(receipt()), job(), 700L, 123L));
    }
}
