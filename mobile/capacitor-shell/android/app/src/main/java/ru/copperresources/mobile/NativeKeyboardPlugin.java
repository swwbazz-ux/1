package ru.copperresources.mobile;

import android.content.Context;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebView;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "NativeKeyboard")
public class NativeKeyboardPlugin extends Plugin {
    @PluginMethod
    public void setAction(PluginCall call) {
        if (getActivity() == null || getBridge() == null || getBridge().getWebView() == null) {
            call.reject("WebView is unavailable");
            return;
        }
        String action = call.getString("action", "");
        getActivity().runOnUiThread(() -> {
            WebView webView = getBridge().getWebView();
            if (!(webView instanceof NativeImeWebView)) {
                call.reject("Native IME bridge is unavailable");
                return;
            }
            ((NativeImeWebView) webView).setNativeImeAction(action);
            JSObject result = new JSObject();
            result.put("action", action);
            call.resolve(result);
        });
    }

    @PluginMethod
    public void hide(PluginCall call) {
        if (getActivity() == null || getBridge() == null || getBridge().getWebView() == null) {
            call.reject("WebView is unavailable");
            return;
        }
        getActivity().runOnUiThread(() -> hideKeyboard(call));
    }

    private void hideKeyboard(PluginCall call) {
        WebView webView = getBridge().getWebView();
        if (webView instanceof NativeImeWebView) {
            ((NativeImeWebView) webView).clearNativeImeAction();
        }
        InputMethodManager inputMethodManager =
            (InputMethodManager) getContext().getSystemService(Context.INPUT_METHOD_SERVICE);
        boolean hidden = inputMethodManager != null
            && webView.getWindowToken() != null
            && inputMethodManager.hideSoftInputFromWindow(webView.getWindowToken(), 0);
        JSObject result = new JSObject();
        result.put("hidden", hidden);
        call.resolve(result);
    }
}
