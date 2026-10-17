package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Build;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

/**
 * 往上翻一屏再翻回来 —— 只为一件事：**把被屏幕切掉的长消息补全**。
 *
 * <p><b>为什么必须有它</b>：{@code getRootInActiveWindow()} 只给可见节点。一条
 * 超过一屏的消息，在无障碍树里只有屏幕上那截，滚出去的部分<b>根本不存在</b> ——
 * 这不是解析 bug，是采集方式的物理上限。表格那次（一张表把整屏顶掉）是同一类
 * 问题的另一种表现。补全的唯一办法就是真的让屏幕动一下。
 *
 * <p><b>为什么不能常态化滚动</b>：群聊一直在来消息，每帧都滚的话手机就永久停在
 * 「滑动中」，滑动过程中读树还可能读到半张屏，反而更容易漏。所以只在
 * <b>电脑侧明确说「这一屏有消息没显示全」</b>时才滚（见 {@code Uploader.Result.body}
 * 里的 {@code scroll} 字段），且同一屏最多补一次。
 *
 * <p><b>为什么是「先上翻、再回到底」而不是「一屏一屏往下翻」</b>：
 * <ol>
 *   <li>聊天页默认就停在最新消息那端，往下翻是空的；</li>
 *   <li>要补的是「最新那条长消息的前半截」，它只可能在上方；</li>
 *   <li>翻完必须回到原位 —— 不回来的话，下一条新消息到来时基线会错位，
 *       表现为「锚点丢失、整屏停转」（今天已经踩过两次）。</li>
 * </ol>
 *
 * <p>⚠ <b>手势是「注入」而不是「模拟」</b>：{@code dispatchGesture} 走的是无障碍
 * 通道，不需要 root。但它要求系统在 {@code accessibilityFlags} 里给了
 * {@code canPerformGestures} —— 没给的话这里会一直返回 false，日志会说清楚，
 * 不会静默失效。
 */
final class Scroller {

    private static final String TAG = "DDWX";

    /** 一次滑动的手指持续时间。太短会被当成 fling（会多滑很远），太长会卡住。 */
    private static final long SWIPE_MS = 260L;

    /**
     * 滑动距离占列表高度的比例。留一点余量是刻意的：整屏高度滑的话，
     * 每次都可能停在一个「正好切在消息中间」的位置，白滑。
     */
    private static final float SWIPE_RATIO = 0.62f;

    /** 滑完等界面稳定再读树。动画没停就读，会读到半张屏。 */
    private static final long SETTLE_MS = 420L;

    private Scroller() {
    }

    /**
     * 这台机器能不能用「注入手势」补全。
     *
     * <p>判据只有两条：系统版本够（{@code dispatchGesture} 要 API 24+）、
     * 服务还连着。**不去读 {@code getServiceInfo().canPerformGestures}** ——
     * 那个字段在不少国产 ROM 上恒为 false，但 {@code dispatchGesture} 其实能用；
     * 拿它当闸门会让功能在真机上直接不出现，而且看不出来为什么。
     * 真正不能派发时，{@link #swipe} 的返回值会说清楚。
     */
    static boolean canSwipe(AccessibilityService svc) {
        return svc != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.N;
    }

    /**
     * 在 {@code list} 这个节点范围内做一次竖直滑动。
     *
     * @param up true = 手指往上划（内容向上走，露出下面的旧内容）
     * @return 手势是否成功派发
     */
    static boolean swipe(AccessibilityService svc, AccessibilityNodeInfo list,
                         boolean up) {
        if (svc == null || list == null) {
            return false;
        }
        android.graphics.Rect b = new android.graphics.Rect();
        list.getBoundsInScreen(b);
        if (b.width() <= 0 || b.height() <= 0) {
            return false;
        }
        // 必须落在列表内部：划在输入框或标题栏上不会滚动，还可能点到别的控件。
        float x = b.exactCenterX();
        float h = b.height() * SWIPE_RATIO / 2f;
        float cy = b.exactCenterY();
        float from = up ? cy + h : cy - h;   // 起点
        float to = up ? cy - h : cy + h;     // 终点

        Path p = new Path();
        p.moveTo(x, from);
        p.lineTo(x, to);
        GestureDescription gd = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, SWIPE_MS))
                .build();
        try {
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "滑动失败（系统没给 canPerformGestures？）：" + t);
            return false;
        }
    }

    /** 补全之后等界面停稳。 */
    static void settle() {
        try {
            Thread.sleep(SETTLE_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
