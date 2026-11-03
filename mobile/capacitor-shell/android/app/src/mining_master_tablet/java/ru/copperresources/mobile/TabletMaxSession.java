package ru.copperresources.mobile;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.UUID;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import org.json.JSONArray;
import org.json.JSONObject;

/** Device-private, no-backup session and outbox; the key never leaves Android Keystore. */
final class TabletMaxSession {
    interface Edit { void apply(JSONObject state) throws Exception; }
    private static final String ALIAS = "copper_max_transport_v1";
    private final AtomicFile file;
    TabletMaxSession(Context context) {
        file = new AtomicFile(new File(context.getNoBackupFilesDir(), "max-transport-v1.bin"));
    }
    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (!store.containsAlias(ALIAS)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(ALIAS, null);
    }
    synchronized JSONObject read() throws Exception {
        if (!file.getBaseFile().exists() && !new File(file.getBaseFile().getPath() + ".bak").exists()) {
            JSONObject state = new JSONObject().put("device_id", UUID.randomUUID().toString()).put("jobs", new JSONArray());
            write(state); return state;
        }
        byte[] bytes = file.readFully();
        if (bytes.length < 30 || bytes[0] != 1) throw new IllegalStateException("max_store_format");
        ByteBuffer buffer = ByteBuffer.wrap(bytes); buffer.get();
        int length = Byte.toUnsignedInt(buffer.get());
        if (length != 12 || buffer.remaining() <= length) throw new IllegalStateException("max_store_iv");
        byte[] iv = new byte[length]; buffer.get(iv);
        byte[] encrypted = new byte[buffer.remaining()]; buffer.get(encrypted);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
        return new JSONObject(new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8));
    }
    synchronized void write(JSONObject state) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] encrypted = cipher.doFinal(state.toString().getBytes(StandardCharsets.UTF_8)), iv = cipher.getIV();
        byte[] bytes = ByteBuffer.allocate(2 + iv.length + encrypted.length).put((byte) 1).put((byte) iv.length).put(iv).put(encrypted).array();
        FileOutputStream output = file.startWrite();
        try { output.write(bytes); file.finishWrite(output); }
        catch (Exception failure) { file.failWrite(output); throw failure; }
    }
    synchronized JSONObject change(Edit edit) throws Exception {
        JSONObject state = read(); edit.apply(state); write(state); return state;
    }
}
