package ru.copperresources.mobile;

import android.util.Base64;
import android.webkit.WebView;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import org.json.JSONObject;

/** Write-only, bounded transfer of a previously fetched export to the same protected
 * reports directory as the online downloader. Never exposes private files to JavaScript. */
final class TabletOfflineReportBridge {
    private static final String NAME = "CopperOfflineReport";
    private static final long LIMIT = 50L * 1024 * 1024;
    private static final int CHUNK = 48 * 1024;
    private final File directory;
    private final ExecutorService worker;
    private volatile boolean closed;
    private String id, expectedHash;
    private File partial, target;
    private FileOutputStream stream;
    private MessageDigest digest;
    private long length, size;
    private final byte[] prefix = new byte[8];
    private int prefixSize;
    TabletOfflineReportBridge(File files, ExecutorService worker) {
        this.directory = new File(files, "reports"); this.worker = worker;
    }
    void install(WebView view) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return;
        WebViewCompat.addWebMessageListener(view, NAME, Collections.singleton(TabletUrlPolicy.ORIGIN),
            (source, message, origin, mainFrame, reply) -> {
                if (closed || source != view || !mainFrame || !TabletUrlPolicy.ORIGIN.equals(origin.toString())
                    || !TabletUrlPolicy.tabletPage(view.getUrl()) || message.getType()!=WebMessageCompat.TYPE_STRING) return;
                String raw=message.getData();
                if(raw==null || raw.length()>CHUNK*2) return;
                worker.execute(()->{
                    JSONObject response=new JSONObject();
                    try {
                        JSONObject m=new JSONObject(raw);
                        response.put("id",m.optString("id")).put("serial",m.getInt("serial"));
                        process(m);
                        response.put("ok",true);
                    } catch(Exception err) {
                        discard();
                        try { response.put("ok",false); } catch(Exception ignored) { }
                    }
                    view.post(()->{ if(!closed) reply.postMessage(response.toString()); });
                });
            });
    }
    private void process(JSONObject m) throws Exception {
        if(closed) throw new IOException("Activity closed");
        String requestId=m.getString("id"),op=m.getString("op");
        if(!UUID.fromString(requestId).toString().equals(requestId)) throw new IOException("Invalid ID");
        if("begin".equals(op)){
            discard();
            size=m.getLong("size"); long created=m.getLong("at"); expectedHash=m.getString("sha256");
            String name=m.getString("name").replaceAll("[^\\p{L}\\p{N}._ -]","_");
            if(name.length()>80) name=name.substring(name.length()-80);
            if(created<=0 || size<1 || size>LIMIT || !expectedHash.matches("[a-f0-9]{64}")
                || !name.toLowerCase(Locale.ROOT).matches(".+\\.(xlsx|xls|csv|pdf)")) throw new IOException("Invalid export");
            if(!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Storage unavailable");
            target=new File(directory,created+"-"+requestId+"-"+name);
            partial=File.createTempFile("report-",".part",directory);
            stream=new FileOutputStream(partial); digest=MessageDigest.getInstance("SHA-256");
            length=0;prefixSize=0;id=requestId;
        } else if(!requestId.equals(id) || stream==null) throw new IOException("Unknown transfer");
        else if("chunk".equals(op)){
            if(m.getLong("offset")!=length)throw new IOException("Gap in transfer");
            byte[] bytes=Base64.decode(m.getString("bytes"),Base64.NO_WRAP);
            if(bytes.length==0 || bytes.length>CHUNK || length+bytes.length>size)throw new IOException("Invalid chunk");
            for(int i=0;i<bytes.length && prefixSize<prefix.length;i++)prefix[prefixSize++]=bytes[i];
            stream.write(bytes);digest.update(bytes);length+=bytes.length;
        } else if("commit".equals(op)){
            if(length!=size || !expectedHash.equals(hex(digest.digest())) || !validPrefix(target.getName()))throw new IOException("Incomplete export");
            stream.getFD().sync();stream.close();stream=null;
            if(target.exists()){
                if(target.length()!=size)throw new IOException("Export ID collision");
                // A retry after a lost bridge ACK must not produce a duplicate visible file.
                MessageDigest old=MessageDigest.getInstance("SHA-256");
                try(java.io.FileInputStream input=new java.io.FileInputStream(target)){
                    byte[] buf=new byte[16384];int n;while((n=input.read(buf))!=-1)old.update(buf,0,n);
                }
                if(!expectedHash.equals(hex(old.digest())))throw new IOException("Export ID collision");
            }else if(!partial.renameTo(target)) throw new IOException("Could not save export");
            discard();
        } else throw new IOException("Unknown operation");
    }
    private boolean validPrefix(String filename){
        String name=filename.toLowerCase(Locale.ROOT);
        if(name.endsWith(".xlsx"))return prefixSize>=4 && prefix[0]=='P' && prefix[1]=='K' && prefix[2]==3 && prefix[3]==4;
        if(name.endsWith(".pdf"))return prefixSize>=5 && prefix[0]=='%' && prefix[1]=='P' && prefix[2]=='D' && prefix[3]=='F' && prefix[4]=='-';
        if(name.endsWith(".xls"))return prefixSize>=4 && (prefix[0]&255)==0xd0 && (prefix[1]&255)==0xcf && (prefix[2]&255)==0x11 && (prefix[3]&255)==0xe0;
        return name.endsWith(".csv") && prefixSize>0;
    }
    private static String hex(byte[] bytes){StringBuilder result=new StringBuilder();for(byte b:bytes)result.append(String.format(Locale.ROOT,"%02x",b&255));return result.toString();}
    private void discard(){
        if(stream!=null){try{stream.close();}catch(IOException ignored){} stream=null;}
        if(partial!=null){partial.delete();partial=null;}
        id=null;target=null;digest=null;length=0;
    }
    void destroy(){closed=true;worker.execute(this::discard);}
}
