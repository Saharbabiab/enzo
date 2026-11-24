package com.enzo.assistant;

import android.app.Notification;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.util.ArrayList;
import java.util.List;

/**
 * Keeps the last notifications in memory (nothing leaves the phone until the user asks
 * "what did I miss"). Skips SHARP's own, ongoing ones (music, downloads) and group summaries.
 */
public class EnzoNotificationListener extends NotificationListenerService {

    public static class Item {
        public String app;
        public String pkg;
        public String title;
        public String text;
        public long time;
    }

    private static final int MAX = 60;
    private static final List<Item> RECENT = new ArrayList<>();

    public static synchronized List<Item> recent() {
        return new ArrayList<>(RECENT);
    }

    public static synchronized void clear() {
        RECENT.clear();
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        try {
            if (sbn == null || getPackageName().equals(sbn.getPackageName())) return;
            Notification n = sbn.getNotification();
            if (n == null) return;
            if ((n.flags & (Notification.FLAG_ONGOING_EVENT | Notification.FLAG_GROUP_SUMMARY)) != 0) return;
            CharSequence title = n.extras.getCharSequence(Notification.EXTRA_TITLE);
            CharSequence text = n.extras.getCharSequence(Notification.EXTRA_TEXT);
            if (title == null && text == null) return;

            Item it = new Item();
            it.pkg = sbn.getPackageName();
            it.title = title == null ? "" : title.toString();
            it.text = text == null ? "" : text.toString();
            it.time = sbn.getPostTime();
            try {
                PackageManager pm = getPackageManager();
                ApplicationInfo ai = pm.getApplicationInfo(it.pkg, 0);
                it.app = pm.getApplicationLabel(ai).toString();
            } catch (Exception e) {
                it.app = it.pkg;
            }
            synchronized (EnzoNotificationListener.class) {
                // same app + title + text posted again (updates) -> keep only the newest
                for (int i = RECENT.size() - 1; i >= 0; i--) {
                    Item o = RECENT.get(i);
                    if (o.pkg.equals(it.pkg) && o.title.equals(it.title) && o.text.equals(it.text)) RECENT.remove(i);
                }
                RECENT.add(it);
                while (RECENT.size() > MAX) RECENT.remove(0);
            }
        } catch (Exception ignored) {
            // never break the phone's notifications
        }
    }
}
