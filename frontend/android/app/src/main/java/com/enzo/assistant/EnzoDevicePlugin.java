package com.enzo.assistant;

import android.Manifest;
import android.app.SearchManager;
import android.content.ActivityNotFoundException;
import android.content.ComponentName;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import androidx.core.app.NotificationManagerCompat;
import java.util.List;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.hardware.camera2.CameraManager;
import android.media.AudioManager;
import android.net.Uri;
import android.provider.ContactsContract;
import android.provider.MediaStore;
import android.provider.Settings;
import android.view.KeyEvent;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import androidx.activity.result.ActivityResult;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/** Phone controls SHARP can use: calls, WhatsApp / SMS drafts, volume, brightness, flashlight, ringer. */
@CapacitorPlugin(
    name = "EnzoDevice",
    permissions = {
        @Permission(alias = "call", strings = { Manifest.permission.CALL_PHONE }),
        @Permission(alias = "contacts", strings = { Manifest.permission.READ_CONTACTS }),
        @Permission(alias = "notifications", strings = { "android.permission.POST_NOTIFICATIONS" })
    }
)
public class EnzoDevicePlugin extends Plugin {

    // ── Assistant shortcut: tell the web app to start listening (kept until it subscribes, for a cold start) ──

    @Override
    public void load() {
        assistIfAsked(getActivity().getIntent());
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        assistIfAsked(intent);
    }

    /** True when this page runs in the small popup (PopupActivity), not the full app. */
    @PluginMethod
    public void isPopup(PluginCall call) {
        JSObject r = new JSObject();
        r.put("popup", getActivity() instanceof PopupActivity);
        call.resolve(r);
    }

    @PluginMethod
    public void closePopup(PluginCall call) {
        if (getActivity() instanceof PopupActivity) { PopupActivity p = (PopupActivity) getActivity(); p.runOnUiThread(p::closeToHole); }
        call.resolve();
    }

