package com.enzo.assistant;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

/**
 * Enzo's notifications pushed through Firebase, like other apps: Android wakes this service even when the app is
 * asleep or closed, so a heads-up shows on time. Data-only messages (id, text, alarm, stale_after), shown with the
 * same notification as reminders: the same id replaces, never doubles.
 */
public class EnzoPush extends FirebaseMessagingService {

    @Override
    public void onMessageReceived(RemoteMessage m) {
        Map<String, String> d = m.getData();
        String text = d.get("text");
        if (text == null || text.isEmpty()) return;
        try {   // late (phone was off the network past the event): it would be wrong now
            String stale = d.get("stale_after");
            if (stale != null && !stale.isEmpty() && System.currentTimeMillis() > Long.parseLong(stale)) return;
        } catch (NumberFormatException ignored) { }
        String id = d.get("id");
        EnzoReminders.post(this, id == null || id.isEmpty() ? text : id, text, "true".equals(d.get("alarm")));
    }

    @Override
    public void onNewToken(String token) {
        // The app sends the current token whenever it connects (pushToken in EnzoDevicePlugin); nothing to keep here.
    }
}
