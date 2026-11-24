package com.enzo.assistant;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * Keeps the microphone allowed while the pill is open. The pill lives in the assistant's window and the popup Activity
 * steps behind the app he is using, so SHARP is no longer "on top"; Android then only lets it record from a
 * foreground service of type microphone, started while the popup was still on top (PopupActivity.moveInto).
 */
public class EnzoMicService extends Service {

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(new NotificationChannel("sharp_mic", "Enzo listening", NotificationManager.IMPORTANCE_LOW));
            }
            Notification n = (Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, "sharp_mic") : new Notification.Builder(this))
                .setSmallIcon(getApplicationInfo().icon)
                .setContentTitle("Enzo is listening")
                .setOngoing(true)
                .build();
            if (Build.VERSION.SDK_INT >= 29) startForeground(7, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            else startForeground(7, n);
        } catch (RuntimeException e) {
            stopSelf();   // not allowed: the popup still steps back, the mic may then be silent
        }
        PopupActivity live = PopupActivity.live;
        if (live != null) live.runOnUiThread(live::stepBack);
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
