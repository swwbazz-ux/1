package ru.copperresources.mobile;

import static org.junit.Assert.assertEquals;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.Arrays;
import net.jpountz.lz4.LZ4Factory;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.msgpack.core.MessageBufferPacker;
import org.msgpack.core.MessagePack;

public class TabletMaxFrameTest {
    @Test public void preservesMessageIdAndText() throws Exception {
        long id = 117554637261445885L;
        JSONObject body = new JSONObject().put("id", id).put("text", "К-99, самосвал №1").put("items", new JSONArray().put(true));
        TabletMaxFrame frame = TabletMaxFrame.read(ByteBuffer.wrap(TabletMaxFrame.request(65534, 64, body)));
        assertEquals(65534, frame.sequence);
        assertEquals(64, frame.opcode);
        assertEquals(id, frame.payload.getLong("id"));
        assertEquals(body.getString("text"), frame.payload.getString("text"));
    }

    @Test public void decodesCompressedReply() throws Exception {
        String data = "ABCDEF".repeat(80);
        byte[] body;
        try (MessageBufferPacker packer = MessagePack.newDefaultBufferPacker()) {
            packer.packMapHeader(1).packString("message").packString(data);
            body = packer.toByteArray();
        }
        byte[] compressed = LZ4Factory.safeInstance().fastCompressor().compress(body);
        int ratio = (body.length + compressed.length - 1) / compressed.length;
        ByteBuffer reply = ByteBuffer.allocate(10 + compressed.length);
        reply.put((byte)10).put((byte)1).putShort((short)1).putShort((short)6)
            .putInt((ratio << 24) | compressed.length).put(compressed).flip();
        TabletMaxFrame decoded = TabletMaxFrame.read(reply);
        assertEquals(1, decoded.command);
        assertEquals(data, decoded.payload.getString("message"));
    }

    @Test public void decodesExtendedIntegerMapKey() throws Exception {
        long id = 117554637261445885L;
        byte[] key;
        try (MessageBufferPacker packer = MessagePack.newDefaultBufferPacker()) {
            packer.packLong(id);
            key = packer.toByteArray();
        }
        byte[] body;
        try (MessageBufferPacker packer = MessagePack.newDefaultBufferPacker()) {
            packer.packMapHeader(1).packExtensionTypeHeader((byte) 1, key.length)
                .writePayload(key).packInt(5);
            body = packer.toByteArray();
        }
        ByteBuffer reply = ByteBuffer.allocate(10 + body.length);
        reply.put((byte) 10).put((byte) 1).putShort((short) 1).putShort((short) 19)
            .putInt(body.length).put(body).flip();
        assertEquals(5, TabletMaxFrame.read(reply).payload.getInt(Long.toString(id)));
    }

    @Test(expected = IOException.class) public void rejectsTruncatedReply() throws Exception {
        byte[] frame = TabletMaxFrame.request(1, 6, new JSONObject().put("message", "hello"));
        TabletMaxFrame.read(ByteBuffer.wrap(Arrays.copyOf(frame, frame.length - 1)));
    }
}