    /** Popup only: {expanded} grows the pill at the camera into the answer panel, or shrinks it back. */
    @PluginMethod
    public void popupSize(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("expanded", false));
        if (getActivity() instanceof PopupActivity) {
            PopupActivity a = (PopupActivity) getActivity();
            a.runOnUiThread(() -> a.setExpanded(on));
        }
        call.resolve();
    }

    /** Read or use the phone screen (EnzoScreen): {do: read|tap|type|scroll|back|home, target, text}. */
    @PluginMethod
    public void screen(PluginCall call) {
        java.util.concurrent.Executor main = getContext().getMainExecutor();
        main.execute(() -> EnzoScreen.run(call.getString("do", "read"), call.getInt("target", 0), call.getString("text", ""),
            main, call::resolve));
    }

    @PluginMethod
    public void accessibilityOn(PluginCall call) {
        JSObject r = new JSObject();
        r.put("on", EnzoAccessibilityService.get() != null);
        call.resolve(r);
    }

    private void assistIfAsked(Intent intent) {
        if (intent == null || !Intent.ACTION_ASSIST.equals(intent.getAction())) return;
        intent.setAction(Intent.ACTION_MAIN);  // fire once, not again when the activity is recreated
        notifyListeners("assist", new JSObject(), true);
    }

    private AudioManager audio() {
        return (AudioManager) getContext().getSystemService(Context.AUDIO_SERVICE);
    }

    private int streamFor(String name) {
        if ("ring".equals(name)) return AudioManager.STREAM_RING;
        if ("alarm".equals(name)) return AudioManager.STREAM_ALARM;
        if ("call".equals(name)) return AudioManager.STREAM_VOICE_CALL;
        return AudioManager.STREAM_MUSIC;
    }

    // ── Contacts (read-only: name + number of everyone, JS does the matching) ──

    @PluginMethod
    public void contacts(PluginCall call) {
        if (getPermissionState("contacts") != PermissionState.GRANTED) {
            requestPermissionForAlias("contacts", call, "contactsPermResult");
            return;
        }
        loadContacts(call);
    }

    @PermissionCallback
    private void contactsPermResult(PluginCall call) {
        if (getPermissionState("contacts") == PermissionState.GRANTED) loadContacts(call);
        else call.reject("permission");
    }

    private void loadContacts(PluginCall call) {
        JSArray list = new JSArray();
        String[] cols = {
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER
        };
        try (Cursor c = getContext().getContentResolver().query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI, cols, null, null, null)) {
            if (c != null) {
                while (c.moveToNext()) {
                    JSObject o = new JSObject();
                    o.put("name", c.getString(0));
                    o.put("number", c.getString(1));
                    list.put(o);
                }
            }
        } catch (Exception e) {
            call.reject("Could not read contacts: " + e.getMessage());
            return;
        }
        JSObject r = new JSObject();
        r.put("contacts", list);
        call.resolve(r);
    }

    // ── Calls ────────────────────────────────────────────────

    @PluginMethod
    public void call(PluginCall call) {
        if (getPermissionState("call") != PermissionState.GRANTED) {
            requestPermissionForAlias("call", call, "callPermResult");
            return;
        }
        startCall(call, Intent.ACTION_CALL);
    }

    @PermissionCallback
    private void callPermResult(PluginCall call) {
        // Without the permission fall back to the dialer (the user presses the green button).
        startCall(call, getPermissionState("call") == PermissionState.GRANTED ? Intent.ACTION_CALL : Intent.ACTION_DIAL);
    }

    private void startCall(PluginCall call, String action) {
        String number = call.getString("number", "");
        if (number.isEmpty()) {
            call.reject("No number");
            return;
        }
        try {
            Intent i = new Intent(action, Uri.parse("tel:" + Uri.encode(number)));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            JSObject r = new JSObject();
            r.put("direct", Intent.ACTION_CALL.equals(action));
            call.resolve(r);
        } catch (Exception e) {
            call.reject("Could not start the call: " + e.getMessage());
        }
    }

    // ── Messages (opens the chat with the text ready; the user presses send) ──

    @PluginMethod
    public void whatsapp(PluginCall call) {
        String digits = call.getString("number", "").replaceAll("[^0-9]", "");
        String text = call.getString("text", "");
        if (digits.isEmpty()) {
            call.reject("No number");
            return;
        }
        Uri uri = Uri.parse("https://wa.me/" + digits + "?text=" + Uri.encode(text));
        boolean wantSend = call.getBoolean("send", false);
        boolean canSend = EnzoAccessibilityService.isEnabled(getContext());
        if (wantSend && canSend) EnzoAccessibilityService.armSend();
        String[] pkgs = { "com.whatsapp", "com.whatsapp.w4b", null };
        for (String pkg : pkgs) {
            try {
                Intent i = new Intent(Intent.ACTION_VIEW, uri);
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (pkg != null) i.setPackage(pkg);
                getContext().startActivity(i);
                JSObject res = new JSObject();
                res.put("autoSend", wantSend && canSend);
                res.put("needsAccessibility", wantSend && !canSend);
                call.resolve(res);
                return;
            } catch (ActivityNotFoundException ignored) {
                // try the next one
            }
        }
        call.reject("WhatsApp is not installed");
    }

    @PluginMethod
    public void sms(PluginCall call) {
        String number = call.getString("number", "");
        try {
            Intent i = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(number)));
            i.putExtra("sms_body", call.getString("text", ""));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open messages: " + e.getMessage());
        }
    }

    // ── Accessibility (lets SHARP press WhatsApp's send button) ──

    @PluginMethod
    public void accessibilityStatus(PluginCall call) {
        JSObject r = new JSObject();
        r.put("enabled", EnzoAccessibilityService.isEnabled(getContext()));
        call.resolve(r);
    }

    @PluginMethod
    public void openAccessibilitySettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open settings: " + e.getMessage());
        }
    }

    @PluginMethod
    public void openAppSettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getContext().getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open settings: " + e.getMessage());
        }
    }

    // ── Music: Spotify search-and-play, and play/pause/next for whatever player is active ──

    private boolean listenerEnabled() {
        return NotificationManagerCompat.getEnabledListenerPackages(getContext()).contains(getContext().getPackageName());
    }

    /** Spotify's running media session, or null (needs notification access to be switched on). */
    private MediaController spotifySession() {
        try {
            MediaSessionManager msm = (MediaSessionManager) getContext().getSystemService(Context.MEDIA_SESSION_SERVICE);
            List<MediaController> list = msm.getActiveSessions(new ComponentName(getContext(), EnzoNotificationListener.class));
            for (MediaController c : list) {
                if ("com.spotify.music".equals(c.getPackageName())) return c;
            }
        } catch (Exception ignored) {
            // no notification access yet
        }
        return null;
    }

    private boolean playViaSession(String query) {
        MediaController c = spotifySession();
        if (c == null) return false;
        Bundle extras = new Bundle();
        extras.putString(MediaStore.EXTRA_MEDIA_FOCUS, "vnd.android.cursor.item/*");
        extras.putString(SearchManager.QUERY, query);
        c.getTransportControls().playFromSearch(query, extras);
        return true;
    }

    /** Ask for the permission to show reminders (Android 13+). Resolves whatever the answer is. */
    @PluginMethod
    public void requestNotifications(PluginCall call) {
        if (android.os.Build.VERSION.SDK_INT < 33 || getPermissionState("notifications") == PermissionState.GRANTED) {
            notificationsDone(call);
            return;
        }
        requestPermissionForAlias("notifications", call, "notificationsDone");
    }

    @PermissionCallback
    private void notificationsDone(PluginCall call) {
        JSObject r = new JSObject();
        r.put("granted", androidx.core.app.NotificationManagerCompat.from(getContext()).areNotificationsEnabled());
        call.resolve(r);
    }

    /** How many reminders are waiting on the phone and when the next one rings. */
    @PluginMethod
    public void reminderStatus(PluginCall call) {
        long[] st = EnzoReminders.status(getContext());
        JSObject r = new JSObject();
        r.put("waiting", st[0]);
        r.put("next", st[1]);
        call.resolve(r);
    }

    /** A timer in the phone's own Clock app (no screen needed). */
    @PluginMethod
    public void setTimer(PluginCall call) {
        int seconds = call.getInt("seconds", 0);
        if (seconds <= 0) {
            call.reject("No time given");
            return;
        }
        Intent i = new Intent(android.provider.AlarmClock.ACTION_SET_TIMER)
            .putExtra(android.provider.AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(android.provider.AlarmClock.EXTRA_MESSAGE, call.getString("label", ""))
            .putExtra(android.provider.AlarmClock.EXTRA_SKIP_UI, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        launchClock(call, i);
    }

    /** An alarm in the phone's own Clock app, one-time or repeating. */
    @PluginMethod
    public void setAlarm(PluginCall call) {
        Intent i = new Intent(android.provider.AlarmClock.ACTION_SET_ALARM)
            .putExtra(android.provider.AlarmClock.EXTRA_HOUR, call.getInt("hour", 7))
            .putExtra(android.provider.AlarmClock.EXTRA_MINUTES, call.getInt("minute", 0))
            .putExtra(android.provider.AlarmClock.EXTRA_MESSAGE, call.getString("label", ""))
            .putExtra(android.provider.AlarmClock.EXTRA_SKIP_UI, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        JSArray days = call.getArray("days", new JSArray());
        java.util.ArrayList<Integer> list = new java.util.ArrayList<>();
        for (int k = 0; k < days.length(); k++) list.add(days.optInt(k));
        if (!list.isEmpty()) i.putExtra(android.provider.AlarmClock.EXTRA_DAYS, list);
        launchClock(call, i);
    }

    private void launchClock(PluginCall call, Intent i) {
        try {
            getContext().startActivity(i);
            // Some clock apps open for a moment: go back to SHARP right after.
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                Intent back = getContext().getPackageManager().getLaunchIntentForPackage(getContext().getPackageName());
                if (back != null) {
                    back.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                    try { getContext().startActivity(back); } catch (Exception ignored) { }
                }
            }, 1500);
            call.resolve();
        } catch (Exception e) {
            call.reject("The phone's Clock app did not accept that: " + e.getMessage());
        }
    }

    /** The phone's own contact picker: the user chooses one person; we return their name and number. */
    @PluginMethod
    public void pickContact(PluginCall call) {
        final String title = call.getString("title", "");
        if (!title.isEmpty()) {
            // The contact list covers the app: say on screen who to pick (stays visible for a few seconds on top of it).
            new Handler(Looper.getMainLooper()).post(() -> {
                android.widget.Toast.makeText(getContext(), title, android.widget.Toast.LENGTH_LONG).show();
            });
        }
        Intent i = new Intent(Intent.ACTION_PICK, ContactsContract.CommonDataKinds.Phone.CONTENT_URI);
        startActivityForResult(call, i, "pickContactResult");
    }

    @ActivityCallback
    private void pickContactResult(PluginCall call, ActivityResult result) {
        if (call == null) return;
        JSObject r = new JSObject();
        r.put("cancelled", true);
        if (result.getResultCode() == android.app.Activity.RESULT_OK && result.getData() != null && result.getData().getData() != null) {
            Uri uri = result.getData().getData();
            try (Cursor c = getContext().getContentResolver().query(uri,
                    new String[] { ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER },
                    null, null, null)) {
                if (c != null && c.moveToFirst()) {
                    r.put("cancelled", false);
                    r.put("name", c.getString(0));
                    r.put("number", c.getString(1));
                }
            } catch (Exception ignored) {
                // leave cancelled = true
            }
        }
        call.resolve(r);
    }

    /** This phone's push address (Firebase). The app sends it to the server, which pushes notifications to it. */
    @PluginMethod
    public void pushToken(PluginCall call) {
        try {
            com.google.firebase.messaging.FirebaseMessaging.getInstance().getToken().addOnCompleteListener(t -> {
                JSObject r = new JSObject();
                r.put("token", t.isSuccessful() && t.getResult() != null ? t.getResult() : "");
                call.resolve(r);
            });
        } catch (RuntimeException e) {          // no google-services.json in this build
            JSObject r = new JSObject();
            r.put("token", "");
            call.resolve(r);
        }
    }

    /** Shows a notification now (the server pushes this when a reminder fires). Same id = replaces, no duplicates. */
    @PluginMethod
    public void notifyNow(PluginCall call) {
        JSObject r = new JSObject();
        r.put("posted", EnzoReminders.post(getContext(), call.getString("id", "sharp"), call.getString("text", ""), call.getBoolean("alarm", false)));
        call.resolve(r);
    }

    /** Posts a notification right now, to check that notifications work. */
    @PluginMethod
    public void testNotification(PluginCall call) {
        JSObject r = new JSObject();
        r.put("posted", EnzoReminders.post(getContext(), "test" + System.currentTimeMillis(), call.getString("text", "This is a test from Enzo."), false));
        long[] st = EnzoReminders.status(getContext());
        r.put("waiting", st[0]);
        r.put("next", st[1]);
        call.resolve(r);
    }

    /** Server hands over the reminder list; Android's alarm clock fires them (works with the app closed). */
    @PluginMethod
    public void syncReminders(PluginCall call) {
        try {
            EnzoReminders.sync(getContext(), call.getArray("items", new JSArray()));
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not set reminders: " + e.getMessage());
        }
    }

    /** The notifications SHARP has seen since notification access was switched on. */
    @PluginMethod
    public void notifications(PluginCall call) {
        JSObject r = new JSObject();
        r.put("enabled", listenerEnabled());
        JSArray arr = new JSArray();
        for (EnzoNotificationListener.Item it : EnzoNotificationListener.recent()) {
            JSObject o = new JSObject();
            o.put("app", it.app);
            o.put("pkg", it.pkg);
            o.put("title", it.title);
            o.put("text", it.text);
            o.put("time", it.time);
            arr.put(o);
        }
        r.put("items", arr);
        call.resolve(r);
    }

    @PluginMethod
    public void clearNotifications(PluginCall call) {
        EnzoNotificationListener.clear();
        call.resolve();
    }

    @PluginMethod
    public void openNotificationAccessSettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open settings: " + e.getMessage());
        }
    }

    @PluginMethod
    public void playMusic(PluginCall call) {
        final String query = call.getString("query", "");
        if (query.isEmpty()) {
            call.reject("Nothing to play");
            return;
        }
        boolean auto = EnzoAccessibilityService.isEnabled(getContext());
        if (auto) EnzoAccessibilityService.armSpotify(query);   // taps the top result, then Play
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse("spotify:search:" + Uri.encode(query)));
            i.setPackage("com.spotify.music");
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            JSObject r = new JSObject();
            r.put("auto", auto);
            call.resolve(r);
        } catch (ActivityNotFoundException e) {
            call.reject("Spotify is not installed");
        }
    }

    @PluginMethod
    public void openUrl(PluginCall call) {
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(call.getString("url", "")));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            // fresh: restart the app's screens; Moovit ignores a new directions link while its trip list is already open
            if (call.getBoolean("fresh", false)) i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not open the link: " + e.getMessage());
        }
    }

    /** Wakes the Spotify app so it shows up as a playback device (SHARP then plays on it from the server).
     *  Default: in the BACKGROUND (Spotify's media session, or a media-button broadcast that starts its player).
     *  ui=true opens Spotify's screen: the last resort, because Android 15+ does not let SHARP come back to the front. */
    @PluginMethod
    public void openSpotify(PluginCall call) {
        if (!call.getBoolean("ui", false)) {
            JSObject r = new JSObject();
            MediaController c = spotifySession();
            if (c != null) {
                c.getTransportControls().play();
                r.put("woke", "session");
            } else {
                for (int action : new int[]{KeyEvent.ACTION_DOWN, KeyEvent.ACTION_UP}) {
                    Intent mb = new Intent(Intent.ACTION_MEDIA_BUTTON).setPackage("com.spotify.music");
                    mb.putExtra(Intent.EXTRA_KEY_EVENT, new KeyEvent(action, KeyEvent.KEYCODE_MEDIA_PLAY));
                    getContext().sendBroadcast(mb);
                }
                r.put("woke", "broadcast");
            }
            call.resolve(r);
            return;
        }
        Intent i = getContext().getPackageManager().getLaunchIntentForPackage("com.spotify.music");
        if (i == null) {
            call.reject("Spotify is not installed");
            return;
        }
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(i);
        // Spotify only needs a moment to register as a playback device; then go straight back to SHARP.
        new Handler(Looper.getMainLooper()).postDelayed(() -> {
            Intent back = getContext().getPackageManager().getLaunchIntentForPackage(getContext().getPackageName());
            if (back != null) {
                back.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                try { getContext().startActivity(back); } catch (Exception ignored) { }
            }
        }, 2500);
        call.resolve();
    }

    /** Installed app build number (CI sets it), so the app can tell when a newer APK exists. */
    @PluginMethod
    public void appVersion(PluginCall call) {
        JSObject r = new JSObject();
        try {
            android.content.pm.PackageInfo pi = getContext().getPackageManager().getPackageInfo(getContext().getPackageName(), 0);
            r.put("code", android.os.Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode);
        } catch (Exception e) {
            r.put("code", 0);
        }
        r.put("canInstall", android.os.Build.VERSION.SDK_INT < 26 || getContext().getPackageManager().canRequestPackageInstalls());
        call.resolve(r);
    }

    /** Opens "Install unknown apps" for SHARP (one-time permission for self-updates). */
    @PluginMethod
    public void openInstallPermission(PluginCall call) {
        Intent i = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getContext().getPackageName()));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(i);
        call.resolve();
    }

    /** Downloads the new APK from SHARP's server and opens Android's installer (one tap on Update). */
    @PluginMethod
    public void installApk(PluginCall call) {
        final String url = call.getString("url", "");
        new Thread(() -> {
            try {
                java.io.File dir = new java.io.File(getContext().getCacheDir(), "updates");
                dir.mkdirs();
                java.io.File f = new java.io.File(dir, "sharp.apk");
                java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(60000);
                try (java.io.InputStream in = c.getInputStream(); java.io.FileOutputStream out = new java.io.FileOutputStream(f)) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                }
                Uri uri = androidx.core.content.FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", f);
                Intent i = new Intent(Intent.ACTION_VIEW);
                i.setDataAndType(uri, "application/vnd.android.package-archive");
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                getContext().startActivity(i);
                call.resolve();
            } catch (Exception e) {
                call.reject("Update failed: " + e.getMessage());
            }
        }).start();
    }

    /** Arm the Moovit taps (trip with this first line, then Start); JS opens the moovit:// link right after. */
    @PluginMethod
    public void armMoovit(PluginCall call) {
        boolean auto = EnzoAccessibilityService.isEnabled(getContext());
        if (auto) EnzoAccessibilityService.armMoovit(call.getString("line", ""));
        JSObject r = new JSObject();
        r.put("auto", auto);
        call.resolve(r);
    }

    @PluginMethod
    public void moovitReport(PluginCall call) {
        JSObject r = new JSObject();
        r.put("report", EnzoAccessibilityService.moovitFinishReport());
        call.resolve(r);
    }

    /** What the Spotify automation did (or saw), to show in the chat. */
    @PluginMethod
    public void spotifyReport(PluginCall call) {
        JSObject r = new JSObject();
        r.put("report", EnzoAccessibilityService.finishReport());
        call.resolve(r);
    }

    @PluginMethod
    public void mediaKey(PluginCall call) {
        String key = call.getString("key", "playpause");
        int code = KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE;
        if ("play".equals(key)) code = KeyEvent.KEYCODE_MEDIA_PLAY;
        else if ("pause".equals(key)) code = KeyEvent.KEYCODE_MEDIA_PAUSE;
        else if ("next".equals(key)) code = KeyEvent.KEYCODE_MEDIA_NEXT;
        else if ("previous".equals(key)) code = KeyEvent.KEYCODE_MEDIA_PREVIOUS;
        else if ("stop".equals(key)) code = KeyEvent.KEYCODE_MEDIA_STOP;
        try {
            AudioManager am = audio();
            am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, code));
            am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, code));
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not control the music: " + e.getMessage());
        }
    }

    /** The WebView can leave the phone in call mode after its microphone closes (music sounds like a call): end it. */
    @PluginMethod
    public void normalAudio(PluginCall call) {
        try {
            AudioManager am = audio();
            if (android.os.Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice();
            am.setMode(AudioManager.MODE_NORMAL);
            JSObject r = new JSObject();
            r.put("mode", am.getMode());
            call.resolve(r);
        } catch (Exception e) {
            call.reject("Could not reset the audio mode: " + e.getMessage());
        }
    }

    // ── Volume ───────────────────────────────────────────────

    @PluginMethod
    public void setVolume(PluginCall call) {
        try {
            AudioManager am = audio();
            int stream = streamFor(call.getString("stream", "music"));
            int max = am.getStreamMaxVolume(stream);
            String dir = call.getString("direction", "");
            if ("up".equals(dir)) {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_RAISE, AudioManager.FLAG_SHOW_UI);
            } else if ("down".equals(dir)) {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_LOWER, AudioManager.FLAG_SHOW_UI);
            } else {
                int level = call.getInt("level", 50);
                int clamped = Math.max(0, Math.min(100, level));
                am.setStreamVolume(stream, Math.round(max * clamped / 100f), AudioManager.FLAG_SHOW_UI);
            }
            JSObject r = new JSObject();
            r.put("percent", Math.round(100f * am.getStreamVolume(stream) / max));
            call.resolve(r);
        } catch (Exception e) {
            call.reject("Could not change the volume: " + e.getMessage());
        }
    }

    /** normal / vibrate (silent needs Do Not Disturb access, which the user may not have given). */
    @PluginMethod
    public void setRinger(PluginCall call) {
        try {
            String mode = call.getString("mode", "normal");
            int m = AudioManager.RINGER_MODE_NORMAL;
            if ("silent".equals(mode)) m = AudioManager.RINGER_MODE_SILENT;
            else if ("vibrate".equals(mode)) m = AudioManager.RINGER_MODE_VIBRATE;
            audio().setRingerMode(m);
            call.resolve();
        } catch (SecurityException e) {
            call.reject("Android needs Do Not Disturb access for that mode");
        } catch (Exception e) {
            call.reject("Could not change the ringer: " + e.getMessage());
        }
    }

    // ── Brightness (needs the one-time "modify system settings" permission) ──

    @PluginMethod
    public void setBrightness(PluginCall call) {
        Context ctx = getContext();
        if (!Settings.System.canWrite(ctx)) {
            try {
                Intent i = new Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, Uri.parse("package:" + ctx.getPackageName()));
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                ctx.startActivity(i);
            } catch (Exception ignored) {
                // nothing else to do
            }
            JSObject r = new JSObject();
            r.put("needsPermission", true);
            call.resolve(r);
            return;
        }
        try {
            ContentResolver cr = ctx.getContentResolver();
            int current = Settings.System.getInt(cr, Settings.System.SCREEN_BRIGHTNESS, 128);
            String dir = call.getString("direction", "");
            int value;
            if ("up".equals(dir)) {
                value = current + 51;
            } else if ("down".equals(dir)) {
                value = current - 51;
            } else {
                int level = Math.max(0, Math.min(100, call.getInt("level", 50)));
                value = Math.round(255 * level / 100f);
            }
            value = Math.max(5, Math.min(255, value));
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL);
            Settings.System.putInt(cr, Settings.System.SCREEN_BRIGHTNESS, value);
            JSObject r = new JSObject();
            r.put("percent", Math.round(100f * value / 255));
            call.resolve(r);
        } catch (Exception e) {
            call.reject("Could not change the brightness: " + e.getMessage());
        }
    }

    // ── Flashlight ───────────────────────────────────────────

    @PluginMethod
    public void setFlashlight(PluginCall call) {
        try {
            CameraManager cm = (CameraManager) getContext().getSystemService(Context.CAMERA_SERVICE);
            String[] ids = cm.getCameraIdList();
            if (ids.length == 0) {
                call.reject("No flashlight");
                return;
            }
            cm.setTorchMode(ids[0], call.getBoolean("on", true));
            call.resolve();
        } catch (Exception e) {
            call.reject("Could not switch the flashlight: " + e.getMessage());
        }
    }
}
