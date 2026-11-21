package ru.copperresources.mobile;

import java.io.IOException;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Iterator;
import java.util.Map;
import net.jpountz.lz4.LZ4Factory;
import org.json.JSONArray;
import org.json.JSONObject;
import org.msgpack.core.MessageBufferPacker;
import org.msgpack.core.MessagePack;
import org.msgpack.core.MessageUnpacker;
import org.msgpack.value.Value;

/** MAX binary WebSocket frames. The compression byte bounds the output size of a raw LZ4 block. */
final class TabletMaxFrame {
    private static final int VERSION = 10;
    private static final int HEADER_BYTES = 10;
    private static final int MAX_PAYLOAD = 16 * 1024 * 1024 - 1;
    private static final int MAX_DEPTH = 64;

    final int command, sequence, opcode;
    final JSONObject payload;

    private TabletMaxFrame(int command, int sequence, int opcode, JSONObject payload) {
        this.command = command;
        this.sequence = sequence;
        this.opcode = opcode;
        this.payload = payload;
    }

    static byte[] request(int sequence, int opcode, JSONObject body) throws Exception {
        byte[] bytes;
        try (MessageBufferPacker out = MessagePack.newDefaultBufferPacker()) {
            pack(out, body);
            bytes = out.toByteArray();
        }
        if (bytes.length > MAX_PAYLOAD) throw invalid();
        int ratio = 0;
        if (bytes.length > 32) {
            byte[] compressed = LZ4Factory.safeInstance().fastCompressor().compress(bytes);
            if (compressed.length < bytes.length) {
                ratio = Math.min(255, (bytes.length + compressed.length - 1) / compressed.length);
                if ((long) ratio * compressed.length >= bytes.length) bytes = compressed;
                else ratio = 0;
            }
        }
        return ByteBuffer.allocate(HEADER_BYTES + bytes.length).order(ByteOrder.BIG_ENDIAN)
            .put((byte) VERSION).put((byte) 0).putShort((short) sequence).putShort((short) opcode)
            .putInt((ratio << 24) | bytes.length).put(bytes).array();
    }

    static TabletMaxFrame read(ByteBuffer raw) throws Exception {
        ByteBuffer in = raw.slice().order(ByteOrder.BIG_ENDIAN);
        if (in.remaining() < HEADER_BYTES || (in.get() & 255) != VERSION) throw invalid();
        int command = in.get() & 255;
        int sequence = in.getShort() & 65535;
        int opcode = in.getShort() & 65535;
        int lengthAndRatio = in.getInt();
        int ratio = lengthAndRatio >>> 24;
        int length = lengthAndRatio & MAX_PAYLOAD;
        if (length != in.remaining()) throw invalid();
        if (length == 0) return new TabletMaxFrame(command, sequence, opcode, new JSONObject());

        byte[] bytes = new byte[length];
        in.get(bytes);
        if (ratio != 0) {
            byte[] expanded = new byte[(int) Math.min(MAX_PAYLOAD, (long) length * ratio)];
            int size = LZ4Factory.safeInstance().safeDecompressor()
                .decompress(bytes, 0, bytes.length, expanded, 0, expanded.length);
            bytes = java.util.Arrays.copyOf(expanded, size);
        }
        try (MessageUnpacker input = MessagePack.newDefaultUnpacker(bytes)) {
            Object body = unpack(input.unpackValue(), 0);
            if (input.hasNext() || !(body instanceof JSONObject)) throw invalid();
            return new TabletMaxFrame(command, sequence, opcode, (JSONObject) body);
        }
    }

    private static void pack(MessageBufferPacker out, Object value) throws Exception {
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            out.packMapHeader(object.length());
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                out.packString(key);
                pack(out, object.get(key));
            }
        } else if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            out.packArrayHeader(array.length());
            for (int i = 0; i < array.length(); i++) pack(out, array.get(i));
        } else if (value == null || value == JSONObject.NULL) out.packNil();
        else if (value instanceof String) out.packString((String) value);
        else if (value instanceof Boolean) out.packBoolean((Boolean) value);
        else if (value instanceof Float || value instanceof Double) out.packDouble(((Number) value).doubleValue());
        else if (value instanceof BigInteger) {
            try (MessageBufferPacker nested = MessagePack.newDefaultBufferPacker()) {
                nested.packBigInteger((BigInteger) value);
                byte[] encoded = nested.toByteArray();
                out.packExtensionTypeHeader((byte) 1, encoded.length).writePayload(encoded);
            }
        } else if (value instanceof Number) out.packLong(((Number) value).longValue());
        else throw invalid();
    }

    private static Object unpack(Value value, int depth) throws Exception {
        if (depth > MAX_DEPTH) throw invalid();
        switch (value.getValueType()) {
            case NIL: return JSONObject.NULL;
            case BOOLEAN: return value.asBooleanValue().getBoolean();
            case INTEGER: return value.asIntegerValue().toBigInteger();
            case FLOAT: return value.asFloatValue().toDouble();
            case STRING: return value.asStringValue().asString();
            case MAP:
                JSONObject object = new JSONObject();
                for (Map.Entry<Value, Value> entry : value.asMapValue().entrySet()) {
                    Object key = unpack(entry.getKey(), depth + 1);
                    if (!(key instanceof String) && !(key instanceof Number)) throw invalid();
                    object.put(key.toString(), unpack(entry.getValue(), depth + 1));
                }
                return object;
            case ARRAY:
                JSONArray array = new JSONArray();
                for (Value item : value.asArrayValue()) array.put(unpack(item, depth + 1));
                return array;
            case EXTENSION:
                if (value.asExtensionValue().getType() == 1) {
                    try (MessageUnpacker nested = MessagePack.newDefaultUnpacker(value.asExtensionValue().getData())) {
                        BigInteger number = nested.unpackBigInteger();
                        if (nested.hasNext()) throw invalid();
                        return number;
                    }
                }
                return JSONObject.NULL;
            case BINARY:
                return JSONObject.NULL;
            default:
                throw invalid();
        }
    }

    private static IOException invalid() { return new IOException("max_frame"); }
}
