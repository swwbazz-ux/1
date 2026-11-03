package ru.copperresources.mobile;

import java.net.URI;
import java.util.Collections;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import javax.net.ssl.SSLParameters;
import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;
import org.json.JSONArray;
import org.json.JSONObject;

/** Minimal user-account transport. Schemas: MIT MaxApiTeam/PyMax v2.1.0; see assets/max-transport-NOTICE.txt.
 * No hidden windows, UI clicks, server relay, or access to the installed MAX application's credentials.
 */
final class TabletMaxWire implements AutoCloseable {
    // The published Android Direct Share ID identifies the approved DIALOG, not a contact.
    static final long APPROVED_CHAT = 504342722L;
    static final class Recipient {
        final long chat, user;
        Recipient(long chat, long user) { this.chat = chat; this.user = user; }
    }
    static final class Rejected extends Exception {
        final String code;
        Rejected(String value) { super("max_operation_rejected"); code = value.matches("[A-Za-z][A-Za-z0-9_.-]{0,79}") ? value : "rejected"; }
    }
    private final AtomicInteger sequence = new AtomicInteger();
    private static final class Pending {
        final int opcode; final CompletableFuture<JSONObject> result = new CompletableFuture<>();
        Pending(int opcode) { this.opcode = opcode; }
    }
    private final Map<Integer, Pending> pending = new ConcurrentHashMap<>();
    private final WebSocketClient socket;

