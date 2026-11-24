package com.enzo.assistant;

import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.os.Bundle;
import android.os.RemoteException;
import android.speech.RecognitionListener;
import android.speech.RecognitionService;
import android.speech.SpeechRecognizer;

import java.util.List;

/**
 * When SHARP is the default assistant, Android also makes this the phone's default speech recognizer (it must belong to
 * the assistant app). SHARP has no recognizer of its own, so this passes every request on to the one that was there
 * before (Google's when installed). Without it, voice typing in other apps and the app's backup recognizer would break.
 */
public class EnzoRecognitionProxy extends RecognitionService {

    private SpeechRecognizer inner;

    private ComponentName delegate() {
        List<ResolveInfo> all = getPackageManager().queryIntentServices(new Intent(RecognitionService.SERVICE_INTERFACE), 0);
        ComponentName pick = null;
        for (ResolveInfo r : all) {
            if (r.serviceInfo == null || getPackageName().equals(r.serviceInfo.packageName)) continue;
            ComponentName c = new ComponentName(r.serviceInfo.packageName, r.serviceInfo.name);
            if (r.serviceInfo.packageName.equals("com.google.android.googlequicksearchbox")) return c;
            if (pick == null) pick = c;
        }
        return pick;
    }

    private void close() {
        if (inner != null) { inner.destroy(); inner = null; }
    }

    @Override
    protected void onStartListening(Intent intent, Callback cb) {
        close();
        ComponentName target = delegate();
        if (target == null) { send(() -> cb.error(SpeechRecognizer.ERROR_CLIENT)); return; }
        inner = SpeechRecognizer.createSpeechRecognizer(this, target);
        inner.setRecognitionListener(new RecognitionListener() {
            @Override public void onReadyForSpeech(Bundle b) { send(() -> cb.readyForSpeech(b)); }
            @Override public void onBeginningOfSpeech() { send(cb::beginningOfSpeech); }
            @Override public void onRmsChanged(float db) { send(() -> cb.rmsChanged(db)); }
            @Override public void onBufferReceived(byte[] buf) { send(() -> cb.bufferReceived(buf)); }
            @Override public void onEndOfSpeech() { send(cb::endOfSpeech); }
            @Override public void onError(int err) { send(() -> cb.error(err)); close(); }
            @Override public void onResults(Bundle b) { send(() -> cb.results(b)); close(); }
            @Override public void onPartialResults(Bundle b) { send(() -> cb.partialResults(b)); }
            @Override public void onEvent(int type, Bundle b) { }
        });
        inner.startListening(intent);
    }

    @Override
    protected void onStopListening(Callback cb) {
        if (inner != null) inner.stopListening();
    }

    @Override
    protected void onCancel(Callback cb) {
        if (inner != null) inner.cancel();
        close();
    }

    @Override
    public void onDestroy() {
        close();
        super.onDestroy();
    }

    private interface Call { void run() throws RemoteException; }

    /** The caller may have gone away; that is not our crash. */
    private static void send(Call c) {
        try { c.run(); } catch (RemoteException ignored) { }
    }
}
