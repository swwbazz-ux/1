package ru.copperresources.mobile;

import android.content.Context;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputConnectionWrapper;

import com.getcapacitor.CapacitorWebView;

public class NativeImeWebView extends CapacitorWebView {
    private static final String ACTION_NEXT = "next";
    private static final String ACTION_DONE = "done";

    private volatile String armedAction = "";
    private volatile boolean consumeEnterRelease;

    public NativeImeWebView(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public void setNativeImeAction(String action) {
        if (ACTION_NEXT.equals(action) || ACTION_DONE.equals(action)) {
            armedAction = action;
        } else {
            armedAction = "";
        }
    }

    public void clearNativeImeAction() {
        armedAction = "";
        consumeEnterRelease = false;
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        InputConnection inputConnection = super.onCreateInputConnection(outAttrs);
        if (inputConnection == null) {
            return null;
        }
        return new InputConnectionWrapper(inputConnection, false) {
            @Override
            public boolean performEditorAction(int actionCode) {
                if (dispatchArmedAction()) {
                    return true;
                }
                return super.performEditorAction(actionCode);
            }

            @Override
            public boolean sendKeyEvent(KeyEvent event) {
                if (event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER) {
                    if (event.getAction() == KeyEvent.ACTION_DOWN && dispatchArmedAction()) {
                        consumeEnterRelease = true;
                        return true;
                    }
                    if (event.getAction() == KeyEvent.ACTION_UP && consumeEnterRelease) {
                        consumeEnterRelease = false;
                        return true;
                    }
                }
                return super.sendKeyEvent(event);
            }

            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                if (text != null && "\n".contentEquals(text) && dispatchArmedAction()) {
                    return true;
                }
                return super.commitText(text, newCursorPosition);
            }
        };
    }

    private boolean dispatchArmedAction() {
        String action = armedAction;
        if (!ACTION_NEXT.equals(action) && !ACTION_DONE.equals(action)) {
            return false;
        }
        armedAction = "";
        post(() -> evaluateJavascript(
            "window.dispatchEvent(new CustomEvent('native-ime-action',{detail:{action:'"
                + action + "'}}));",
            null
        ));
        return true;
    }
}
