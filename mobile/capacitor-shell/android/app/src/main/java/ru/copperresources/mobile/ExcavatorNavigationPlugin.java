package ru.copperresources.mobile;

import android.graphics.Rect;
import android.os.Build;
import android.webkit.WebView;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.util.Collections;

@CapacitorPlugin(name = "ExcavatorNavigation")
public class ExcavatorNavigationPlugin extends Plugin {
    @PluginMethod
    public void setDowntimeEdge(PluginCall call) {
        double top = call.getDouble("top", 0.0);
        double bottom = call.getDouble("bottom", 0.0);
        getActivity().runOnUiThread(() -> {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                WebView view = getBridge().getWebView();
                int height = view.getHeight();
                int width = view.getWidth();
                float density = view.getResources().getDisplayMetrics().density;
                int y = (int) Math.round(top * height);
                int end = Math.min((int) Math.round(bottom * height), y + Math.round(160 * density));
                if (width > 0 && top >= 0 && bottom <= 1 && end > y) {
                    Rect edge = new Rect(Math.max(0, width - Math.round(16 * density)), y, width, end);
                    view.setSystemGestureExclusionRects(Collections.singletonList(edge));
                } else {
                    view.setSystemGestureExclusionRects(Collections.emptyList());
                }
            }
            call.resolve();
        });
    }
}
