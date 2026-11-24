package com.enzo.assistant;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.RectF;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;

/**
 * Enzo and the user drawing on the phone screen, in an accessibility overlay (no extra permission, sits over every app):
 *  - point: Enzo rings an item with a short label ("tap here"), gone after a few seconds; touches pass through.
 *  - circle: the user circles something for Enzo; the strokes come back in screen pixels.
 */
final class EnzoOverlay {

    interface Circled { void done(Path path, Rect bounds); }   // path null: he did not circle anything

    private static final int ACCENT = Color.rgb(255, 176, 32);
    private static final String CANCEL = "Tap here to cancel";
    private static final Handler ui = new Handler(Looper.getMainLooper());
    private static View pointView, circleView;

    private EnzoOverlay() { }

    private static WindowManager.LayoutParams params(boolean touchable) {
        WindowManager.LayoutParams p = new WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                | (touchable ? 0 : WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE),
            PixelFormat.TRANSLUCENT);
        p.gravity = Gravity.TOP | Gravity.LEFT;
        return p;
    }

    private static void remove(Context c, View v) {
        if (v == null) return;
        try { ((WindowManager) c.getSystemService(Context.WINDOW_SERVICE)).removeView(v); } catch (RuntimeException ignored) { }
    }

    private static Paint paint(float width, Paint.Style style, int color) {
        Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        p.setStyle(style);
        p.setStrokeWidth(width);
        p.setColor(color);
        p.setStrokeCap(Paint.Cap.ROUND);
        p.setStrokeJoin(Paint.Join.ROUND);
        return p;
    }

    /** Enzo rings `b` (screen pixels) with `label` under it. Main thread. Replaces an earlier ring. */
    static void point(Context c, Rect b, String label) {
        remove(c, pointView);
        float d = c.getResources().getDisplayMetrics().density;
        Paint ring = paint(4 * d, Paint.Style.STROKE, ACCENT);
        Paint text = paint(0, Paint.Style.FILL, Color.WHITE);
        text.setTextSize(16 * d);
        Paint bubble = paint(0, Paint.Style.FILL, Color.argb(230, 20, 20, 24));
        View v = new View(c) {
            @Override protected void onDraw(Canvas cv) {
                int[] at = new int[2];
                getLocationOnScreen(at);                                    // screen pixels -> this view
                RectF r = new RectF(b);
                r.offset(-at[0], -at[1]);
                r.inset(-10 * d, -10 * d);
                cv.drawRoundRect(r, 18 * d, 18 * d, ring);
                if (label == null || label.isEmpty()) return;
                float w = text.measureText(label) + 24 * d, h = 34 * d;
                float x = Math.max(8 * d, Math.min(r.centerX() - w / 2, getWidth() - w - 8 * d));
                float y = r.bottom + 8 * d + h < getHeight() ? r.bottom + 8 * d : r.top - 8 * d - h;
                cv.drawRoundRect(x, y, x + w, y + h, h / 2, h / 2, bubble);
                cv.drawText(label, x + 12 * d, y + h / 2 + 6 * d, text);
            }
        };
        ((WindowManager) c.getSystemService(Context.WINDOW_SERVICE)).addView(v, params(false));
        pointView = v;
        ui.postDelayed(() -> { if (pointView == v) { remove(c, v); pointView = null; } }, 8000);
    }

    private static void grow(Rect b, float x, float y) {
        if (b.isEmpty()) b.set((int) x, (int) y, (int) x + 1, (int) y + 1); else b.union((int) x, (int) y);
    }

    /** The user circles something; `cb` gets his strokes once he stops drawing for a moment (or null after `waitMs`). */
    static void circle(Context c, String hint, long waitMs, Circled cb) {
        remove(c, circleView);
        remove(c, pointView);
        pointView = null;
        float d = c.getResources().getDisplayMetrics().density;
        Path path = new Path();
        Rect bounds = new Rect();
        boolean[] finished = {false};
        Paint ink = paint(5 * d, Paint.Style.STROKE, ACCENT);
        Paint text = paint(0, Paint.Style.FILL, Color.WHITE);
        text.setTextSize(15 * d);
        Paint bar = paint(0, Paint.Style.FILL, Color.argb(235, 20, 20, 24));
        Paint small = paint(0, Paint.Style.FILL, Color.argb(190, 255, 255, 255));
        small.setTextSize(12 * d);
        String msg = hint == null || hint.isEmpty() ? "Circle it for Enzo" : hint;
        View[] self = new View[1];
        Runnable finish = new Runnable() {
            @Override public void run() {
                if (finished[0]) return;
                finished[0] = true;
                remove(c, self[0]);
                if (circleView == self[0]) circleView = null;
                cb.done(bounds.isEmpty() ? null : path, bounds.isEmpty() ? null : bounds);
            }
        };
        View v = new View(c) {
            final int[] at = new int[2];
            @Override protected void onDraw(Canvas cv) {
                cv.drawColor(Color.argb(40, 0, 0, 0));                      // he sees that the screen is in "circle" mode
                getLocationOnScreen(at);
                cv.save();
                cv.translate(-at[0], -at[1]);
                cv.drawPath(path, ink);
                cv.restore();
                float max = getWidth() - 44 * d;                            // the hint on one line, "tap here to cancel" under it
                String line = android.text.TextUtils.ellipsize(msg, new android.text.TextPaint(text), max, android.text.TextUtils.TruncateAt.END).toString();
                float top = 48 * d, h = 58 * d, w = Math.max(text.measureText(line), small.measureText(CANCEL)) + 28 * d;
                float x = (getWidth() - w) / 2;
                cv.drawRoundRect(x, top, x + w, top + h, 20 * d, 20 * d, bar);
                cv.drawText(line, (getWidth() - text.measureText(line)) / 2, top + 24 * d, text);
                cv.drawText(CANCEL, (getWidth() - small.measureText(CANCEL)) / 2, top + 46 * d, small);
            }
            @Override public boolean onTouchEvent(MotionEvent e) {
                float x = e.getRawX(), y = e.getRawY();                      // screen pixels, like the item bounds
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        ui.removeCallbacks(finish);
                        getLocationOnScreen(at);
                        if (bounds.isEmpty() && e.getY() < 112 * d) { path.reset(); finish.run(); return true; }   // the cancel bar
                        path.moveTo(x, y);
                        grow(bounds, x, y);
                        break;
                    case MotionEvent.ACTION_MOVE:
                        path.lineTo(x, y);
                        grow(bounds, x, y);
                        break;
                    case MotionEvent.ACTION_UP:
                        ui.postDelayed(finish, 900);                       // a second stroke may follow
                        break;
                    default:
                }
                invalidate();
                return true;
            }
        };
        self[0] = v;
        ((WindowManager) c.getSystemService(Context.WINDOW_SERVICE)).addView(v, params(true));
        circleView = v;
        ui.postDelayed(finish, waitMs);
    }
}
