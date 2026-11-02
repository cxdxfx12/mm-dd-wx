package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Path;
import android.graphics.Rect;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * 群里的「复制」按钮，看到就点一下。
 *
 * <p>用户 2026-11-02：『群里面的复制都要点一下，这个复制是要考核的数据，
 * 不用粘贴出来，但是要点一下』。所以这里只做「点」，**不读剪贴板、不粘贴、
 * 不改界面**——点完钉钉自己弹一个 toast「已复制」，画面立刻恢复。
 *
 * <p>识别方式：钉钉的复制按钮是 {@code android.view.View} + {@code content-desc="复制"}
 * （真机 2026-11-02 实测：bounds 94×63、clickable=false、没有 text 节点）。
 * 只认 content-desc / text 等于「复制」的节点，不碰任何长得像的长文本。
 *
 * <p>去重是这套东西的关键：一轮巡检几十帧，同一张卡片会反复出现在树上，
 * 不去重就是一秒点十几次。指纹取「这条消息的容器 + 容器内全部文本」的摘要：
 * 容器 clickable 且 content-desc 是时间条（「今天 12:13」「昨天 18:02」），
 * 换行/滚动/重进群都不变。点过的指纹落盘保存，重启 App 也不会重复点。
 *
 * <p>三条纪律：
 * <ul>
 *   <li>附件抓取在跑时**不点** —— 手势是同一条通道，抢了会打断抓取；</li>
 *   <li>两次点击至少隔 {@link #MIN_GAP_MS}，每帧最多 {@link #MAX_PER_FRAME} 个，
 *       不能让「点复制」把抓帧拖成慢动作；</li>
 *   <li>指纹记满 {@link #MAX_REMEMBER} 条就丢最旧的（群里的卡片不会无限增长）。</li>
 * </ul>
 */
final class CopyTapper {

    private static final String TAG = "DDWX";
    private static final String PREF_FILE = "copy_tapped";
    private static final String PREF_KEY = "keys";
    private static final String PREF_COUNT = "count";

    /** 两次点击的最小间隔：留出 toast 消失、界面回落的时间。 */
    private static final long MIN_GAP_MS = 1500L;
    /** 每帧最多点几个。一帧几百毫秒，点多了一样能追上，只是别让帧变慢。 */
    private static final int MAX_PER_FRAME = 1;
    /** 记住多少条已点指纹。按一屏 2~3 个复制算，300 条够一个月。 */
    private static final int MAX_REMEMBER = 300;
    /** 找消息容器最多往上几层。真机是 2 层（View → LinearLayout[时间]）。 */
    private static final int MAX_UP = 8;

    private static long lastTapAt = 0L;
    private static long lastDiagAt = 0L;
    private static Set<String> cache = null;

    private CopyTapper() {
    }

    /**
     * 把当前树里**没点过**的复制按钮点掉，返回这一次点了几个。
     *
     * @param busy 附件抓取是否在进行（进行中直接不点，避免两条手势打架）
     */
    static int tapNew(CollectorService svc, AccessibilityNodeInfo root, boolean busy) {
        if (svc == null || root == null || busy) {
            return 0;
        }
        long now = System.currentTimeMillis();
        if (now - lastTapAt < MIN_GAP_MS) {
            return 0;                       // 刚点过，让界面缓一拍
        }
        List<AccessibilityNodeInfo> hits = new ArrayList<AccessibilityNodeInfo>();
        collect(root, hits, 0);
        if (hits.isEmpty()) {
            diag(now, "本帧没有「复制」按钮（树里 0 个）");
            return 0;
        }
        int screenW = 0, screenH = 0;
        try {
            android.util.DisplayMetrics dm = svc.getResources().getDisplayMetrics();
            screenW = dm.widthPixels;
            screenH = dm.heightPixels;
        } catch (Throwable ignored) {
        }
        Set<String> done = load(svc);
        int n = 0;
        int skipped = 0;
        for (AccessibilityNodeInfo v : hits) {
            String key = keyOf(v, root, hits);
            if (key == null || done.contains(key)) {
                skipped++;
                continue;
            }
            Rect r = new Rect();
            try {
                v.getBoundsInScreen(r);
            } catch (Throwable t) {
                skipped++;
                continue;
            }
            if (r.width() <= 4 || r.height() <= 4) {
                skipped++;
                continue;                   // 折叠/离屏节点，点了也没意义
            }
            // 视口外的节点 bounds 照样「有效」，点下去会打到别的控件上 ——
            // 宁可这一帧不点，等它滚进屏幕。
            if (screenW > 0 && screenH > 0
                    && (r.centerX() < 0 || r.centerY() < 0
                        || r.centerX() >= screenW || r.centerY() >= screenH)) {
                skipped++;
                continue;
            }
            if (!tap(svc, r.centerX(), r.centerY())) {
                Log.w(TAG, "点「复制」失败（坐标注入被拒）");
                skipped++;
                continue;
            }
            done.add(key);
            lastTapAt = System.currentTimeMillis();
            n++;
            Log.i(TAG, "点了「复制」（考核数据）· " + key);
            if (n >= MAX_PER_FRAME) {
                break;
            }
        }
        if (n > 0) {
            save(svc, done);
        } else if (skipped > 0) {
            diag(now, "本帧 " + hits.size() + " 个「复制」，都已点过/跳过 "
                    + skipped + " 个（累计已点 " + done.size() + " 条）");
        }
        return n;
    }

    /** 诊断日志节流：30 秒最多一条，别把 logcat 刷满。 */
    private static void diag(long now, String msg) {
        if (now - lastDiagAt < 30000L) {
            return;
        }
        lastDiagAt = now;
        Log.i(TAG, "复制按钮：" + msg);
    }

    /** 深度优先找「复制」按钮。深度/节点都设上限，坏树不能拖死采集线程。 */
    private static void collect(AccessibilityNodeInfo node, List<AccessibilityNodeInfo> out,
                                int depth) {
        if (node == null || depth > 40) {
            return;
        }
        if (isCopy(node)) {
            out.add(node);
            return;                         // 复制节点下面不会再有复制
        }
        int n;
        try {
            n = node.getChildCount();
        } catch (Throwable t) {
            return;
        }
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo c;
            try {
                c = node.getChild(i);
            } catch (Throwable t) {
                continue;
            }
            collect(c, out, depth + 1);
        }
    }

    private static boolean isCopy(AccessibilityNodeInfo node) {
        String desc = str(node.getContentDescription()).trim();
        if ("复制".equals(desc)) {
            return true;
        }
        String text = str(node.getText()).trim();
        return "复制".equals(text);
    }

    /**
     * 一条消息的指纹：先找「这条消息」的文本，再取摘要。
     *
     * <p>为什么不用坐标：滚动、换行、进来出去都会变。为什么带上时间条：
     * 同一段文案可能一天出现两次（日报/提醒），只有「时间 + 内容」才唯一。
     *
     * <p>真机 2026-11-02 踩到两种卡片结构，必须都能认：
     * <ul>
     *   <li><b>机器人图文卡</b>：复制节点 → View → 容器 LinearLayout
     *       （clickable、content-desc=「今天 12:13」）→ 正文都在容器里；</li>
     *   <li><b>H5/富文本卡</b>：往上七八层全是 FrameLayout、**一个字的 desc 都没有**，
     *       正文在复制按钮**上方的兄弟节点**里 —— 只能从整棵树按 y 找。</li>
     * </ul>
     * 所以先试「往上找带文本的祖先」，不行再退回「全树找复制按钮正上方最近的文本」。
     */
    private static String keyOf(AccessibilityNodeInfo copyNode, AccessibilityNodeInfo root,
                                List<AccessibilityNodeInfo> hits) {
        // ① 往上找「这条消息」的容器：越往外越可能把别的消息也圈进来，
        //    所以一旦某层装了不止一个复制按钮就停 —— 否则两张卡算出一模一样
        //    的指纹（真机 2026-11-02：屏幕上两个复制只点了第一个）。
        AccessibilityNodeInfo cur = copyNode;
        String bestFlat = "";
        String bestTimeKey = "";
        for (int up = 0; up < MAX_UP && cur != null; up++) {
            AccessibilityNodeInfo p;
            try {
                p = cur.getParent();
            } catch (Throwable t) {
                p = null;
            }
            if (p == null) {
                break;
            }
            Rect pr = new Rect();
            try {
                p.getBoundsInScreen(pr);
            } catch (Throwable ignored) {
            }
            if (copiesInside(pr, hits) > 1) {
                break;                      // 已经圈到别的消息了，就此打住
            }
            String desc = str(p.getContentDescription()).trim();
            // 时间条容器：可点 + desc 含冒号。顶部标题栏「钉钉的标题栏。」没有冒号，
            // 自然被排除。
            if (p.isClickable() && desc.contains(":") && bestTimeKey.isEmpty()) {
                bestTimeKey = desc + "\u0001" + flatten(p, 0);
            }
            String f = flatten(p, 0);
            if (f.length() >= 12) {
                bestFlat = f;               // 记最外层（循环越往上越外层）
            }
            cur = p;
        }
        if (!bestTimeKey.isEmpty()) {
            return "m" + sha1(bestTimeKey);
        }
        if (!bestFlat.isEmpty()) {
            return "a" + sha1(bestFlat);
        }
        // ② 兜底：全树里找复制按钮正上方最近的文本（H5 卡片的正文在上面兄弟里）
        Rect r = new Rect();
        try {
            copyNode.getBoundsInScreen(r);
        } catch (Throwable ignored) {
            return null;
        }
        String near = nearestTextAbove(root, r.top);
        if (!near.isEmpty()) {
            return "y" + sha1(near) + "@" + (r.top / 40);
        }
        // ③ 最后兜底：上方一个字都没有（卡片被顶部裁切、只剩半个按钮）。
        //    拿屏幕位置当指纹 —— 画面静止时不会重复点，滚动回来最多再点一次。
        //    点「复制」本身没有副作用（钉钉只弹个 toast），宁可多一次也别漏考核。
        return "p" + r.left + "," + r.top;
    }

    /** 这个矩形里圈住了几个「复制」按钮（按中心点算，不遍历子树，便宜）。 */
    private static int copiesInside(Rect container, List<AccessibilityNodeInfo> hits) {
        if (container.width() <= 0 || container.height() <= 0) {
            return hits.size();             // 拿不到边界时按「全都算」处理，别冒险
        }
        int c = 0;
        for (AccessibilityNodeInfo h : hits) {
            Rect r = new Rect();
            try {
                h.getBoundsInScreen(r);
            } catch (Throwable t) {
                continue;
            }
            if (container.contains(r.centerX(), r.centerY())) {
                c++;
            }
        }
        return c;
    }

    /** 容器内全部文本（含 content-desc）拼起来，用于做指纹。 */
    private static String flatten(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > 12) {
            return "";
        }
        StringBuilder sb = new StringBuilder(96);
        String t = str(node.getText()).trim();
        if (!t.isEmpty()) {
            sb.append(t).append('|');
        }
        String d = str(node.getContentDescription()).trim();
        if (!d.isEmpty()) {
            sb.append(d).append('|');
        }
        int n;
        try {
            n = node.getChildCount();
        } catch (Throwable e) {
            n = 0;
        }
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo c;
            try {
                c = node.getChild(i);
            } catch (Throwable e) {
                continue;
            }
            sb.append(flatten(c, depth + 1));
            if (sb.length() > 4000) {
                break;                      // 长卡片也够区分了，别把摘要喂太胖
            }
        }
        return sb.toString();
    }

    /**
     * 复制按钮正上方最近的一段文本（容器找不到时的退路）。
     *
     * <p>取的是**上方 900px 内最靠下的 4 段**，不是只取最近一条：最近的往往是
     * 发送者名（「签收跟单专员」）或时间条，几张卡片都一样，拿它做指纹会把
     * 不同消息混成同一条。多带几段才分得开。
     */
    private static String nearestTextAbove(AccessibilityNodeInfo root, int top) {
        List<Object[]> near = new ArrayList<Object[]>();   // {bottom, text}
        List<AccessibilityNodeInfo> stack = new ArrayList<AccessibilityNodeInfo>();
        stack.add(root);
        int guard = 0;
        final int floor = top - 900;
        while (!stack.isEmpty() && guard++ < 4000) {
            AccessibilityNodeInfo n = stack.remove(stack.size() - 1);
            Rect r = new Rect();
            try {
                n.getBoundsInScreen(r);
            } catch (Throwable t) {
                continue;
            }
            String t = str(n.getText()).trim();
            if (t.isEmpty()) {
                t = str(n.getContentDescription()).trim();
            }
            if (!t.isEmpty() && r.bottom <= top && r.bottom > floor) {
                near.add(new Object[]{r.bottom, t});
            }
            int c;
            try {
                c = n.getChildCount();
            } catch (Throwable e) {
                c = 0;
            }
            for (int i = 0; i < c; i++) {
                try {
                    stack.add(n.getChild(i));
                } catch (Throwable ignored) {
                }
            }
        }
        if (near.isEmpty()) {
            return "";
        }
        java.util.Collections.sort(near, new java.util.Comparator<Object[]>() {
            @Override
            public int compare(Object[] a, Object[] b) {
                return ((Integer) b[0]) - ((Integer) a[0]);   // bottom 降序
            }
        });
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < near.size() && i < 4; i++) {
            sb.append(near.get(i)[1]).append('|');
        }
        return sb.toString();
    }

    private static boolean tap(AccessibilityService svc, int x, int y) {
        try {
            Path p = new Path();
            p.moveTo(x, y);
            GestureDescription.StrokeDescription s =
                    new GestureDescription.StrokeDescription(p, 0, 40);
            GestureDescription gd = new GestureDescription.Builder()
                    .addStroke(s).build();
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "点「复制」抛异常：" + t);
            return false;
        }
    }

    // ---------------- 已点指纹（落盘） ----------------

    private static Set<String> load(Context ctx) {
        if (cache != null) {
            return cache;
        }
        Set<String> s = new LinkedHashSet<String>();
        try {
            SharedPreferences sp = ctx.getSharedPreferences(PREF_FILE, Context.MODE_PRIVATE);
            String raw = sp.getString(PREF_KEY, "");
            if (raw != null && !raw.isEmpty()) {
                for (String k : raw.split("\n")) {
                    if (!k.isEmpty()) {
                        s.add(k);
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "读已点指纹失败：" + t);
        }
        cache = s;
        return s;
    }

    private static void save(Context ctx, Set<String> s) {
        try {
            // 超上限丢最旧的：LinkedHashSet 是插入序，掐掉前面几条
            if (s.size() > MAX_REMEMBER) {
                List<String> all = new ArrayList<String>(s);
                s.clear();
                for (int i = all.size() - MAX_REMEMBER; i < all.size(); i++) {
                    s.add(all.get(i));
                }
            }
            StringBuilder sb = new StringBuilder();
            for (String k : s) {
                sb.append(k).append('\n');
            }
            ctx.getSharedPreferences(PREF_FILE, Context.MODE_PRIVATE)
                    .edit().putString(PREF_KEY, sb.toString()).apply();
        } catch (Throwable t) {
            Log.w(TAG, "存已点指纹失败：" + t);
        }
    }

    static int remembered(Context ctx) {
        return load(ctx).size();
    }

    private static String sha1(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] d = md.digest(s.getBytes("UTF-8"));
            StringBuilder sb = new StringBuilder(12);
            for (int i = 0; i < 6; i++) {
                sb.append(String.format("%02x", d[i]));
            }
            return sb.toString();
        } catch (Throwable t) {
            return Integer.toHexString(s.hashCode());
        }
    }

    private static String str(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }
}
