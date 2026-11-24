package com.enzo.assistant;

import android.accessibilityservice.AccessibilityService;
import android.content.Context;
import android.content.Intent;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Two small jobs, each only right after SHARP asked for it and only inside that app
 * (see accessibility_service_config.xml):
 *  - WhatsApp: press "send" after SHARP opened a chat for you (armed 20 s).
 *  - Spotify: after SHARP opened a search, tap the top result and press Play (armed 25 s).
 *  - Moovit: after SHARP opened directions, tap the trip he chose and press Start (armed 30 s).
 * It stores nothing and does nothing at any other time.
 */
public class EnzoAccessibilityService extends AccessibilityService {

    private static volatile long armedUntil = 0;           // WhatsApp send
    private static volatile String spotifyQuery = null;    // Spotify play
    private static volatile long spotifyUntil = 0;
    private static int spotifyStep = 0;                    // 0 = find result, 1 = find Play, 2 = done
    private static long spotifyLastAction = 0;
    private static volatile String spotifyReport = "";

    private final Handler handler = new Handler(Looper.getMainLooper());

    /** Called by the plugin right before it opens the WhatsApp chat. */
    public static void armSend() {
        armedUntil = System.currentTimeMillis() + 20000;
        EnzoAccessibilityService s = instance;
        if (s != null) s.handler.postDelayed(s::pollWhatsapp, 800);   // don't wait for a screen change that may not come
    }

    /** Look for WhatsApp's send button every 0.6 s while armed. */
    private void pollWhatsapp() {
        if (System.currentTimeMillis() >= armedUntil) return;
        tryWhatsapp();
        if (System.currentTimeMillis() < armedUntil) handler.postDelayed(this::pollWhatsapp, 600);
    }

    /** WhatsApp's window even when Enzo's own window (the popup) is the active one. */
    private void tryWhatsapp() {
        try {
            for (AccessibilityWindowInfo w : getWindows()) {
                AccessibilityNodeInfo r = w.getRoot();
                if (r != null && r.getPackageName() != null && r.getPackageName().toString().startsWith("com.whatsapp")) {
                    handleWhatsapp(r, r.getPackageName().toString());
                    return;
                }
            }
        } catch (RuntimeException ignored) {
            // window list not available: the screen-change event path still runs
        }
    }

    /** Called by the plugin right after it opened Spotify's search. */
    public static synchronized void armSpotify(String query) {
        spotifyQuery = query;
        spotifyUntil = System.currentTimeMillis() + 25000;
        spotifyStep = 0;
        spotifyLastAction = 0;
        spotifyReport = "";
    }

    public static String report() {
        return spotifyReport;
    }

