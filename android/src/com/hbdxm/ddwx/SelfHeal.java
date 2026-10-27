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

    /** 写完关键词后等搜索结果渲染的最长时间。弱网首搜 1.4s 常常不够（真机）。 */
    private static final long RESULT_WAIT_MS = 5000L;

    /** 点进群到页面画完。 */
    private static final long OPEN_SETTLE_MS = 1200L;

    /** 打开「最近任务」到卡片画出来。比冷启动快，但要等动画结束。 */
    private static final long RECENTS_SETTLE_MS = 900L;

    /** 单点点击的手指时长。太短会被当 tap 抖动，40ms 足够稳。 */
    private static final long TAP_MS = 40L;

    /** 搜索入口在图标的 content-desc / text 里的候选写法（钉钉各版本不一致）。 */
    private static final String[] SEARCH_ENTRY_LABELS = {
            "搜索", "搜索聊天", "搜索消息", "搜一搜", "Search",
    };

    /**
     * 全局搜索结果页顶部的分类 tab（2026-10-27 真机实测）。这些文字**永远**
     * 在树上、永远可点，而搜索结果是异步渲染的 —— fallback 找「任意可点
     * 祖先」时第一个扫到的就是它们，点掉之后标题栏显示「综合」，
     * 巡检报「点开的是『综合』」。这些词永远不许当 fallback 的点按目标。
     */
    private static final java.util.Set<String> SEARCH_TAB_WORDS = new java.util.HashSet<>(
            java.util.Arrays.asList("综合", "群组", "聊天记录", "联系人",
                    "应用", "全部", "更多"));

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
            //
            // ⚠ 拉完必须**回读确认**（2026-10-20 真机）：
            //    `startActivity` 返回 true 只代表「意图发出去了」，不代表
            //    钉钉真的到了前台。华为等 ROM 的「后台弹出界面」权限一关，
            //    系统会静默丢掉这次启动 —— 于是旧代码接着去读标题栏，
            //    读到的还是上一个应用的标题，最后报出来的失败原因
            //    变成了「读不到钉钉标题栏」或「搜索里没找到」，
            //    真正的原因（拉不起来）反而被埋掉了。
            if (!isDingTalkForeground(svc)) {
                String why = "";
                // 三条路依次试：正常启动 → 最近任务栈 → 系统「最近任务」界面。
                // 真机上（华为 PAL-AL00）前两条都会被「后台启动拦截」拒掉，
                // 只有第三条走得通，原因见 viaRecents 的说明。
                boolean ok = launchDingTalk(svc);
                if (ok) {
                    sleep(LAUNCH_SETTLE_MS);
                    ok = isDingTalkForeground(svc);
                    if (!ok) {
                        why = "启动请求发出去了但前台仍不是钉钉";
                    }
                } else {
                    why = "系统拒绝了直接启动";
                }
                if (!ok) {
                    Log.i(TAG, "直接启动无效（" + why + "），改用最近任务界面");
                    if (viaRecents(svc)) {
                        ok = true;
                        why = "";
                    } else {
                        why = why + "；最近任务里也点不开钉钉";
                    }
                }
                if (!ok) {
                    return fail("拉不起钉钉（" + why + "）。请确认钉钉没被系统杀掉，"
                            + "并给本应用开「应用启动管理 → 手动管理」的三个开关");
                }
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

            // ②.5 停在网页/内置浏览器这类**盖住一切**的页面上时，先退出去。
            //      实测（2026-10-20）：钉钉停在「喵喵采集端 · 安装」这个 H5 页
            //      （webview_root / webview_frame_layout），整棵树只有 3 个文本，
            //      会话列表和搜索入口一个都不在。此时直接找搜索必然失败，
            //      而且失败原因会显示成「搜索里没找到」—— 离真正的原因很远。
            String blocked = blockingPage(svc);
            if (blocked != null) {
                // 只退一两层：退太多可能把钉钉退到桌面（那就变成另一类故障）。
                for (int i = 0; i < 3; i++) {
                    if (!back(svc)) {
                        break;
                    }
                    sleep(600L);
                    String now = readTitle(svc);
                    if (TextUtils.isEmpty(now) || !blocked.equals(now)) {
                        break;         // 已经离开那个页面了
                    }
                }
                sleep(600L);
                String back2 = readTitle(svc);
                if (!TextUtils.isEmpty(back2) && norm(back2).equals(want)) {
                    return new Result(true,
                            "已从「" + clip(blocked) + "」退出并回到目标群", true);
                }
                title = TextUtils.isEmpty(back2) ? title : back2;
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
    /**
     * 体检：把「现在钉钉停在哪一页」一句话说清楚，不碰手机。
     *
     * <p><b>为什么要加这个方法</b>（2026-10-20）：巡检失败时电脑端只看到
     * 「读不到钉钉标题栏（页面还在加载？）」，而这一句至少对应三种完全不同的
     * 情况 —— 钉钉被拉到桌面前、停在某个没有标题的页、或者根本不在钉钉里。
     * 华为屏蔽第三方 logcat，人不在手机边上就只能猜。快查把这些一次性答完：
     * 前台是谁、根节点在不在、标题读成什么、viewId 里有什么能当判据。
     *
     * <p>只读，不点、不切、不滚动 —— 随时可以调，不会干扰用户。
     */
    static String probe(AccessibilityService svc) {
        StringBuilder sb = new StringBuilder();
        try {
            sb.append("service=").append(svc == null ? "no" : "yes");
            if (svc == null) {
                return sb.toString();
            }
            AccessibilityNodeInfo root = safeRoot(svc);
            if (root == null) {
                // 整棵树拿不到时**什么也别推断**：可能是页面正在切换，
                // 也可能是无障碍被系统临时掐了。
                sb.append(" · root=null");
                return sb.toString();
            }
            CharSequence pkg = root.getPackageName();
            CharSequence cls = root.getClassName();
            sb.append(" · pkg=").append(pkg == null ? "?" : pkg.toString());
            sb.append(" · rootcls=").append(cls == null ? "?" : cls.toString());

            java.util.List<AccessibilityNodeInfo> all = flatten(root);
            sb.append(" · nodes=").append(all.size());

            String title = readTitle(svc);
            sb.append(" · title=").append(TextUtils.isEmpty(title) ? "空" : clip(title));
            String blocked = blockingPage(svc);
            sb.append(" · blocked=").append(blocked == null ? "无" : clip(blocked));
            sb.append(" · inChat=").append(inChatPage(svc) ? "yes" : "no");
            sb.append(" · searchBox=").append(findSearchBox(svc) == null ? "no" : "yes");

            String page = svc.getPackageName() == null ? "" : currentComponent(svc);
            sb.append(" · comp=").append(page);

            // 顶部 25% 的 id / 文字，按行号排 —— 这是「这一页长什么样」的
            // 唯一可靠描述。标题读错、认错页面时，答案通常就在这几行里。
            Rect screen = new Rect();
            root.getBoundsInScreen(screen);
            int limit = screen.height() <= 0 ? 400 : (int) (screen.height() * 0.25);
            StringBuilder top = new StringBuilder();
            int shown = 0;
            for (AccessibilityNodeInfo n : all) {
                Rect b = new Rect();
                n.getBoundsInScreen(b);
                if (b.top < 0 || b.top > limit) {
                    continue;
                }
                CharSequence rid = n.getViewIdResourceName();
                CharSequence tx = n.getText();
                if ((rid == null || rid.length() == 0)
                        && (tx == null || tx.toString().trim().isEmpty())) {
                    continue;
                }
                if (shown++ >= 12) {
                    break;
                }
                top.append('[').append(b.left).append(',').append(b.top).append(']');
                if (rid != null && rid.length() > 0) {
                    String id = rid.toString();
                    int slash = id.lastIndexOf('/');
                    top.append(id.substring(slash + 1));
                }
                if (tx != null && tx.toString().trim().length() > 0) {
                    top.append('「').append(clip(tx.toString())).append('」');
                }
                top.append(' ');
            }
            sb.append(" · top=").append(top.length() == 0 ? "空" : top.toString().trim());
        } catch (Throwable t) {
            sb.append(" · 快查出错：").append(t);
        }
        return sb.toString();
    }

    /** 当前窗口的组件名（Activity），用来区分「同一包名的不同页面」。 */
    private static String currentComponent(AccessibilityService svc) {
        try {
            // 根节点上没有 Activity 名，但节点树分布已经能区分页面；
            // 这里只取一个尽量短的标识：根节点下第一层子节点的 id。
            AccessibilityNodeInfo root = safeRoot(svc);
            if (root == null || root.getChildCount() <= 0) {
                return "?";
            }
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < root.getChildCount() && i < 3; i++) {
                AccessibilityNodeInfo c = root.getChild(i);
                if (c == null) {
                    continue;
                }
                CharSequence rid = c.getViewIdResourceName();
                if (rid != null && rid.length() > 0) {
                    String id = rid.toString();
                    sb.append(id.substring(id.lastIndexOf('/') + 1)).append('/');
                }
            }
            return sb.length() == 0 ? "?" : sb.toString();
        } catch (Throwable t) {
            return "?";
        }
    }

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
        // 第一条路：正常启动意图。冷启动时走这条才对（会恢复上次的页面）。
        if (tryStart(c, c.getPackageManager().getLaunchIntentForPackage(Prefs.DINGTALK_PKG)))
            return true;
        // 第二条路：从最近任务里把它调回来。第一条被「后台弹出界面」权限挡住时，
        // 这条有时还能成 —— 它不是「启动」而是「切回」，很多 ROM 对这两种
        // 的拦截策略不同。注意它只能唤醒**已存在**的任务栈，钉钉被杀掉时无效。
        try {
            android.app.ActivityManager am = (android.app.ActivityManager)
                    c.getSystemService(Context.ACTIVITY_SERVICE);
            if (am != null) {
                java.util.List<android.app.ActivityManager.RecentTaskInfo> tasks =
                        am.getRecentTasks(32, android.app.ActivityManager.RECENT_IGNORE_UNAVAILABLE);
                for (android.app.ActivityManager.RecentTaskInfo t : tasks) {
                    if (t == null || t.baseIntent == null) {
                        continue;
                    }
                    android.content.ComponentName cn = t.baseIntent.getComponent();
                    if (cn == null || !Prefs.DINGTALK_PKG.equals(cn.getPackageName())) {
                        continue;
                    }
                    android.content.Intent i = new android.content.Intent(t.baseIntent);
                    i.setFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                    if (tryStart(c, i)) {
                        return true;
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "从最近任务调回钉钉失败：" + t);
        }
        // 第三条路（真机验证有效）见 ensureGroup 的「最近任务」分支。
        // 它不走 startActivity，而是让无障碍服务自己打开最近任务再点钉钉，
        // 属于系统级动作，不受「后台启动 Activity」这条限制。
        return false;
    }

    /**
     * 通过「最近任务」把钉钉切回前台。
     *
     * <p><b>为什么需要这一条</b>（2026-10-20 真机，华为 PAL-AL00）：
     * 用户把「后台弹出界面」「应用启动管理」三个开关全开了，
     * `startActivity` **依然**抛异常 —— 华为对无障碍服务启动第三方应用
     * 额外做了拦截，那个权限开关管不到这条路径。前两条路全部失效，
     * 表现就是「点了立即巡检，手机一点反应都没有」。
     *
     * <p>而 `GLOBAL_ACTION_RECENTS` 是**系统替无障碍服务执行的**，
     * 不经过「应用启动」的检查，因此不受限制。代价是要多点一下：
     * 打开最近任务 → 找到钉钉那一张卡 → 点它。
     *
     * @return 切回来了返回 true
     */
    private static boolean viaRecents(AccessibilityService svc) {
        for (int round = 0; round < 2; round++) {
            try {
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_RECENTS);
            } catch (Throwable t) {
                Log.w(TAG, "打开最近任务失败：" + t);
                return false;
            }
            sleep(RECENTS_SETTLE_MS);
            if (isDingTalkForeground(svc)) {
                return true;               // 有些 ROM 会直接回到上一个应用
            }
            AccessibilityNodeInfo card = findDingTalkCard(svc);
            if (card != null && tap(svc, card)) {
                sleep(LAUNCH_SETTLE_MS);
                if (isDingTalkForeground(svc)) {
                    return true;
                }
            }
            Log.w(TAG, "最近任务里没找到钉钉卡片（第 " + (round + 1) + " 次）");
        }
        return false;
    }

    /**
     * 在最近任务界面里找钉钉那张卡。
     *
     * <p>判据：节点的 content-desc / text 里含「钉钉」，且可点。华为最近任务里
     * 卡片的无障碍名通常就是应用名，很稳。找不到就返回 null —— **绝不**点
     * 「最像的那个」，点错等于把用户切到别的应用，比不动作更糟。
     */
    private static AccessibilityNodeInfo findDingTalkCard(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        for (AccessibilityNodeInfo n : flatten(root)) {
            CharSequence t = n.getText();
            CharSequence d = n.getContentDescription();
            String s = (t == null ? "" : t.toString()) + " " + (d == null ? "" : d.toString());
            if (!s.contains("钉钉")) {
                continue;
            }
            AccessibilityNodeInfo c = clickableAncestor(n, 4);
            if (c != null) {
                return c;
            }
        }
        return null;
    }

    private static boolean tryStart(Context c, android.content.Intent i) {
        if (i == null) {
            return false;
        }
        try {
            i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                    | android.content.Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
            c.startActivity(i);
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "启动意图失败：" + t);
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
     *   <li>取 `<b>text</b>` 属性（不是 content-desc）里最靠上的那个 ——
     *       群名永远在第二行（企业名）之上。</li>
     * </ol>
     *
     * <p>⚠ <b>只能用 text，不能用 content-desc</b>（2026-10-20 真机翻车）：
     * 标题栏右侧「发起钉钉电话」「群聊信息」两个**图标按钮**的 content-desc
     * 落在 y=115，比群名的 y=143 还高。原来 {@link #textOf} 会退回
     * content-desc，于是每一轮都读到「发起钉钉电话」，判定成「不在目标群」→
     * 去搜索 → 在聊天页里根本没有搜索入口 → 报「搜索里没找到」。
     * 手机其实一直好好停在目标群，巡检却每 10 分钟把界面点乱一次。
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
            // 只认 text。content-desc 在这块区域里全是按钮的无障碍说明，
            // 它们的 y 比群名更高，会把群名挤掉（见上面的真机记录）。
            CharSequence cs = n.getText();
            String t = cs == null ? "" : cs.toString();
            if (t.trim().isEmpty()) {
                continue;
            }
            if (isNoise(t)) {
                continue;
            }
            // ⚠ 全局搜索页的搜索框里就写着**用户输入的群名**，位置比标题栏还高。
            //    把它当标题会得出「已在目标群」的假成功（2026-10-20 真机）——
            //    实际人停在搜索页，一条消息都没采集到。搜索框有稳定的
            //    viewId（search_src_text），直接跳过。
            CharSequence nid = n.getViewIdResourceName();
            if (nid != null && nid.toString().contains("search_src_text")) {
                continue;
            }
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            if (b.top < 0 || b.top > limit) {
                continue;
            }
            if (b.top < bestTop) {
                bestTop = b.top;
                best = t;
            }
        }
        return best;
    }

    /**
     * 当前是不是停在「盖住一切」的页面上（网页 / 内置浏览器 / 文件预览）。
     *
     * <p>返回那个页面的标题，不是就返回 null。
     *
     * <p><b>为什么非要有这一步</b>（2026-10-20 真机）：手机装完 App 后钉钉打开了
     * 内置浏览器里的「喵喵采集端 · 安装」页，整棵树只剩
     * ``webview_root / webview_frame_layout`` 和 3 个文本 —— 会话列表、搜索框、
     * 底部 Tab 全都不在。巡检从这一页出发只会得到「搜索里没找到」，
     * 让人以为是群名写错了，实际只要退一层就回来了。
     *
     * <p>判据用**布局 id**（``webview``）而不是标题文字：标题是用户业务内容，
     * 任何写法都可能出现；而 WebView 容器是钉钉自己的容器 id，稳定得多。
     */
    private static String blockingPage(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        boolean webview = false;
        for (AccessibilityNodeInfo n : flatten(root)) {
            CharSequence id = n.getViewIdResourceName();
            if (id != null && id.toString().contains("webview")) {
                webview = true;
                break;
            }
            if ("android.webkit.WebView".contentEquals(n.getClassName())) {
                webview = true;
                break;
            }
        }
        if (!webview) {
            // AI 助理对话页同样「盖住一切」：它有输入框、有气泡，
            // 气泡里甚至可能出现群名（用户问过这个群）——但它不是聊天页，
            // 采集不到消息，也不该在这里搜索。特征文案是钉钉自己的
            // 界面文字，不是用户内容，不会误伤（2026-10-20 19:09 真机）。
            for (AccessibilityNodeInfo n : flatten(root)) {
                CharSequence cs = n.getText();
                if (cs == null) {
                    continue;
                }
                String t = cs.toString();
                if (t.contains("内容由 AI 生成") || t.contains("正在理解你的问题")) {
                    return "AI助理页";
                }
            }
            return null;
        }
        String t = readTitle(svc);
        return TextUtils.isEmpty(t) ? "网页页" : t;
    }

    /** 按一次返回。无障碍服务发全局返回键，不需要 root。 */
    private static boolean back(AccessibilityService svc) {
        try {
            return svc.performGlobalAction(
                    AccessibilityService.GLOBAL_ACTION_BACK);
        } catch (Throwable t) {
            Log.w(TAG, "发送返回键失败：" + t);
            return false;
        }
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
        if (inChatPage(svc) && !backToMessageList(svc)) {
            Log.w(TAG, "巡检：在聊天页里退不出消息列表，放弃本轮（不乱点）");
            return null;
        }
        // ⚠ 识别「内页」不能只靠 inChatPage：服务号会话（工作通知）底部
        //    没有输入框，inChatPage 判不出来（2026-10-20 21:26 真机：
        //    用户停在「工作通知：杭州东申物流」，报「搜索里没找到」）。
        //    改成**正向判据**：找不到搜索入口 = 不在消息列表主页，
        //    只要还在钉钉里就先退一层再找 —— 消息列表主页必有
        //    header_search_btn，退不到它就说明真的没路了。
        // ⚠ 已在全局搜索页时**绝不能再找入口点**：这一页上还有别的
        //    「搜索」字样节点（输入框提示语）和 AI 助理圆球 —— 兜底
        //    匹配点中 AI 球就掉进 AI 助理对话，把群名当问题问出去
        //    （2026-10-20 19:25 真机：watch 报「搜索里没找到」，
        //    前台却停在 AI 生成的 WebView 答案页）。判据：搜索框
        //    （search_src_text）已经存在 = 已经在搜索页，直接跳到写词。
        if (findSearchBox(svc) == null) {
            AccessibilityNodeInfo entry = findSearchEntry(svc);
            if (entry == null) {
                // 不在消息列表主页（聊天页 / 服务号页 / 别的内页）：
                // 退一层再找。退完还找不到才是真没路。
                Log.i(TAG, "巡检：这一屏没有搜索入口，先退一层");
                if (!backToMessageList(svc)) {
                    Log.w(TAG, "巡检：找不到搜索入口，退一层后也没有");
                    return null;
                }
                entry = findSearchEntry(svc);
                if (entry == null) {
                    Log.w(TAG, "巡检：退回消息列表后仍找不到搜索入口");
                    return null;
                }
            }
            if (!tap(svc, entry)) {
                Log.w(TAG, "巡检：点搜索入口失败");
                return null;
            }
            sleep(SEARCH_SETTLE_MS);
        }

        // ② 写关键词。搜索框可能是刚点出来的新输入框，重新找一次。
        AccessibilityNodeInfo box = findSearchBox(svc);
        if (box == null) {
            Log.w(TAG, "巡检：搜索框没出来");
            return null;
        }
        if (!setText(box, group)) {
            Log.w(TAG, "巡检：关键词写不进搜索框");
            return null;
        }
        // 结果是异步的：固定 sleep 一次在弱网/首搜时会等空 —— 结果行还没
        // 渲染出来就判「没找到」，下一轮又从头来。这里改成轮询，
        // 最多再等 RESULT_WAIT_MS，每 500ms 重新抓一遍树。
        long deadline = System.currentTimeMillis() + RESULT_WAIT_MS;
        while (true) {
            sleep(500L);
            // 结果行没渲染出来之前**绝不允许 fallback 抢点** —— 搜索页顶部的
            // tab（「综合」等）永远可点，先放开就会把巡检带去点标签（2026-10-27
            // 连续两轮「点开的是『综合』」就是它）。只在轮询到点的最后一刻
            // 才放开，保住「绝不空手而归」的老底线。
            boolean timeUp = System.currentTimeMillis() >= deadline;
            String found = findResultRow(svc, want, timeUp);
            if (found != null) {
                return found;                       // findResultRow 内部已点
            }
            if (timeUp) {
                Log.w(TAG, "巡检：搜索结果等了 " + RESULT_WAIT_MS + "ms 没出来"
                        + "（搜索框实际内容：\"" + currentInputText(svc) + "\"）");
                return null;
            }
        }
    }

    /**
     * 在结果里找完全同名的行并点它（{@code want} 是 norm 过的目标群名）。
     *
     * @param allowFallback false = 只认精确命中，找不到就空手而归（让调用方
     *                      继续轮询）；true = 允许退回「任意可点祖先」。
     * @return 命中的原文；没找到返回 null（不点任何东西）
     */
    private static String findResultRow(AccessibilityService svc, String want,
                                        boolean allowFallback) {

        // ③ 在结果里找**完全同名**的那一行，点它。
        //
        // ⚠ 真机（2026-10-20，钉钉 9.0.2）在同一个结果行里放了两个命中点：
        //    · 左边 tv_friend_name 是**会话条目**，点它进 ChatMsgActivity（要的）
        //    · 右边 group_tag_layout 是「猜你想找」标签，点它开
        //      CommonWebViewActivity —— 一个空白网页，看起来像「卡住了」
        //  同一个群名 tex 也出现了两次。所以顺序必须固定：
        //  先认会话条目（viewId 含 tv_friend_name 的那层），认不到再退回
        //  通用可点祖先，并且**排除**落在标签区里的那个。
        //
        // ⚠ 2026-10-27 再加一条：fallback **不许提前抢跑**。结果页顶部
        //    「综合 / 群组 / 聊天记录」三个 tab 是永远可点的文本节点，而搜索
        //    结果是异步渲染的 —— 结果还没画出来时，fallback 就会第一个扫到
        //    tab 并当场点掉（点完的页面标题恰好就是「综合」），外层的轮询
        //    「等结果行」机制等于没做。真机两次「点开的是『综合』」就是它。
        //    所以 fallback 必须由调用方在轮询超时后才放开（allowFallback），
        //    并且 tab 文本本身永远不许当 fallback。
        String hit = null;
        AccessibilityNodeInfo target = null;
        AccessibilityNodeInfo fallback = null;
        String fallbackHit = null;
        AccessibilityNodeInfo root = safeRoot(svc);
        for (AccessibilityNodeInfo n : flatten(root)) {
            String t = textOf(n);
            if (t == null) {
                continue;
            }
            // ⚠ 结果行的文字是「群名(成员数)」，如「…大江东公司(8)」——
            //    直接 equals(want) 永远不等，每轮都报「搜索里没找到」
            //    （2026-10-20 真机，watch_runs 连续失败就是这个）。
            //    所以先把尾部括号后缀剥掉再比。
            String bare = stripTailParen(t);
            if (bare == null || !norm(bare).equals(want)) {
                continue;
            }
            String rid = String.valueOf(n.getViewIdResourceName());
            boolean inTag = isInsideTag(n);
            // 直接认 tv_friend_name 文本节点本身 —— 不找可点祖先：
            // tap() 走坐标手势，节点可不可点无所谓。而且**必须点它自己的
            // 矩形并靠左**：真机（2026-10-20）同一行里点 x≈610（行中心）
            // 会开群资料 H5（看起来像卡死），点 x≈300（靠左）才进会话。
            if (rid.contains("tv_friend_name") && !inTag) {
                hit = t;
                target = n;
                break;
            }
            if (fallback == null && !inTag
                    && !SEARCH_TAB_WORDS.contains(t)) {
                AccessibilityNodeInfo c = clickableAncestor(n, 4);
                if (c != null) {
                    fallback = c;
                    fallbackHit = t;
                }
            }
        }
        if (target == null && allowFallback) {
            target = fallback;
            hit = fallbackHit;
        }
        if (target == null) {
            return null;            // 没到点：交给外层的轮询继续等
        }
        if (!tapLeft(svc, target)) {
            Log.w(TAG, "巡检：点结果行失败");
            return null;
        }
        Log.i(TAG, "巡检：搜索并点开了「" + hit + "」");
        return hit;
    }

    /**
     * 全局搜索页的搜索框（{@code search_src_text}）。已在该页时用它判断
     * 「不用再点入口」；也是唯一应该被写关键词的输入框 —— 泛泛找
     * EditText 会撞上 AI 助理的对话框。
     */
    private static AccessibilityNodeInfo findSearchBox(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        for (AccessibilityNodeInfo n : flatten(root)) {
            CharSequence rid = n.getViewIdResourceName();
            if (rid != null && rid.toString().endsWith("search_src_text")) {
                return n;
            }
        }
        return null;
    }

    /**
     * 剥掉尾部的「(8)」「（3）」「(内部群)」这类括号后缀。
     *
     * <p>只剥**一对**且必须在末尾；剥完为空就返回 null（说明原文整个是括号，
     * 不是群名）。钉钉搜索结果行在群名后面拼成员数，直接全文比对永远失败。
     */
    private static String stripTailParen(String s) {
        s = s.trim();
        if (s.isEmpty()) {
            return null;
        }
        char last = s.charAt(s.length() - 1);
        if (last != ')' && last != '）') {
            return s;
        }
        char open = last == ')' ? '(' : '（';
        int i = s.lastIndexOf(open);
        if (i <= 0) {
            return null;
        }
        return s.substring(0, i).trim();
    }

    /**
     * 这个节点是不是坐在「猜你想找」标签区里（{@code group_tag_layout}）。
     *
     * <p>真机上点那块会开一个空白 WebView 页——看起来就像「点了没反应」。
     * 判据用 viewId 而不是坐标：坐标随字体大小/机型变，id 是钉钉自己的。
     */
    private static boolean isInsideTag(AccessibilityNodeInfo n) {
        AccessibilityNodeInfo cur = n;
        for (int i = 0; i <= 3 && cur != null; i++) {
            String rid = String.valueOf(cur.getViewIdResourceName());
            if (rid.contains("group_tag_layout")) {
                return true;
            }
            cur = cur.getParent();
        }
        return false;
    }

    /**
     * 从聊天页退回到「有搜索入口」的那一屏（消息列表），成功返回 true。
     *
     * <p>为什么不直接在聊天页点搜索：聊天页里**没有**全局搜索入口，
     * 只有底部输入框；而输入框上方的联想区经常出现含「搜索」二字的正文，
     * 兜底匹配会把它们当入口点下去。
     *
     * <p>为什么最多退 2 次：钉钉聊天页往上一两层就是消息列表，
     * 退多了会退到桌面，那就从「找不到入口」变成「拉不起钉钉」两种故障混在一起了。
     */
    private static boolean backToMessageList(AccessibilityService svc) {
        for (int i = 0; i < 2; i++) {
            if (!back(svc)) {
                return false;
            }
            sleep(700L);
            if (!isDingTalkForeground(svc)) {
                return false;          // 退到别的应用了，交给上层判失败
            }
            // 到站判据用**正向**的：消息列表主页 = header_search_btn 存在。
            // ⚠ 不能再要求 !inChatPage：服务号会话没有输入框，inChatPage
            //    判不出它，把它和消息列表混为一谈（2026-10-20 21:26 真机）。
            if (findSearchEntry(svc) != null) {
                return true;
            }
        }
        return findSearchEntry(svc) != null;
    }

    /**
     * 当前是不是停在聊天页（有消息列表 + 底部输入区）。
     *
     * <p>判据用底部输入框的提示语。⚠ 不能只认「发消息」一种：群被禁言时
     *    提示语是「禁言中」（2026-10-20 20:54 真机：用户切进禁言群后点
     *    「切到钉钉群」，inChatPage 返回 false → 在聊天页里找搜索入口 →
     *    聊天页没有 header_search_btn → 报「搜索里没找到」）。
     * 真机上它是 `com.alibaba.android.rimet:id/tv_text_content`，y≈2191
     * （屏幕高 2480 的 88%），固定在输入区，不会出现在会话列表/搜索页。
     *
     * <p>为什么要这个判断：聊天页里根本没有搜索入口。一旦标题读错
     * （见 {@link #readTitle} 的真机记录），就会走到这里找搜索，
     * 而兜底匹配会把输入区附近的东西当成入口点下去 —— 不产生任何效果，
     * 但会把界面弄乱，还会让失败原因显示成「搜索里没找到」。
     */
    private static boolean inChatPage(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return false;
        }
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        int bottomZone = screen.height() <= 0 ? 0 : (int) (screen.height() * 0.75);
        // ⚠ 判据是「屏幕下方有一个可编辑输入框」，**不是提示语文案**。
        //    文案是打地鼠：「发消息或按住说话」→ 禁言群是「禁言中」→
        //    开了 AI 助手的群是「请输入消息或@AI小钉」（2026-10-20 21:10
        //    真机又栽一次）。可编辑节点是输入框的本体属性，不会跟文案变。
        //    消息列表主页面没有底部输入框，所以不会误判。
        for (AccessibilityNodeInfo n : flatten(root)) {
            if (!n.isEditable()) {
                continue;
            }
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            if (b.top >= bottomZone) {
                return true;        // 在屏幕下方，确实是底部输入区
            }
        }
        // 兜底：个别机型 isEditable 不可靠时退回文案判据（含已知三种变体）。
        for (AccessibilityNodeInfo n : flatten(root)) {
            CharSequence cs = n.getText();
            if (cs == null) {
                continue;
            }
            String t = cs.toString().trim();
            if (!t.startsWith("发消息") && !t.contains("按住说话")
                    && !t.contains("禁言") && !t.contains("请输入")) {
                continue;
            }
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            if (b.top >= bottomZone) {
                return true;
            }
        }
        return false;
    }

    /** 找搜索入口：先认 input 的提示语，再认带「搜索」字样的可点节点。 */
    private static AccessibilityNodeInfo findSearchEntry(AccessibilityService svc) {
        AccessibilityNodeInfo root = safeRoot(svc);
        if (root == null) {
            return null;
        }
        AccessibilityNodeInfo byInput = null;
        AccessibilityNodeInfo icon = null;
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        int topLimit = screen.height() <= 0 ? 400 : (int) (screen.height() * 0.2);
        // ① 最高优先级：直接认钉钉消息列表顶部搜索按钮的 viewId。
        //
        //    真机（2026-10-20，钉钉 9.0.2）实测 id = header_search_btn，
        //    text 恰好是「搜索」。为什么 viewId 优先于文字匹配：AI 助理页、
        //    聊天页、搜索历史页上都可能出现 text ==「搜索」的节点
        //    （AI 入口气泡、输入框提示语……），点错一个就会掉进
        //    AI 助理对话 —— 把群名写进 AI 输入框，等来的不是搜索结果，
        //    而是「正在理解你的问题」。表现就是 watch 报「搜索里没找到」，
        //    而手机停在 AI 页（2026-10-20 19:09 真机复现）。
        for (AccessibilityNodeInfo n : flatten(root)) {
            CharSequence rid = n.getViewIdResourceName();
            if (rid != null && rid.toString().endsWith("header_search_btn")) {
                return n.isClickable() ? n : clickableAncestor(n, 4);
            }
        }
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
            // 真机（钉钉 9.0.2 / PAL-AL00）主界面的搜索入口是个**图标字体**按钮：
            // text 是私有区乱码，只有 content-desc = "搜索"，自己不可点，
            // 可点是外面那层 FrameLayout。所以这里必须**不看 text**，
            // 只认「content-desc 恰好是『搜索』+ 在屏幕顶部」。
            // 不加这一条，主界面上根本找不到任何入口 —— 这正是
            // 2026-10-20「按钮点了没反应」的最后一环。
            if (icon == null && s.equals("搜索")
                    && n.getContentDescription() != null
                    && TextUtils.isEmpty(n.getText())) {
                Rect b = new Rect();
                n.getBoundsInScreen(b);
                if (b.top >= 0 && b.top <= topLimit) {
                    AccessibilityNodeInfo c = clickableAncestor(n, 4);
                    if (c != null) {
                        icon = c;
                    }
                }
            }
        }
        // 顶部图标优先于「文本里含搜索」的兜底：后者在会话列表页会把
        // 某个带「搜索」二字的会话行当成入口点进去。
        return icon != null ? icon : byInput;
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

    /**
     * 在节点矩形**靠左 20%** 的位置单击。
     *
     * <p>为什么不用中心点（2026-10-20 真机教训）：钉钉 9.0.2 的搜索结果行
     * 内部有两个命中区 —— 左边（x≈300）进会话，右边/中间（x≈610，正好是
     * 行中心）开群资料 H5。肉眼看都是「点了这一行」，结果完全不同。
     * 点名字文本的左端永远落在会话命中区里。
     */
    private static boolean tapLeft(AccessibilityService svc, AccessibilityNodeInfo node) {
        if (svc == null || node == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.N) {
            return false;
        }
        Rect b = new Rect();
        node.getBoundsInScreen(b);
        if (b.width() <= 0 || b.height() <= 0) {
            return false;
        }
        float x = b.left + b.width() * 0.20f;
        float y = b.exactCenterY();
        Path p = new Path();
        p.moveTo(x, y);
        GestureDescription gd = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, TAP_MS))
                .build();
        try {
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "注入点击失败：" + t);
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
