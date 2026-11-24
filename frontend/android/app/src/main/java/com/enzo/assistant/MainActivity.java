package com.enzo.assistant;

import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebView;

import androidx.activity.OnBackPressedCallback;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(EnzoDevicePlugin.class);
        super.onCreate(savedInstanceState);
        // The keyboard shrinks the app (the reply box sits on top of it) instead of sliding the whole screen up.
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= 35) {   // edge-to-edge (Android 15+): Android no longer resizes for us, so leave room for the keyboard
            View root = findViewById(android.R.id.content);
            ViewCompat.setOnApplyWindowInsetsListener(root, (v, insets) -> {
                v.setPadding(0, 0, 0, insets.getInsets(WindowInsetsCompat.Type.ime()).bottom);
                return insets;
            });
        }
        // Back steps back inside the web app first (Chat -> Home), and only then leaves SHARP.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                WebView web = getBridge().getWebView();
                if (web.canGoBack()) { web.goBack(); return; }
                setEnabled(false);                     // let Android do its normal Back (to the background)
                getOnBackPressedDispatcher().onBackPressed();
                setEnabled(true);                      // still handled when SHARP comes back to the front
            }
        });
    }
}