    TabletMaxWire(String deviceId) throws Exception {
        socket = new WebSocketClient(URI.create("wss://ws-api.oneme.ru/websocket"), Collections.singletonMap("Origin", "https://web.max.ru")) {
            @Override public void onOpen(ServerHandshake response) {}
            @Override public void onSetSSLParameters(SSLParameters parameters) {
                parameters.setEndpointIdentificationAlgorithm("HTTPS"); super.onSetSSLParameters(parameters);
            }
            @Override public void onMessage(String raw) {
                if (raw.length() > 16 * 1024 * 1024) { close(); return; }
                try {
                    JSONObject frame = new JSONObject(raw);
                    int cmd = frame.optInt("cmd"), seq = frame.optInt("seq", -1);
                    Pending request = pending.get(seq);
                    if (request == null || request.opcode != frame.optInt("opcode") || (cmd != 1 && cmd != 3)) return;
                    pending.remove(seq);
                    if (cmd == 3) {
                        JSONObject body = frame.optJSONObject("payload");
                        request.result.completeExceptionally(new Rejected(body == null ? "rejected" : body.optString("error", "rejected")));
                    }
                    else request.result.complete(frame.optJSONObject("payload") == null ? new JSONObject() : frame.getJSONObject("payload"));
                } catch (Exception invalid) { failPending(new IllegalStateException("max_frame")); }
            }
            @Override public void onClose(int code, String reason, boolean remote) { failPending(new IllegalStateException("max_disconnected")); }
            @Override public void onError(Exception failure) { failPending(new IllegalStateException("max_connection")); }
        };
        socket.setConnectionLostTimeout(30);
        if (!socket.connectBlocking(20, TimeUnit.SECONDS)) { socket.close(); throw new IllegalStateException("max_connect_timeout"); }
        JSONObject user = new JSONObject().put("deviceType", "WEB").put("locale", "ru").put("deviceLocale", "ru")
            .put("osVersion", "Android 11").put("deviceName", "Chrome")
            .put("headerUserAgent", "Mozilla/5.0 (Linux; Android 11; Tab11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
            .put("appVersion", "26.5.5").put("screen", "1200x2000 1.0x").put("timezone", "Europe/Samara");
        try { rpc(6, new JSONObject().put("deviceId", deviceId).put("userAgent", user)); }
        catch (Exception failure) { socket.close(); throw failure; }
    }
    private void failPending(Exception failure) {
        for (Pending request : pending.values()) request.result.completeExceptionally(failure);
        pending.clear();
    }
    JSONObject rpc(int opcode, JSONObject payload) throws Exception {
        int seq = sequence.incrementAndGet(); Pending request = new Pending(opcode); pending.put(seq, request);
        try {
            socket.send(new JSONObject().put("ver", 11).put("cmd", 0).put("seq", seq).put("opcode", opcode).put("payload", payload).toString());
            return request.result.get(25, TimeUnit.SECONDS);
        } catch (java.util.concurrent.ExecutionException wrapped) {
            if (wrapped.getCause() instanceof Rejected) throw (Rejected) wrapped.getCause();
            throw new IllegalStateException("max_request_failed");
        } finally { pending.remove(seq); }
    }
    JSONObject requestQr() throws Exception { return rpc(288, new JSONObject()); }
    boolean qrConfirmed(String track) throws Exception {
        JSONObject response = rpc(289, new JSONObject().put("trackId", track));
        JSONObject status = response.optJSONObject("status"); return status != null && status.optBoolean("loginAvailable");
    }
    String confirmQr(String track) throws Exception {
        JSONObject response = rpc(291, new JSONObject().put("trackId", track));
        JSONObject attrs = response.optJSONObject("tokenAttrs"), login = attrs == null ? null : attrs.optJSONObject("LOGIN");
        if (login == null || login.optString("token").isEmpty()) throw new IllegalStateException("max_additional_verification");
        return login.getString("token");
    }
    JSONObject login(String token) throws Exception {
        return rpc(19, new JSONObject().put("token", token).put("interactive", false).put("chatsCount", 40)
            .put("chatsSync", -1).put("contactsSync", -1).put("presenceSync", -1).put("draftsSync", -1));
    }
    static String name(JSONObject contact) {
        JSONArray names = contact.optJSONArray("names");
        if (names == null || names.length() == 0) return "";
        JSONObject item = names.optJSONObject(0);
        if (item == null) return "";
        String full = item.optString("name");
        return full.isEmpty() ? (item.optString("firstName") + " " + item.optString("lastName")).trim() : full;
    }
    Recipient verifyRecipient(JSONObject login, long self) throws Exception {
        JSONArray chats = login.optJSONArray("chats"); long peer = 0; int matches = 0;
        if (chats != null) for (int i = 0; i < chats.length(); i++) {
            JSONObject chat = chats.getJSONObject(i), participants = chat.optJSONObject("participants");
            if (chat.optLong("id") == APPROVED_CHAT && "DIALOG".equals(chat.optString("type")) && participants != null) {
                java.util.Iterator<String> keys = participants.keys(); int others = 0;
                while (keys.hasNext()) { long id = Long.parseLong(keys.next()); if (id != self) { peer = id; others++; } }
                if (others != 1 || participants.length() > 2) throw new IllegalStateException("max_dialog_members");
                matches++;
            }
        }
        if (matches != 1 || peer == 0 || peer == self) throw new IllegalStateException("max_recipient_chat");
        JSONArray contacts = rpc(32, new JSONObject().put("contactIds", new JSONArray().put(peer))).optJSONArray("contacts");
        if (contacts == null) throw new IllegalStateException("max_recipient_identity");
        JSONObject approved = null;
        for (int i = 0; i < contacts.length(); i++) if (contacts.getJSONObject(i).optLong("id") == peer) approved = contacts.getJSONObject(i);
        boolean named = false;
        if (approved != null) {
            JSONArray names = approved.optJSONArray("names");
            if (names != null) for (int i = 0; i < names.length(); i++) {
                JSONObject item = names.getJSONObject(i);
                String value = (item.optString("name") + " " + item.optString("firstName") + " " + item.optString("lastName"))
                    .toLowerCase(java.util.Locale.ROOT);
                java.util.List<String> words = java.util.Arrays.asList(value.split("[^а-яё]+"));
                if (words.contains("василий") && words.contains("сергеев")) named = true;
            }
            android.util.Log.i("MiningMasterTablet", "max_background approved_peer id=" + peer
                + " name=" + name(approved).replaceAll("[^А-Яа-яЁёA-Za-z .-]", "") + " names_match=" + named);
        }
        String link = approved == null ? "" : approved.optString("link");
        String expected = TabletMaxShare.RECIPIENT_URL.substring(TabletMaxShare.RECIPIENT_URL.lastIndexOf('/') + 1);
        if (approved == null || (!TabletMaxShare.RECIPIENT_URL.equals(link) && !expected.equals(link) && !named))
            throw new IllegalStateException("max_recipient_identity");
        android.util.Log.i("MiningMasterTablet", "max_background recipient_check chat_verified=true profile_verified=true");
        return new Recipient(APPROVED_CHAT, peer);
    }
    JSONObject send(long chat, long cid, String text) throws Exception {
        return message(rpc(64, new JSONObject().put("chatId", chat).put("notify", true).put("message", new JSONObject()
            .put("text", text).put("cid", cid).put("elements", new JSONArray()).put("attaches", new JSONArray()))));
    }
    static JSONObject message(JSONObject payload) throws Exception {
        JSONObject nested = payload.optJSONObject("message");
        if (nested == null) return payload;
        JSONObject result = new JSONObject(nested.toString());
        if (payload.has("chatId")) result.put("chatId", payload.get("chatId"));
        return result;
    }
    JSONArray history(long chat) throws Exception {
        JSONObject response = rpc(49, new JSONObject().put("chatId", chat).put("forward", 0).put("backward", 100)
            .put("backwardTime", 0).put("forwardTime", 0).put("getChat", false).put("from", System.currentTimeMillis())
            .put("itemType", "REGULAR").put("getMessages", true).put("interactive", false));
        return response.optJSONArray("messages");
    }
    @Override public void close() { socket.close(); failPending(new IllegalStateException("max_closed")); }
}