    /** True if the user has switched this service on in Android's accessibility settings. */
    public static boolean isEnabled(Context ctx) {
        String enabled = Settings.Secure.getString(ctx.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (TextUtils.isEmpty(enabled)) return false;
        String mine = ctx.getPackageName() + "/" + EnzoAccessibilityService.class.getName();
        for (String s : enabled.split(":")) {
            if (s.equalsIgnoreCase(mine) || s.equalsIgnoreCase(ctx.getPackageName() + "/.EnzoAccessibilityService")) return true;
        }
        return false;
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        long now = System.currentTimeMillis();
        boolean wa = now < armedUntil;
        boolean sp = spotifyQuery != null && now < spotifyUntil && spotifyStep < 2;
        if (!wa && !sp) return;
        if (wa) tryWhatsapp();
        if (!sp) return;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return;
        CharSequence pkgSeq = root.getPackageName();
        if (pkgSeq == null) return;
        String pkg = pkgSeq.toString();
        if (pkg.equals("com.spotify.music")) handleSpotify(root);
    }

    // ── WhatsApp ─────────────────────────────────────────────

    private void handleWhatsapp(AccessibilityNodeInfo root, String pkg) {
        AccessibilityNodeInfo send = findSend(root, pkg);
        if (send == null) return;
        AccessibilityNodeInfo target = clickableAncestor(send);
        if (target == null) return;
        if (target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            armedUntil = 0;
            handler.postDelayed(this::bringSharpBack, 1500);   // back to SHARP once the message has gone
        }
    }

    private AccessibilityNodeInfo findSend(AccessibilityNodeInfo root, String pkg) {
        List<AccessibilityNodeInfo> byId = root.findAccessibilityNodeInfosByViewId(pkg + ":id/send");
        if (byId != null && !byId.isEmpty()) return byId.get(0);
        for (String label : new String[] { "Send", "שלח" }) {
            List<AccessibilityNodeInfo> byText = root.findAccessibilityNodeInfosByText(label);
            if (byText == null) continue;
            for (AccessibilityNodeInfo n : byText) {
                CharSequence desc = n.getContentDescription();
                if (desc != null && desc.toString().equalsIgnoreCase(label)) return n;
            }
        }
        return null;
    }

    // ── Spotify ──────────────────────────────────────────────

    private static class Item {
        AccessibilityNodeInfo node;
        String label;    // text or content description
        String cls;
        int top;
        boolean editable;
    }

    private void collect(AccessibilityNodeInfo n, List<Item> out, int depth) {
        if (n == null || out.size() > 600 || depth > 40) return;
        CharSequence t = n.getText();
        CharSequence d = n.getContentDescription();
        String label = (t != null && t.length() > 0) ? t.toString() : (d != null ? d.toString() : "");
        if (!label.isEmpty()) {
            Item it = new Item();
            it.node = n;
            it.label = label;
            it.cls = n.getClassName() == null ? "" : n.getClassName().toString();
            Rect r = new Rect();
            n.getBoundsInScreen(r);
            it.top = r.top;
            it.editable = n.isEditable() || it.cls.contains("EditText");
            out.add(it);
        }
        for (int i = 0; i < n.getChildCount(); i++) collect(n.getChild(i), out, depth + 1);
    }

    private static boolean isPlayLabel(String s) {
        String l = s.toLowerCase(Locale.ROOT).trim();
        return l.equals("play") || l.equals("shuffle play") || l.equals("נגן") || l.equals("ניגון") || l.equals("ערבב ונגן");
    }

    private synchronized void handleSpotify(AccessibilityNodeInfo root) {
        long now = System.currentTimeMillis();
        if (now - spotifyLastAction < 1500) return;   // let the screen settle after each tap
        List<Item> items = new ArrayList<>();
        collect(root, items, 0);
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        int minTop = (int) (screen.height() * 0.16);   // below the search bar / header
        int maxTop = (int) (screen.height() * 0.80);   // above the player bar and tab bar

        if (spotifyStep == 0) {
            String[] words = spotifyQuery.toLowerCase(Locale.ROOT).trim().split("\\s+");
            Item best = null;
            for (Item it : items) {
                if (it.editable || it.top < minTop || it.top > maxTop) continue;
                String l = it.label.toLowerCase(Locale.ROOT);
                boolean all = true;
                for (String w : words) if (!l.contains(w)) { all = false; break; }
                if (all && (best == null || it.top < best.top)) best = it;
            }
            if (best != null) {
                AccessibilityNodeInfo target = clickableAncestor(best.node);
                if (target != null && target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    spotifyStep = 1;
                    spotifyLastAction = now;
                    spotifyReport = "Tapped the result \"" + best.label + "\". ";
                    spotifyUntil = now + 12000;   // time to find the Play button
                }
            }
            return;
        }

        if (spotifyStep == 1) {
            Item play = null;
            for (Item it : items) {
                if (!isPlayLabel(it.label) || it.top < minTop || it.top > maxTop) continue;   // not the bottom player bar
                if (play == null || it.top < play.top) play = it;
            }
            if (play != null) {
                AccessibilityNodeInfo target = clickableAncestor(play.node);
                if (target != null && target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    spotifyStep = 2;
                    spotifyLastAction = now;
                    spotifyReport += "Pressed \"" + play.label + "\". Done.";
                    handler.postDelayed(this::bringSharpBack, 2500);
                }
            }
        }
    }

    // ── Moovit ───────────────────────────────────────────────

    private static volatile String moovitLine = null;     // armed: the first line of the trip he chose ("" = the top trip)
    private static volatile long moovitUntil = 0;
    private static int moovitStep = 0;                    // 0 = find the trip, 1 = find Start, 2 = done
    private static long moovitLastAction = 0;
    private static volatile String moovitReport = "";

    /** Called by the plugin right before it opens Moovit's directions: tap the trip, then Start (armed 30 s). */
    public static synchronized void armMoovit(String line) {
        moovitLine = line == null ? "" : line.trim();
        moovitUntil = System.currentTimeMillis() + 30000;
        moovitStep = 0;
        moovitLastAction = 0;
        moovitReport = "";
        EnzoAccessibilityService s = instance;
        if (s != null) s.handler.postDelayed(s::pollMoovit, 1500);
    }

    private void pollMoovit() {
        if (moovitLine == null || moovitStep >= 2 || System.currentTimeMillis() > moovitUntil) return;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null && root.getPackageName() != null && root.getPackageName().toString().equals("com.tranzmate")) {
            handleMoovit(root);
        }
        handler.postDelayed(this::pollMoovit, 700);
    }

