package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.Intent;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.text.TextUtils;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * 自动巡检：把钉钉拉回前台、进到指定的群，让采集链路自己恢复。
 *
 * <p><b>为什么需要它</b>：采集只在「画面变了」时上报，而画面不变时电脑侧根本
 * 分不清「群里没人说话」和「钉钉被切走了／崩溃了／停在会话列表页」。后三种
 * 情况下一条都不会再转发，且不会有任何异常 —— 只能靠人发现。这个类就是
 * 把「人去看一眼手机」这件事自动化。
 *
 * <p><b>为什么要做成可配间隔的巡检，而不是「掉了立刻拉」</b>：这是用户
 * 2026-10-20 定的。立刻拉的问题是手机被强行抢前台 —— 用户回个消息、看个
 * 别的群，程序就跳出来。定时（5/10/15 分钟）虽然恢复得慢，但行为可预期、
 * 不会跟人抢操作。代价是**最多有这么久的盲区**，这一点必须让用户知道。
 *
 * <p><b>动作顺序（每一轮）</b>：
 * <ol>
 *   <li>前台不是钉钉 → {@code startActivity} 拉起来；</li>
 *   <li>等界面稳定，读标题栏；</li>
 *   <li>已经是目标群 → 什么都不做（这是最常见的分支，也是最该便宜的）；</li>
 *   <li>不是 → 唤出搜索框、填群名、在结果里找<b>文字完全一致</b>的那一行、点它；</li>
 *   <li>等一会儿再读一次标题栏校验，成功/失败都记到 {@link #lastResult}。</li>
 * </ol>
 *
 * <p><b>三道闸，一道都不能少</b>（和电脑端投放微信那两道闸同一个理由 ——
 * 点错群 = 整条采集链路开始把别人的群转进业务群）：
 * <ol>
 *   <li>点击前群名必须**完全一致**（去空白后 {@code equals}，不是 contains，
 *       不是模糊匹配）。找不到就整体放弃，**绝不点「最像的那个」**；</li>
 *   <li>点了之后必须**回读标题栏确认**。对不上就记 WARNING，并把
 *       {@link #lastResult} 写成失败 —— 不要靠「点了应该就对了」；</li>
 *   <li>搜索框里的旧关键词先清空再写，`ACTION_SET_TEXT` 失败就退回整串替换；
 *       写不进去直接放弃，不试着用键盘打（键盘状态不可控）。</li>
 * </ol>
 *
 * <p><b>不做什么</b>：不滚动会话列表去找群。列表页的节点读不出来时（真机上
 * 完全可能），搜索是唯一可靠的路径；两条路都留着会让失败原因变模糊。
 */
final class SelfHeal {

    private static final String TAG = "DDWX";

    /** 拉起钉钉后等它画完。冷启动实测 2~5 秒，这里取中间偏上。 */
    private static final long LAUNCH_SETTLE_MS = 2600L;

    /** 打完字到搜索结果出来。钉钉搜索是本地库，通常 < 1s。 */
    private static final long SEARCH_SETTLE_MS = 1400L;

    /** 点进群到页面画完。 */
    private static final long OPEN_SETTLE_MS = 1200L;

    /** 单点点击的手指时长。太短会被当 tap 抖动，40ms 足够稳。 */
    private static final long TAP_MS = 40L;

    /** 搜索入口在图标的 content-desc / text 里的候选写法（钉钉各版本不一致）。 */
    private static final String[] SEARCH_ENTRY_LABELS = {
            "搜索", "搜索聊天", "搜索消息", "搜一搜", "Search",
    };

    private SelfHeal() {
    }

    /** 一轮巡检的结果，直接显示在设置页和日志里。 */
    static final class Result {
        final boolean ok;
        final String detail;
        /** 这一轮到底动没动手（没动 = 本来就在目标群，最理想的情况）。 */
        final boolean acted;

        Result(boolean ok, String detail, boolean acted) {
            this.ok = ok;
            this.detail = detail;
            this.acted = acted;
        }

        @Override
        public String toString() {
            return (ok ? "正常" : "失败") + "：" + detail;
        }
    }

    static Result noop(String why) {
        return new Result(true, why, false);
    }

    static Result fail(String why) {
        return new Result(false, why, true);
    }

    /**
     * 跑一轮：确保钉钉在前台并且停在 {@code group} 这个群里。
     *
     * @param group 目标群名，必须与标题栏**完全一致**
     * @return 结果；调用方负责记日志。**本方法从不抛异常**（巡检线程被杀掉
     *         就再也没人恢复链路了），内部错误一律转成 fail。
     */
    static Result ensureGroup(AccessibilityService svc, String group) {
        if (svc == null) {
            return fail("无障碍服务没连着，做不了");
        }
        if (TextUtils.isEmpty(group)) {
            return fail("没配置目标群名");
        }
        String want = norm(group);
        try {
            // ① 前台不是钉钉 —— 先拉起来。这一步不需要看树，也最便宜。
            if (!isDingTalkForeground(svc)) {
                if (!launchDingTalk(svc)) {
                    return fail("拉不起钉钉（可能被系统自启动管理拦了）");
                }
                sleep(LAUNCH_SETTLE_MS);
            }

            // ② 读标题栏。读不到时**不当作「不在群里」** —— 可能是页面还在画。
            //    多等一次再读，还读不到就放弃，绝不盲目往下点搜索：
            //    在聊天页里乱搜索会把用户正看的会话顶掉。
            String title = readTitle(svc);
            if (TextUtils.isEmpty(title)) {
                sleep(1200L);
                title = readTitle(svc);
                if (TextUtils.isEmpty(title)) {
                    return fail("读不到钉钉标题栏（页面还在加载？）");
                }
            }
            if (norm(title).equals(want)) {
                return noop("已在目标群");
            }

            // ③ 不在目标群 —— 走搜索。宁可多走一次搜索，也不靠「列表里找」：
            //    列表要滚动、群名可能被截断，认错的代价是点进别的群。
            String opened = searchAndOpen(svc, group);
            if (opened == null) {
                return fail("搜索里没找到「" + group + "」，已放弃（不猜）");
            }

            // ④ 回读校验。点了不等于进去了，这一步是最后一道闸。
            sleep(OPEN_SETTLE_MS);
            String after = readTitle(svc);
            if (TextUtils.isEmpty(after)) {
                return fail("点开了但读不到标题栏，没法确认进的是哪个群");
            }
            if (!norm(after).equals(want)) {
                return fail("点开的是「" + clip(after) + "」，不是目标群");
            }
            return new Result(true, "已切回目标群（原在「" + clip(title) + "」）", true);
        } catch (Throwable t) {
            Log.w(TAG, "自动巡检出错", t);
            return fail("内部错误：" + t);
        }
    }

    // ------------------------------------------------------------ 前台与拉起

    /**
     * 前台是不是钉钉。
     *
     * <p>用根节点的包名判断，不用 {@code getRunningTasks}（API 21 起对普通
     * 应用不可用，无障碍服务也不保证能拿到）。根节点为空就当作「不确定」，
     * 此时**按不在前台处理** —— 拉起钉钉是幂等的（已经在前台会被系统忽略）。
     */
    static boolean isDingTalkForeground(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return false;
        }
        CharSequence pkg = root.getPackageName();
        return pkg != null && Prefs.DINGTALK_PKG.contentEquals(pkg);
    }

    /**
     * 把钉钉拉到前台。
     *
     * <p>{@code FLAG_ACTIVITY_NEW_TASK} 必须加：这里的 context 是 Service，
     * 不加会直接抛 {@code AndroidRuntimeException}。
     *
     * <p>⚠ 自 Android 10 起后台启动 Activity 默认被拦。无障碍服务属于
     * 少数例外，但**国产 ROM 上仍可能被「后台弹出界面」权限挡住** ——
     * 那种情况下这里返回 false 或拉起后前台仍是别的应用，后者由下一轮
     * 的标题栏读取自然暴露出来。所以设置页里要提示用户去开这个权限。
     */
    private static boolean launchDingTalk(Context c) {
        try {
            Intent i = c.getPackageManager().getLaunchIntentForPackage(Prefs.DINGTALK_PKG);
            if (i == null) {
                return false;
            }
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
            c.startActivity(i);
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "拉起钉钉失败：" + t);
            return false;
        }
    }

    // ------------------------------------------------------------ 读标题栏

    /**
     * 读聊天页标题栏里的群名。不在聊天页时返回 null。
     *
     * <p><b>判据来自真机样本</b>（{@code data/screen-20261017_191838.xml}）：
     * 标题栏里群名、企业名、成员数分三个 TextView，从上到下依次是
     * 「【数字质控】浙江杭州萧山区大江东公司」（y≈143）、「申通快递集团」
     * （y≈196）、「(内部群)」「5位成员」（y≈194）。所以：
     * <ol>
     *   <li>只在**屏幕上部 18%** 内找，避开消息正文（第一屏正文从 y≈250 起）；</li>
     *   <li>跳过明显的噪声：纯数字、括号开头的成员数/类型标签、纯图标字符；</li>
     *   <li>取<b>最上面</b>那个非噪声文本 —— 群名永远在第二行（企业名）之上。</li>
     * </ol>
     *
     * <p>⚠ 别改成「取最长的那个」：长群名和第一条消息的正文容易混淆，
     * 而正文里恰好含群名的情况（引用、回复）会让判断直接错掉。
     */
    static String readTitle(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        int limit = screen.height() <= 0 ? 400 : (int) (screen.height() * 0.18);

        String best = null;
        int bestTop = Integer.MAX_VALUE;
        for (AccessibilityNodeInfo n : flatten(root)) {
            String t = textOf(n);
            if (t == null) {
                continue;
            }
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            if (b.top < 0 || b.top > limit) {
                continue;
            }
            if (isNoise(t)) {
                continue;
            }
            if (b.top < bestTop) {
                bestTop = b.top;
                best = t;
            }
        }
        return best;
    }

    /** 括号开头的是「(内部群)」「(4)」这类角标；纯数字是未读/成员数。 */
    private static boolean isNoise(String t) {
        String s = t.trim();
        if (s.isEmpty()) {
            return true;
        }
        char c0 = s.charAt(0);
        if (c0 == '(' || c0 == '（' || c0 == '[' || c0 == '【' && s.length() <= 4) {
            return true;
        }
        if (s.matches("^\\d+$")) {
            return true;
        }
        // 图标字体：私有区字符，读出来是乱码方块，不可能是群名
        return s.length() == 1 && c0 >= 0xE000 && c0 <= 0xF8FF;
    }

    // ------------------------------------------------------------ 搜索并进群

    /**
     * 在钉钉里搜索 {@code group} 并点进去。
     *
     * @return 实际点开的那一行的群名；没找到返回 null
     */
    private static String searchAndOpen(AccessibilityService svc, String group) {
        String want = norm(group);

        // ① 找搜索入口。有的版本是 input 里带「搜索」提示语，有的是个图标按钮。
        String before = currentInputText(svc);
        AccessibilityNodeInfo entry = findSearchEntry(svc);
        if (entry == null) {
            Log.w(TAG, "巡检：找不到搜索入口");
            return null;
        }
        if (!tap(svc, entry)) {
            Log.w(TAG, "巡检：点搜索入口失败");
            return null;
        }
        sleep(SEARCH_SETTLE_MS);

        // ② 写关键词。搜索框可能是刚点出来的新输入框，重新找一次。
        AccessibilityNodeInfo box = findEditable(svc);
        if (box == null) {
            Log.w(TAG, "巡检：搜索框没出来");
            return null;
        }
        if (!setText(box, group)) {
            Log.w(TAG, "巡检：关键词写不进搜索框");
            return null;
        }
        sleep(SEARCH_SETTLE_MS);

        // ③ 在结果里找**完全同名**的那一行，点它。
        String hit = null;
        AccessibilityNodeInfo target = null;
        AccessibilityNodeInfo root = safeRoot(svc);
        for (AccessibilityNodeInfo n : flatten(root)) {
            String t = textOf(n);
            if (t == null || !norm(t).equals(want)) {
                continue;
            }
            // 结果行里可能同时有群名和「最近聊天」的会话名，取可点击的那层；
            // 都不可点就向上找一层可点的父节点（TextView 常包在 LinearLayout 里）。
            AccessibilityNodeInfo clickable = clickableAncestor(n, 3);
            if (clickable == null) {
                continue;
            }
            hit = t;
            target = clickable;
            break;
        }
        if (target == null) {
            Log.w(TAG, "巡检：搜索结果里没有完全同名的「" + group + "」");
            return null;
        }
        if (!tap(svc, target)) {
            Log.w(TAG, "巡检：点结果行失败");
            return null;
        }
        Log.i(TAG, "巡检：从「" + clip(before) + "」搜索并点开了「" + hit + "」");
        return hit;
    }

    /** 找搜索入口：先认 input 的提示语，再认带「搜索」字样的可点节点。 */
    private static AccessibilityNodeInfo findSearchEntry(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        AccessibilityNodeInfo byInput = null;
        for (AccessibilityNodeInfo n : flatten(root)) {
            String t = textOf(n);
            if (t == null) {
                continue;
            }
            String s = t.trim();
            for (String label : SEARCH_ENTRY_LABELS) {
                if (s.equalsIgnoreCase(label)) {
                    AccessibilityNodeInfo c = clickableAncestor(n, 3);
                    if (c != null) {
                        return c;
                    }
                }
            }
            if (byInput == null && s.contains("搜索") && n.isClickable()) {
                byInput = n;
            }
        }
        return byInput;
    }

    private static AccessibilityNodeInfo findEditable(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        for (AccessibilityNodeInfo n : flatten(root)) {
            if (n.isEditable()) {
                return n;
            }
        }
        return null;
    }

    private static String currentInputText(AccessibilityService svc) {
        AccessibilityNodeInfo n = findEditable(svc);
        return n == null ? "" : String.valueOf(textOf(n));
    }

    /**
     * 往输入框里写文字。
     *
     * <p>先 {@code SELECT_ALL} + 粘贴式覆盖（{@code ACTION_SET_TEXT} 本身是整串
     * 替换，不需要先删）。返回 false 表示这条路走不通 —— **不要**退回
     * 「模拟键盘逐字打」：输入法状态、候选词、中文编码全都不可控。
     */
    private static boolean setText(AccessibilityNodeInfo box, String text) {
        if (box == null || text == null) {
            return false;
        }
        try {
            android.os.Bundle args = new android.os.Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    text);
            return box.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        } catch (Throwable t) {
            Log.w(TAG, "ACTION_SET_TEXT 失败：" + t);
            return false;
        }
    }

    /** 从 n 往上找最多 {@code up} 层，返回第一个可点击的节点（含 n 自己）。 */
    private static AccessibilityNodeInfo clickableAncestor(AccessibilityNodeInfo n, int up) {
        AccessibilityNodeInfo cur = n;
        for (int i = 0; i <= up && cur != null; i++) {
            if (cur.isClickable()) {
                return cur;
            }
            cur = cur.getParent();
        }
        return null;
    }

    // ------------------------------------------------------------ 手势

    /**
     * 在节点的中心点单击一下。
     *
     * <p>用 {@code dispatchGesture} 而不是 {@code performAction(ACTION_CLICK)}：
     * 后者要求节点自己 {@code clickable}，钉钉大量可点区域是父容器上的点击代理，
     * 直接点坐标反而更接近真人操作。同一套通道 {@link Scroller} 已经在用。
     *
     * <p>坐标必须落在节点矩形**内部**（不要贴边）：贴边时骨架屏/圆角可能吃掉点击。
     */
    private static boolean tap(AccessibilityService svc, AccessibilityNodeInfo node) {
        if (svc == null || node == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.N) {
            return false;
        }
        Rect b = new Rect();
        node.getBoundsInScreen(b);
        if (b.width() <= 0 || b.height() <= 0) {
            return false;
        }
        float x = b.exactCenterX();
        float y = b.exactCenterY();
        Path p = new Path();
        p.moveTo(x, y);
        GestureDescription gd = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, TAP_MS))
                .build();
        try {
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "注入点击失败（系统没给 canPerformGestures？）：" + t);
            return false;
        }
    }

    // ------------------------------------------------------------ 工具

    /** 深度优先摊平成列表。节点数上限与 {@link UiTree} 一致，防极端界面卡死。 */
    static List<AccessibilityNodeInfo> flatten(AccessibilityNodeInfo root) {
        List<AccessibilityNodeInfo> out = new ArrayList<AccessibilityNodeInfo>();
        if (root == null) {
            return out;
        }
        Deque<AccessibilityNodeInfo> stack = new ArrayDeque<AccessibilityNodeInfo>();
        stack.push(root);
        int guard = 0;
        while (!stack.isEmpty() && guard++ < 4000) {
            AccessibilityNodeInfo n = stack.pop();
            if (n == null) {
                continue;
            }
            out.add(n);
            for (int i = n.getChildCount() - 1; i >= 0; i--) {
                AccessibilityNodeInfo c = n.getChild(i);
                if (c != null) {
                    stack.push(c);
                }
            }
        }
        return out;
    }

    private static AccessibilityNodeInfo safeRoot(AccessibilityService svc) {
        CollectorService s = CollectorService.get();
        return s == null ? null : s.safeRootPublic();
    }

    /** 节点上「看得见的那句话」：优先 text，其次 content-desc。 */
    private static String textOf(AccessibilityNodeInfo n) {
        if (n == null) {
            return null;
        }
        CharSequence t = n.getText();
        if (t != null && t.length() > 0) {
            return t.toString();
        }
        CharSequence d = n.getContentDescription();
        if (d != null && d.length() > 0) {
            return d.toString();
        }
        return null;
    }

    /**
     * 归一化：去掉所有空白与零宽字符，转半角括号。
     *
     * <p>钉钉标题栏里的群名常带不可见字符（真机样本里能找到 U+200B），而
     * 用户填配置时一定是从群里复制或手打。不做归一化就会出现「名字明明一样
     * 却永远判不相等」→ 每轮都去搜索 → 每轮都在抢前台。
     */
    static String norm(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (Character.isWhitespace(c) || c == '\u200B' || c == '\u00A0' || c == '\uFEFF') {
                continue;
            }
            if (c == '（') {
                sb.append('(');
            } else if (c == '）') {
                sb.append(')');
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private static String clip(String s) {
        if (s == null) {
            return "";
        }
        String t = s.replace('\n', ' ').trim();
        return t.length() <= 18 ? t : t.substring(0, 18) + "…";
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
