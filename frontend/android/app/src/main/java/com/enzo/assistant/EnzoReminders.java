package com.enzo.assistant;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Reminders that live on the phone: SHARP's server hands over the list, Android's alarm clock fires them
 * even when the app is closed, the screen is off or the network is down.
 */
public class EnzoReminders {

    static final String CHANNEL = "sharp_reminders";
    private static final String PREFS = "sharp_reminders";
    private static final String KEY = "items";

    /** Replace everything scheduled with this list (items: id, text, at (ms), repeat (ms), alarm). */
    public static void sync(Context c, JSONArray items) {
        cancelAll(c);
        SharedPreferences.Editor ed = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit();
        ed.putString(KEY, items.toString());
        ed.apply();
        for (int i = 0; i < items.length(); i++) schedule(c, items.optJSONObject(i));
    }

    /** After a reboot Android forgets alarms: put them back. */
    public static void restore(Context c) {
        try {
            String s = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "[]");
            JSONArray items = new JSONArray(s);
            for (int i = 0; i < items.length(); i++) schedule(c, items.optJSONObject(i));
        } catch (Exception ignored) {
            // nothing saved
        }
    }

    private static PendingIntent pending(Context c, JSONObject it) {
        Intent i = new Intent(c, Receiver.class);
        i.putExtra("id", it.optString("id"));
        i.putExtra("text", it.optString("text"));
        i.putExtra("at", it.optLong("at"));
        i.putExtra("repeat", it.optLong("repeat"));
        i.putExtra("alarm", it.optBoolean("alarm"));
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(c, it.optString("id").hashCode(), i, flags);
    }

    static void schedule(Context c, JSONObject it) {
        if (it == null) return;
        long at = it.optLong("at");
        long repeat = it.optLong("repeat");
        long now = System.currentTimeMillis();
        if (at <= now) {
            if (repeat <= 0) return;                       // a one-time reminder that already passed
            at += ((now - at) / repeat + 1) * repeat;      // recurring: next slot
        }
        try {
            JSONObject next = new JSONObject(it.toString());
            next.put("at", at);
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            Intent open = c.getPackageManager().getLaunchIntentForPackage(c.getPackageName());
            PendingIntent show = PendingIntent.getActivity(c, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0));
            // setAlarmClock is exact and needs no special permission.
            am.setAlarmClock(new AlarmManager.AlarmClockInfo(at, show), pending(c, next));
        } catch (Exception ignored) {
            // could not schedule this one
        }
    }

    private static void cancelAll(Context c) {
        try {
            String s = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "[]");
            JSONArray items = new JSONArray(s);
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            for (int i = 0; i < items.length(); i++) {
                JSONObject it = items.optJSONObject(i);
                if (it != null) am.cancel(pending(c, it));
            }
        } catch (Exception ignored) {
            // nothing to cancel
        }
    }

    /** Shows the notification. Returns false when Android refuses (permission off). */
    public static boolean post(Context c, String id, String text, boolean alarm) {
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (!nm.areNotificationsEnabled()) return false;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Enzo reminders", NotificationManager.IMPORTANCE_HIGH);
            ch.enableVibration(true);
            nm.createNotificationChannel(ch);
        }
        Intent open = c.getPackageManager().getLaunchIntentForPackage(c.getPackageName());
        PendingIntent tap = PendingIntent.getActivity(c, 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0));
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setContentTitle(alarm ? "Enzo alarm" : "Enzo")
            .setContentText(text)
            .setStyle(new Notification.BigTextStyle().bigText(text))
            .setContentIntent(tap)
            .setAutoCancel(true);
        if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_HIGH).setDefaults(Notification.DEFAULT_ALL);
        try {
            nm.notify(id.hashCode(), b.build());   // same id as the alarm = one notification, not two
            return true;
        } catch (SecurityException e) {
            return false;
        }
    }

    /** How many reminders are waiting on the phone and when the next one rings (ms, 0 if none). */
    public static long[] status(Context c) {
        long count = 0, next = 0, now = System.currentTimeMillis();
        try {
            JSONArray items = new JSONArray(c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "[]"));
            for (int i = 0; i < items.length(); i++) {
                JSONObject it = items.optJSONObject(i);
                if (it == null) continue;
                long at = it.optLong("at"), repeat = it.optLong("repeat");
                if (at <= now && repeat > 0) at += ((now - at) / repeat + 1) * repeat;
                if (at > now) {
                    count++;
                    if (next == 0 || at < next) next = at;
                }
            }
        } catch (Exception ignored) {
            // none saved
        }
        return new long[] { count, next };
    }

    public static class Receiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent in) {
            String text = in.getStringExtra("text");
            boolean alarm = in.getBooleanExtra("alarm", false);
            post(c, in.getStringExtra("id"), text, alarm);
            long repeat = in.getLongExtra("repeat", 0);
            if (repeat > 0) {
                try {
                    JSONObject it = new JSONObject();
                    it.put("id", in.getStringExtra("id"));
                    it.put("text", text);
                    it.put("at", in.getLongExtra("at", 0) + repeat);
                    it.put("repeat", repeat);
                    it.put("alarm", alarm);
                    schedule(c, it);
                } catch (Exception ignored) {
                    // next slot will be restored on the next sync
                }
            }
        }
    }

    public static class Boot extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent in) {
            restore(c);
        }
    }
}
