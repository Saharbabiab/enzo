package com.enzo.assistant;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executor;

/**
 * SHARP's eyes and fingers on the phone screen, through EnzoAccessibilityService. Only runs when SHARP asks
 * (the phone_screen tool); the backend asks the user before every tap and every typing.
 *  - read: the app in front (not SHARP's own popup), its visible items numbered, and a small JPEG screenshot.
 *  - tap / type / scroll on a number from the last read; back / home.
 */
final class EnzoScreen {

    interface Done { void done(JSObject result); }

    private static final List<AccessibilityNodeInfo> items = new ArrayList<>();
    private static final int MAX_ITEMS = 150;

    private EnzoScreen() { }

    static void run(String what, int target, String text, Executor main, Done cb) {
        EnzoAccessibilityService s = EnzoAccessibilityService.get();
        if (s == null) { cb.done(err("Screen access is off. Turn on \"Enzo\" in Android settings > Accessibility > Installed apps (the top switch, not the shortcut).")); return; }
        try {
            switch (what) {
                case "read": read(s, main, cb, null, null); return;
                case "point": cb.done(point(s, target, text)); return;
                case "circle":
                    EnzoOverlay.circle(s, text, 40000, (path, bounds) -> {
                        if (path == null) { cb.done(err("He did not circle anything (cancelled or no answer).")); return; }
                        new android.os.Handler(android.os.Looper.getMainLooper())
                            .postDelayed(() -> read(s, main, cb, path, bounds), 250);   // the overlay is gone from the screenshot
                    });
                    return;
                case "open": cb.done(open(s, text)); return;
                case "tap": cb.done(tap(s, target)); return;
                case "type": cb.done(type(s, target, text)); return;
                case "scroll": cb.done(scroll(s, "up".equalsIgnoreCase(text))); return;
                case "back": s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK); cb.done(ok("Pressed Back")); return;
                case "home": s.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME); cb.done(ok("Pressed Home")); return;
                default: cb.done(err("Unknown screen action: " + what));
            }
        } catch (RuntimeException e) {
            cb.done(err("Screen action failed: " + e.getMessage()));
        }
    }

    /** The top app window that is not SHARP itself (the popup sits on top of it). */
    private static AccessibilityNodeInfo appRoot(EnzoAccessibilityService s) {
        for (AccessibilityWindowInfo w : s.getWindows()) {               // top to bottom
            if (w.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
            AccessibilityNodeInfo r = w.getRoot();
            if (r == null || r.getPackageName() == null) continue;
            if (s.getPackageName().contentEquals(r.getPackageName())) continue;
            return r;
        }
        return null;
    }

    private static String label(AccessibilityNodeInfo n) {
        CharSequence t = n.getText();
        if (t == null || t.length() == 0) t = n.getContentDescription();
        if ((t == null || t.length() == 0) && Build.VERSION.SDK_INT >= 26) t = n.getHintText();
        return t == null ? "" : t.toString().replaceAll("\\s+", " ").trim();
    }

    private static void collect(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 40 || items.size() >= MAX_ITEMS || !n.isVisibleToUser()) return;
        boolean useful = n.isEditable() || ((n.isClickable() || n.isLongClickable()) && !label(n).isEmpty())
            || (n.getChildCount() == 0 && !label(n).isEmpty());
        if (useful) items.add(n);
        for (int i = 0; i < n.getChildCount(); i++) collect(n.getChild(i), depth + 1);
    }

    /** circled / path: what he circled (screen pixels): those items are marked, and his ring is drawn on the screenshot. */
    private static void read(EnzoAccessibilityService s, Executor main, Done cb, Path path, Rect circled) {
        AccessibilityNodeInfo root = appRoot(s);
        items.clear();
        collect(root, 0);
        JSObject out = new JSObject();
        out.put("app", root == null || root.getPackageName() == null ? "" : appName(s, root.getPackageName().toString()));
        JSArray list = new JSArray();
        for (int i = 0; i < items.size(); i++) {
            AccessibilityNodeInfo n = items.get(i);
            JSObject o = new JSObject();
            o.put("n", i + 1);
            o.put("kind", n.isEditable() ? "field" : n.isClickable() ? "button" : "text");
            String l = label(n);
            o.put("text", l.length() > 200 ? l.substring(0, 200) : l);
            if (circled != null) {
                Rect b = new Rect();
                n.getBoundsInScreen(b);
                if (circled.contains(b.centerX(), b.centerY()) || b.contains(circled)) o.put("circled", true);
            }
            list.put(o);
        }
        out.put("items", list);
        if (Build.VERSION.SDK_INT < 30) { cb.done(out); return; }
        s.takeScreenshot(Display.DEFAULT_DISPLAY, main, new AccessibilityService.TakeScreenshotCallback() {
            @Override public void onSuccess(AccessibilityService.ScreenshotResult r) {
                try {
                    Bitmap hw = Bitmap.wrapHardwareBuffer(r.getHardwareBuffer(), r.getColorSpace());
                    if (hw != null) out.put("image", jpeg(hw, path));
                } catch (RuntimeException ignored) { }
                r.getHardwareBuffer().close();
                cb.done(out);
            }
            @Override public void onFailure(int code) { cb.done(out); }   // the list alone still helps
        });
    }

    /** Small JPEG (720 px wide) as base64: enough to read the screen, cheap to send to Claude. */
    private static String jpeg(Bitmap hw, Path path) {
        Bitmap soft = hw.copy(Bitmap.Config.ARGB_8888, path != null);
        if (path != null) {                                                 // his circle, so Claude sees what he meant
            android.graphics.Paint ink = new android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG);
            ink.setStyle(android.graphics.Paint.Style.STROKE);
            ink.setStrokeWidth(soft.getWidth() / 90f);
            ink.setColor(android.graphics.Color.rgb(255, 40, 40));
            new android.graphics.Canvas(soft).drawPath(path, ink);
        }
        int w = Math.min(720, soft.getWidth());
        Bitmap small = Bitmap.createScaledBitmap(soft, w, Math.round(soft.getHeight() * (w / (float) soft.getWidth())), true);
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        small.compress(Bitmap.CompressFormat.JPEG, 60, buf);
        return Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP);
    }

    private static String appName(EnzoAccessibilityService s, String pkg) {
        try {
            return s.getPackageManager().getApplicationLabel(s.getPackageManager().getApplicationInfo(pkg, 0)) + " (" + pkg + ")";
        } catch (Exception e) {
            return pkg;
        }
    }

    private static AccessibilityNodeInfo item(int n) {
        if (n < 1 || n > items.size()) return null;
        AccessibilityNodeInfo node = items.get(n - 1);
        return node.refresh() ? node : null;                              // false: it is gone from the screen
    }

    private static JSObject tap(EnzoAccessibilityService s, int n) {
        AccessibilityNodeInfo node = item(n);
        if (node == null) return err("Item " + n + " is not on the screen any more. Read the screen again.");
        for (AccessibilityNodeInfo p = node; p != null; p = p.getParent()) {
            if (p.isClickable() && p.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return ok("Tapped \"" + label(node) + "\"");
        }
        Rect b = new Rect();                                               // nothing clickable: tap its middle like a finger
        node.getBoundsInScreen(b);
        Path path = new Path();
        path.moveTo(b.exactCenterX(), b.exactCenterY());
        boolean sent = s.dispatchGesture(new GestureDescription.Builder()
            .addStroke(new GestureDescription.StrokeDescription(path, 0, 60)).build(), null, null);
        return sent ? ok("Tapped \"" + label(node) + "\"") : err("Could not tap item " + n);
    }

    /** Enzo shows him something: a ring around item n with a short label ("tap here"). */
    private static JSObject point(EnzoAccessibilityService s, int n, String label) {
        AccessibilityNodeInfo node = item(n);
        if (node == null) return err("Item " + n + " is not on the screen any more. Read the screen again.");
        Rect b = new Rect();
        node.getBoundsInScreen(b);
        EnzoOverlay.point(s, b, label.length() > 40 ? label.substring(0, 40) : label);
        return ok("Showing him \"" + label(node) + "\" with a ring");
    }

    /** Open an app by its name ("Moovit", "WhatsApp"): exact name first, then the first name that contains it. */
    private static JSObject open(EnzoAccessibilityService s, String name) {
        String want = name.trim().toLowerCase(java.util.Locale.ROOT);
        if (want.isEmpty()) return err("Say which app to open.");
        android.content.pm.PackageManager pm = s.getPackageManager();
        android.content.Intent main = new android.content.Intent(android.content.Intent.ACTION_MAIN).addCategory(android.content.Intent.CATEGORY_LAUNCHER);
        android.content.pm.ResolveInfo best = null;
        String bestLabel = "";
        for (android.content.pm.ResolveInfo r : pm.queryIntentActivities(main, 0)) {
            String l = String.valueOf(r.loadLabel(pm));
            String low = l.toLowerCase(java.util.Locale.ROOT);
            if (low.equals(want)) { best = r; bestLabel = l; break; }
            if (best == null && low.contains(want)) { best = r; bestLabel = l; }
        }
        if (best == null) return err("No app called \"" + name + "\" on the phone.");
        android.content.Intent go = pm.getLaunchIntentForPackage(best.activityInfo.packageName);
        if (go == null) return err("\"" + bestLabel + "\" cannot be opened.");
        go.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
        s.startActivity(go);
        return ok("Opened " + bestLabel);
    }

    private static JSObject type(EnzoAccessibilityService s, int n, String text) {
        AccessibilityNodeInfo node = item(n);
        if (node == null) return err("Item " + n + " is not on the screen any more. Read the screen again.");
        if (!node.isEditable()) return err("Item " + n + " is not a text field.");
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args) ? ok("Typed it") : err("The field did not take the text");
    }

    private static AccessibilityNodeInfo scrollable(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 40) return null;
        if (n.isScrollable() && n.isVisibleToUser()) return n;
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo f = scrollable(n.getChild(i), depth + 1);
            if (f != null) return f;
        }
        return null;
    }

    private static JSObject scroll(EnzoAccessibilityService s, boolean up) {
        AccessibilityNodeInfo node = scrollable(appRoot(s), 0);
        if (node == null) return err("Nothing on this screen scrolls.");
        boolean done = node.performAction(up ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD : AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);
        return done ? ok(up ? "Scrolled up" : "Scrolled down") : err("It did not scroll (already at the end?)");
    }

    private static JSObject ok(String what) {
        JSObject o = new JSObject();
        o.put("result", what);
        return o;
    }

    private static JSObject err(String why) {
        JSObject o = new JSObject();
        o.put("error", why);
        return o;
    }
}