    /** All labels under a node, lower case (to tell a trip card from the taxi card). */
    private static String subtreeText(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 12) return "";
        StringBuilder sb = new StringBuilder();
        if (n.getText() != null) sb.append(n.getText()).append(' ');
        if (n.getContentDescription() != null) sb.append(n.getContentDescription()).append(' ');
        for (int i = 0; i < n.getChildCount(); i++) sb.append(subtreeText(n.getChild(i), depth + 1));
        return sb.toString().toLowerCase(Locale.ROOT);
    }

    private static boolean hasToken(String label, String token) {
        for (String w : label.split("[^\\p{L}\\p{N}]+")) if (w.equalsIgnoreCase(token)) return true;
        return false;
    }

    private synchronized void handleMoovit(AccessibilityNodeInfo root) {
        long now = System.currentTimeMillis();
        if (now - moovitLastAction < 1500) return;   // let the screen settle after each tap
        List<Item> items = new ArrayList<>();
        collect(root, items, 0);
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        int minTop = (int) (screen.height() * 0.15);   // below the from / to fields

        if (moovitStep == 0) {
            AccessibilityNodeInfo best = null;
            int bestTop = Integer.MAX_VALUE;
            for (Item it : items) {
                if (it.top < minTop || it.top >= bestTop) continue;
                if (!moovitLine.isEmpty() && !hasToken(it.label, moovitLine)) continue;
                AccessibilityNodeInfo card = clickableAncestor(it.node);
                String all = subtreeText(card, 0);
                if (card == null || !(all.contains("leaves") || all.contains("יוצא"))) continue;   // a trip, not the taxi offer
                best = card;
                bestTop = it.top;
            }
            if (best != null && best.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                moovitStep = 1;
                moovitLastAction = now;
                moovitReport = "Chose the trip" + (moovitLine.isEmpty() ? "" : " with line " + moovitLine) + ". ";
                moovitUntil = now + 12000;   // time to find Start
            }
            return;
        }
        for (Item it : items) {
            String l = it.label.trim().toLowerCase(Locale.ROOT);
            if (!l.equals("start") && !l.equals("התחל") && !l.equals("התחלה")) continue;
            AccessibilityNodeInfo t = clickableAncestor(it.node);
            if (t != null && t.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                moovitStep = 2;
                moovitReport += "Pressed Start. The trip is running in Moovit.";
                return;
            }
        }
    }

    /** What the Moovit automation did, or what it saw when it got stuck. */
    public static String moovitFinishReport() {
        EnzoAccessibilityService s = instance;
        if (moovitLine == null) return "";
        if (moovitStep == 2) return moovitReport;
        String prefix = moovitStep == 1 ? moovitReport + "Then I could not find Moovit's Start button."
            : "I could not find the trip" + (moovitLine.isEmpty() ? "" : " with line " + moovitLine) + " in Moovit's list.";
        return prefix + " Screen items:\n" + (s != null ? s.describeScreen() : "");
    }

    /** What the Spotify screen looks like to SHARP, for when it could not find the button. */
    private String describeScreen() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "(no screen)";
        List<Item> items = new ArrayList<>();
        collect(root, items, 0);
        StringBuilder sb = new StringBuilder();
        int n = 0;
        for (Item it : items) {
            if (n++ >= 30) break;
            String cls = it.cls.substring(it.cls.lastIndexOf('.') + 1);
            sb.append("- ").append(it.label.replace('\n', ' '), 0, Math.min(it.label.length(), 50))
              .append(" [").append(cls).append(" y=").append(it.top).append("]\n");
        }
        return sb.toString();
    }

    /** The plugin asks for this a while after a play request, to tell the user what happened. */
    public static String finishReport() {
        EnzoAccessibilityService s = instance;
        if (spotifyQuery == null) return "";
        if (spotifyStep == 2) return spotifyReport;
        String screen = s != null ? s.describeScreen() : "";
        String prefix = spotifyStep == 1
            ? spotifyReport + "Then I could not find a Play button."
            : "I could not find a result for \"" + spotifyQuery + "\" on Spotify's screen.";
        return prefix + " Screen items:\n" + screen;
    }

    private static volatile EnzoAccessibilityService instance;

    /** The running service, or null if he has not switched it on (EnzoScreen uses it to read and use the screen). */
    static EnzoAccessibilityService get() {
        return instance;
    }

    @Override
    protected void onServiceConnected() {
        instance = this;
    }

    @Override
    public boolean onUnbind(Intent intent) {
        instance = null;
        return super.onUnbind(intent);
    }

    // ── helpers ──────────────────────────────────────────────

    private static AccessibilityNodeInfo clickableAncestor(AccessibilityNodeInfo n) {
        AccessibilityNodeInfo t = n;
        while (t != null && !t.isClickable()) t = t.getParent();
        return t;
    }

    private void bringSharpBack() {
        Intent i = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (i == null) return;
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        try {
            startActivity(i);
        } catch (Exception ignored) {
            // staying in the other app is fine
        }
    }

    @Override
    public void onInterrupt() {
        // nothing to clean up
    }
}
