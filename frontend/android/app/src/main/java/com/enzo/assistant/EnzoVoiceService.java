package com.enzo.assistant;

import android.content.Context;
import android.content.Intent;
import android.graphics.drawable.ColorDrawable;
import android.os.Build;
import android.os.Bundle;
import android.service.voice.VoiceInteractionService;
import android.service.voice.VoiceInteractionSession;
import android.service.voice.VoiceInteractionSessionService;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.FrameLayout;

/**
 * Makes SHARP choosable as the phone's "Digital assistant app" (Settings > Apps > Default apps).
 * The phone's assistant shortcut (long-press power / home, corner swipe) then opens SHARP listening, from anywhere.
 * Android only lists an app here if it has this service, a session service and a recognition service (see
 * res/xml/voice_interaction_service.xml).
 */
public class EnzoVoiceService extends VoiceInteractionService {

    public static class Sessions extends VoiceInteractionSessionService {
        @Override
        public VoiceInteractionSession onNewSession(Bundle args) {
            return new PillSession(this);
        }
    }

    /**
     * The assistant's own window, which shows the pill (PopupActivity hands its web view over). Only the pill's
     * rectangle takes touches; the rest of the screen reaches the app behind. An Activity window can't allow that
     * on Android 15+ (its ActivityRecordInputSink blocks every touch outside it), so the pill does not live there.
     */
    public static class PillSession extends VoiceInteractionSession {
        static PillSession showing;
        FrameLayout root;

        PillSession(Context c) { super(c); }

        @Override
        public View onCreateContentView() {
            Window w = getWindow().getWindow();
            if (w != null) {
                w.setBackgroundDrawable(new ColorDrawable(0));
                w.clearFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
                w.addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
                w.setWindowAnimations(0);   // no system slide: the pill itself grows out of the camera hole (PopupActivity)
                if (Build.VERSION.SDK_INT >= 28) {
                    WindowManager.LayoutParams p = w.getAttributes();
                    p.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
                    w.setAttributes(p);
                }
            }
            root = new FrameLayout(getContext());
            return root;
        }

        @Override
        public void onShow(Bundle args, int showFlags) {
            super.onShow(args, showFlags);
            showing = this;
            PopupActivity live = PopupActivity.live;
            if (live != null && !live.isFinishing()) {   // pressed again while open: same page, it acts like a tap
                live.moveInto(this);
                live.getBridge().onNewIntent(new Intent(Intent.ACTION_ASSIST));
                return;
            }
            Intent i = new Intent(Intent.ACTION_ASSIST).setClass(getContext(), PopupActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            try {
                startAssistantActivity(i);  // allowed from the background, unlike a plain startActivity
            } catch (RuntimeException e) {
                getContext().startActivity(i);
            }
        }

        @Override
        public void onHide() {
            super.onHide();
            if (showing == this) showing = null;
            PopupActivity live = PopupActivity.live;
            if (live != null && live.session == this && !live.isFinishing()) live.finishAndRemoveTask();
        }

        @Override
        public void onComputeInsets(Insets out) {
            super.onComputeInsets(out);
            out.touchableInsets = Insets.TOUCHABLE_INSETS_REGION;
            out.touchableRegion.setEmpty();
            View pill = root == null || root.getChildCount() == 0 ? null : root.getChildAt(0);
            if (pill != null) {
                int[] at = new int[2];
                pill.getLocationInWindow(at);
                out.touchableRegion.set(at[0], at[1], at[0] + pill.getWidth(), at[1] + pill.getHeight());
            }
        }
    }
}
