package com.enzo.assistant;

import android.animation.ValueAnimator;
import android.content.Intent;
import android.graphics.Rect;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.util.DisplayMetrics;
import android.view.DisplayCutout;
import android.view.Gravity;
import android.view.ViewGroup;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.view.animation.PathInterpolator;
import android.Manifest;
import android.content.pm.PackageManager;
import android.webkit.PermissionRequest;
import android.webkit.WebView;
import android.widget.FrameLayout;

import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebChromeClient;

/**
 * SHARP as a pill around the camera hole, over whatever app is open (like a dynamic island). The assistant shortcut
 * opens it (EnzoVoiceService). It grows down from the hole into a panel when there is something to read
 * (EnzoDevicePlugin.popupSize, called by the page). Touches outside it go to the app behind, so that app stays usable.
 * It is the same web app as MainActivity: EnzoDevicePlugin.isPopup tells the page to draw its compact layout.
 */
public class PopupActivity extends BridgeActivity {

    static PopupActivity live;   // the open popup, if any (EnzoVoiceService.PillSession talks to it)
    EnzoVoiceService.PillSession session;   // when set, the pill is drawn in the assistant's window, not here
    private FrameLayout pill;   // holds the web view inside the session window
    private GradientDrawable bg;
    private int holeX, holeY;   // camera hole centre (screen pixels)
    private boolean expanded;
    private float shown;   // 0 = pill, 1 = panel
    private ValueAnimator anim;
    private boolean micStarted;
    private boolean closing;
    private boolean revealed;
    // Enzo's motion (board 13): sheets and the island use this curve; open 380 ms, close 240 ms
    static final PathInterpolator DRAWER = new PathInterpolator(.32f, .72f, 0f, 1f);
    static final float HOLE_SX = .2f, HOLE_SY = .5f;   // the pill's size while it is still "inside" the camera hole

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(EnzoDevicePlugin.class);
        super.onCreate(savedInstanceState);
        // Capacitor asks for the mic through this Activity, and the answer never comes while it is behind the app he
        // uses (the pill lives in the assistant window). The permission is already granted, so grant the page directly.
        getBridge().getWebView().setWebChromeClient(new BridgeWebChromeClient(getBridge()) {
            @Override
            public void onPermissionRequest(PermissionRequest r) {
                boolean audioOnly = r.getResources().length == 1 && PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r.getResources()[0]);
                if (audioOnly && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) r.grant(r.getResources());
                else super.onPermissionRequest(r);
            }
        });

        DisplayMetrics dm = getResources().getDisplayMetrics();
        holeX = dm.widthPixels / 2;
        int bar = getResources().getIdentifier("status_bar_height", "dimen", "android");
        holeY = (bar > 0 ? getResources().getDimensionPixelSize(bar) : Math.round(24 * dm.density)) / 2;
        if (Build.VERSION.SDK_INT >= 30) {
            DisplayCutout c = getWindowManager().getCurrentWindowMetrics().getWindowInsets().getDisplayCutout();
            Rect top = c == null ? null : c.getBoundingRectTop();
            if (top != null && !top.isEmpty()) { holeX = top.centerX(); holeY = top.centerY(); }
        }

        Window w = getWindow();
        WindowManager.LayoutParams p = w.getAttributes();
        p.gravity = Gravity.TOP | Gravity.LEFT;
        if (Build.VERSION.SDK_INT >= 28) p.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        w.setAttributes(p);
        w.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
        w.clearFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);

        bg = new GradientDrawable();
        bg.setColor(0xFF000000);
        View decor = w.getDecorView();
        decor.setBackground(bg);
        decor.setClipToOutline(true);
        // The status bar would draw over the pill's row (clock on the bars, battery on the close button) while it is open.
        WindowInsetsControllerCompat bars = WindowCompat.getInsetsController(w, decor);
        bars.setSystemBarsBehavior(WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        bars.hide(WindowInsetsCompat.Type.statusBars());
        apply(0f);
        live = this;
        if (EnzoVoiceService.PillSession.showing != null) moveInto(EnzoVoiceService.PillSession.showing);
    }

    /** Hand the page over to the assistant's window (only the pill takes touches there), then step behind the app he
     *  was using: this Activity's window would otherwise block every touch on the screen (Android 15+ input sink). */
    void moveInto(EnzoVoiceService.PillSession s) {
        if (s.root == null) return;
        session = s;
        WebView web = getBridge().getWebView();
        // Only the pill may show: this Activity's own window stays empty and clear (it shared the pill's drawable,
        // and the two fought over it while the pill grew: the flicker on open).
        getWindow().getDecorView().setBackground(null);
        if (pill == null) {
            pill = new FrameLayout(this);
            pill.setBackground(bg);
            pill.setClipToOutline(true);
            s.root.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> { if (session != null) apply(shown); });
        }
        if (web.getParent() != pill) {
            if (web.getParent() != null) ((ViewGroup) web.getParent()).removeView(web);
            pill.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        boolean appearing = pill.getParent() == null;
        if (pill.getParent() != s.root) {
            if (pill.getParent() != null) ((ViewGroup) pill.getParent()).removeView(pill);
            s.root.addView(pill);
        }
        apply(shown);
        if (appearing && !revealed) {
            // The page loads fresh on every open: grow an empty pill now, fade the page in once it has drawn (its
            // first popupSize call), so it never zooms in half-drawn. A slow page shows anyway after 2.5 s.
            web.setAlpha(0f);
            pill.postDelayed(this::reveal, 2500);
        }
        if (appearing) growFromHole();
        if (micStarted) { stepBack(); return; }
        micStarted = true;   // EnzoMicService calls stepBack once it runs (the mic needs it started while on top)
        try {
            Intent mic = new Intent(this, EnzoMicService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(mic); else startService(mic);
        } catch (RuntimeException e) {
            stepBack();
        }
    }

    /** The pill comes out of the camera hole: small and clear to full size, 380 ms. The pivot is set by apply(), from
     *  the same numbers that place the pill, so it is right from the first frame; withLayer scales one picture of the
     *  page instead of redrawing the web view every frame. */
    private void growFromHole() {
        pill.setScaleX(HOLE_SX);
        pill.setScaleY(HOLE_SY);
        pill.setAlpha(0f);
        pill.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(380).setInterpolator(DRAWER).withLayer().start();
    }

    /** The page fades in over the grown pill (once). */
    void reveal() {
        if (revealed) return;
        revealed = true;
        getBridge().getWebView().animate().alpha(1f).setDuration(180).start();
    }

    /** Close: the island shrinks back into the camera hole (240 ms), then the popup ends. */
    void closeToHole() {
        if (closing) return;
        closing = true;
        if (pill == null || pill.getParent() == null || !pill.isAttachedToWindow()) { finishAndRemoveTask(); return; }
        if (anim != null) anim.cancel();
        pill.animate().cancel();
        pill.animate().scaleX(HOLE_SX).scaleY(HOLE_SY).alpha(0f).setDuration(240).setInterpolator(DRAWER).withLayer()
            .withEndAction(this::finishAndRemoveTask).start();
    }

    void stepBack() {
        if (session != null && !isFinishing()) moveTaskToBack(true);
    }

    @Override
    public void onDestroy() {
        if (live == this) live = null;
        if (micStarted) stopService(new Intent(this, EnzoMicService.class));
        EnzoVoiceService.PillSession s = session;
        session = null;
        if (pill != null && pill.getParent() != null) ((ViewGroup) pill.getParent()).removeView(pill);
        if (s != null) s.finish();   // closes the assistant's window too
        super.onDestroy();
    }

    /** The pill (collapsed) or the panel under the camera (expanded). The page calls this through the plugin. */
    public void setExpanded(boolean on) {
        reveal();   // the popup page is up and drawing
        if (on == expanded) return;
        expanded = on;
        float from = on ? 0f : 1f;
        if (anim != null) { from = (float) anim.getAnimatedValue(); anim.cancel(); }
        anim = ValueAnimator.ofFloat(from, on ? 1f : 0f).setDuration(on ? 380 : 240);
        anim.setInterpolator(DRAWER);   // grows down from the hole, settles without bounce
        anim.addUpdateListener(a -> apply((float) a.getAnimatedValue()));
        anim.start();
    }

    /** t = 0: a 170 x 40 dp pill centred on the camera hole; t = 1: a panel almost as wide as the screen, top edge kept. */
    private void apply(float t) {
        shown = t;
        DisplayMetrics dm = getResources().getDisplayMetrics();
        float d = dm.density;
        int pillW = Math.round(170 * d), pillH = Math.round(40 * d), margin = Math.round(10 * d);
        int width = Math.round(pillW + t * (dm.widthPixels - 2 * margin - pillW));
        int height = Math.round(pillH + t * (dm.heightPixels * 0.24f - pillH));
        int x = Math.max(0, Math.min(dm.widthPixels - width, holeX - width / 2)), y = Math.max(0, holeY - pillH / 2);
        bg.setCornerRadius(pillH / 2f + t * (28 * d - pillH / 2f));
        if (session != null && pill != null) {   // in the session window: place the pill by screen position
            int[] at = new int[2];
            session.root.getLocationOnScreen(at);
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(width, height);
            lp.leftMargin = x - at[0];
            lp.topMargin = y - at[1];
            FrameLayout.LayoutParams old = (FrameLayout.LayoutParams) pill.getLayoutParams();
            if (old == null || old.width != width || old.height != height || old.leftMargin != lp.leftMargin || old.topMargin != lp.topMargin) pill.setLayoutParams(lp);
            pill.setPivotX(holeX - x);                 // scale in and out of the camera hole
            pill.setPivotY(Math.max(0, holeY - y));
            return;
        }
        WindowManager.LayoutParams p = getWindow().getAttributes();
        p.width = width;
        p.height = height;
        p.x = x;
        p.y = y;
        getWindow().setAttributes(p);
    }
}
