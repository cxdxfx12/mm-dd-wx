package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Environment;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.File;
import java.io.FilenameFilter;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/**
 * 附件抓取：看到卡片里的「附件：运单明细」→ 点它 → 等钉钉下载 → 传回电脑
 * → **立刻回到钉钉聊天页**。
 *
 * <p>真机依据（2026-10-27 探测，PHQ0222C07004266 / Android 12）：
 * <ol>
 *   <li>点卡片里「附件：运单明细」文本 → 钉钉内置 UC 内核（com.UCMobile）
 *       自动打开并<b>静默下载，全程无确认框</b>；</li>
 *   <li>文件落在<b>公开目录</b> {@code /sdcard/Download/UCDownloads/}
 *       （固定名 export.xlsx，重名会变 export(1).xlsx —— 按 mtime 取新的）；</li>
 *   <li>退出预览要按 4 次返回 → 弹「退出UC吗？」→ 点「退出」→ 落在钉钉的
 *       WebView 层 → 再退一次才回到聊天页。所以恢复流程是<b>循环</b>而不是
 *       固定次数。</li>
 * </ol>
 *
 * <p><b>用户定的铁律：下载完（或超时）必须马上回聊天页</b>——在 UC 里待着的
 * 每一秒钉钉推送都收不到。所以轮询文件一出现就退出预览，绝不停留；
 * 超时也一样退出。恢复失败时用现成的巡检兜底（搜索框拉回目标群）。
 *
 * <p>这个类是「听话」的：什么时候抓（哪条附件、冷却）由电脑侧
 * （android_gateway.py 的 fetch_attach 指令）决定，手机只执行。
 */
final class AttachmentFetcher {

    private static final String TAG = "DDWX";
    private static final String DINGTALK_PKG = "com.alibaba.android.rimet";

    /** 钉钉把附件下到这里。UC 内核的默认下载目录，公开、可读。 */
    private static final File DOWNLOAD_DIR = new File(
            Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS), "UCDownloads");

    /** 等文件出现的最长时间。实测 4KB 文件 2 秒内落盘；35 秒容忍慢网 +
     *  「下载文件」弹层慢速弹出（2026-11-01 15:50 真机 20 秒不够）。 */
    private static final long WAIT_MS = 35000L;
    private static final long POLL_MS = 600L;
    /** 点击时刻往前的容差：下载完成时间可能略早于我们记录的时刻。 */
    private static final long MTIME_SLACK_MS = 2000L;
    /** 恢复循环的最多步数。实测 6 步（4 返回 + 1 退出 + 1 返回）；留余量。 */
    private static final int RESTORE_MAX_STEPS = 12;
    /** 「立即下载」最多主动点几次（弹层偶尔要点第二次才生效）。 */
    private static final int NUDGE_MAX = 2;
    /** 两次 nudge 的最小间隔（毫秒）：连点会把下载取消重下。 */
    private static final long NUDGE_SPACING_MS = 3000L;
    /** 屏上找不到附件行时，向上翻屏找的次数（每翻一次 ≈ 0.6 屏）。
     *  长卡片刚到时视口停在卡片底部，「附件：xxx」在上方 —— 2026-11-01
     *  15:35 真机实锤「屏上找不到附件行」就是这么来的。 */
    private static final int ATTACH_SCROLL_TRIES = 3;
    /** 锚点滚动（ACTION_SCROLL_*）最多几屏：它比坐标滑动可靠，可以多给几屏。 */
    private static final int ANCHOR_SCROLL_TRIES = 6;
    /** 像素级精确滚动最多几次（每次都会重新 dump 校正距离）。 */
    private static final int PRECISE_TRIES = 6;
    /** 精确滚动的单次手势时长：太短会被当成快滑（惯性），太长会卡顿。 */
    private static final long DRAG_MS = 420L;
    /** 第一次找不到后再等一等（布局/自动滚动未必停稳）。 */
    private static final long ATTACH_FIND_SETTLE_MS = 800L;

    /**
     * 截屏找「可点行」需要的最低系统版本：{@code takeScreenshot} 是 API 30
     * （Android 11）才有的。本机 Android 12（API 31），可用。
     */
    private static final boolean CAN_SHOT =
            android.os.Build.VERSION.SDK_INT >= 30;

    /**
     * 颜色条带的门槛。真机 2026-11-01 实测（1176 宽）：
     *   · 蓝字「明细：普通件滞留催提AI代打明细」链接行 w=567、px≈7000 —— 通过；
     *   · 蓝字「复制」按钮 w=87、「内部群」标签 w=87 —— 被宽度挡掉；
     *   · 绿字「下载」胶囊按钮 w≈258 —— 通过。
     * 宽度卡 180 是为了把「复制」这类短按钮排除在蓝字兜底之外；绿色按钮
     * 只在预览页里找，卡 150 即可。
     */
    private static final int BLUE_MIN_WIDTH = 180;
    private static final int BLUE_MIN_PX = 400;
    private static final int GREEN_MIN_WIDTH = 150;
    private static final int GREEN_MIN_PX = 300;

    /**
     * 行文本在树上但坐标是坏的（H5 卡片重绘抖动）时，重新 dump 的次数与间隔。
     * 8 × 320ms ≈ 2.6 秒，够跨过绝大多数抖动窗口 —— 2026-11-01 实测同一行
     * 在 1 秒内就能从 h=-140 恢复成 h=+71。
     */
    private static final int BOUNDS_RETRY = 8;
    private static final long BOUNDS_RETRY_MS = 320L;

    /**
     * 以格式标记收尾的「下载按钮」文案：「查看完整履约失败明细（Excel）」。
     * 见 {@link #isAttachLine(String)} 的兜底分支 —— 字眼表没覆盖时靠它认出来。
     */
    private static final Pattern FORMAT_TAIL = Pattern.compile(
            "[（(【\\[]\\s*(?i:excel|xlsx?|xlsm|word|docx?|pdf|csv|zip|rar|7z"
            + "|txt|pptx?|图片|文件)\\s*[）)】\\]]\\s*$");

    /**
     * 树遍历的深度上限，必须与 {@link UiTree#MAX_DEPTH} 对齐（40）。曾经是 30
     * —— 结果附件卡片在 WebView 树里深度 31+，采集端序列化看得见（上限 40）、
     * 这边永远找不到，表现为「屏上找不到附件行，放弃本次」（2026-10-27 真机）。
     */
    private static final int MAX_WALK_DEPTH = 40;

    /** 触发字眼默认值，与电脑端 DEFAULT_ATTACH_KEYS 一致（dingtalk.attach_keys）。 */
    private static final List<String> DEFAULT_KEYS =
            java.util.Arrays.asList("附件：", "附件:");

    /** 实际使用的触发字眼（电脑端随指令下发；没带就用默认）。 */
    private final List<String> keys;

    /**
     * 本次要**依次尝试**的候选行（下标 0 最像附件）。
     *
     * <p>为什么是列表：用户 2026-11-01 要求「只要带明细字眼的都点开转发原文件」。
     * 钉钉卡片里「明细」两字会出现在三种行上 —— 真附件行、卡片底部
     * 「查看完整…（Excel）」按钮、以及「履约失败明细（10票）」这种<b>表格标题</b>。
     * 电脑侧没法百分百分辨，于是把候选排序后一起下发，这里逐个点、
     * <b>拿到文件就停</b>：标题行点空了只是白等几秒，不会把垃圾当附件传回去。
     */
    private final List<String> cands;

    /**
     * 非最后一个候选的等待时长。
     *
     * <p>真附件行点了通常 2～4 秒就开始下载；表格标题点了则<b>什么都不会发生</b>。
     * 每个候选都等满 {@link #WAIT_MS} 的话，一张卡 4 个候选要耗掉两分多钟，
     * 期间 {@code attachBusy} 一直占着、巡检全让路。所以前面的只给 9 秒，
     * 最后一个（才可能是真附件）给足 35 秒。
     */
    private static final long CAND_WAIT_MS = 9000L;

    /** 最近一次附件抓取的一句话状态。设置页/日志看这里。 */
    public static volatile String lastStatus = "尚未抓取过附件";

    /** 执行中的标志。抓取期间巡检必须让路（两个都在动界面会互相点掉对方）。 */
    private final AtomicBoolean busy;

    /** 为了找附件行向上翻了几屏（抓完要翻回去，见 {@link #scrollBackToBottom}）。 */
    private int scrolledUp = 0;
    /** 为了找附件行**向下**翻了几屏（附件行在卡片底部时必需）。 */
    private int scrolledDown = 0;
    /**
     * 上一次「文本在树上但坐标拿不到」时，那行相对视口的方向：
     * {@code -1} 在上方、{@code +1} 在下方、{@code 0} 不清楚。
     * 翻屏方向靠它决定 —— 猜错方向就是白翻 3 屏还找不到（见 {@link #invertDir}）。
     */
    private int missDir = 0;
    /**
     * 命中附件行时那个**节点本身**。点击优先走无障碍 {@code ACTION_CLICK}：
     * 坐标点击要依赖 bounds 算中心点，而 H5 卡片这一行的节点宽度往往远大于
     * 文字实际宽度（`[212,5417][964,1913]` 宽 752px），中心点可能落在文字的
     * 右侧空白里 —— 点了等于没点，然后白等 35 秒（2026-11-01 18:31 实锤）。
     */
    private AccessibilityNodeInfo hitNode = null;
    /**
     * 命中文本、但坐标被钳掉时那行**自己的节点**，以及离它最近的
     * {@code isScrollable()} 祖先。
     *
     * <p>为什么要祖先：0.5.29 真机实锤，坐标全坏时用 {@code Scroller.swipe}
     * 在外层列表上划 3 屏，屏幕上什么都没动（下一屏首行文本和上一屏一模一样），
     * 于是「已上翻 3 屏」其实是白翻 —— 手势落在了不接收滚动的容器上。
     * 无障碍的 {@code ACTION_SCROLL_FORWARD/BACKWARD} 直接派发给**目标自己
     * 所在的那个滚动容器**，不依赖手势落点，这才是该用的方式。
     */
    private AccessibilityNodeInfo missAnchor = null;
    private AccessibilityNodeInfo missScroller = null;
    /**
     * 目标行离可视区还有**多少像素**：{@code >0} 要往下滚（目标在下方），
     * {@code <0} 往上滚。这是 0.5.30 之后才想明白的数。
     *
     * <p>真机 19:07 实锤：目标行 {@code top=2198}、列表底 {@code 2018}，
     * 只差 180px，可一屏 {ACTION_SCROLL_FORWARD} 直接推 1600px，把目标
     * 甩到屏幕上方去了 —— 于是"翻了 4 屏还没找到"，其实每次都精准越过。
     * 滚动必须按像素走，不能按屏走。
     */
    private int missNeedPx = 0;
    /** 每翻一屏记一条「方向+首行文本」，失败时拼进状态，一眼看出到底滚没滚。 */
    private final List<String> scrollTrace = new ArrayList<>();
    /** 「立即下载」已经主动点了几次、上次是什么时候。 */
    private int nudges = 0;
    private long lastNudgeAt = 0L;
    /**
     * 原生预览标志：等文件期间看到钉钉原生预览的「转发另存/复制链接」
     * 弹窗就置位 —— 这条路**给不了文件**（转发出去的是链接不是文件，
     * 2026-11-01 20:0x 真机实验），立刻收队，不再等满 35 秒。
     * 每个候选开始时重置。
     */
    private boolean nativePreview = false;
    /** 蓝字兜底最近一次的观察记录（失败时拼进状态，方便远程判断）。 */
    private String blueTrace = "";

    private AttachmentFetcher(AtomicBoolean busy, List<String> keys,
                              List<String> cands) {
        this.busy = busy;
        this.keys = (keys == null || keys.isEmpty()) ? DEFAULT_KEYS : keys;
        this.cands = (cands == null) ? new ArrayList<String>() : cands;
    }

    // ------------------------------------------------------------------ 入口

    /**
     * 收到电脑侧 fetch_attach 指令时调用。已有抓取在跑就直接放弃 ——
     * 电脑侧有 10 分钟冷却，并发抓取只可能是异常重叠，宁可少抓一份
     * （卡片重渲染还会再给机会）也不冒两个流程互相干扰的险。
     */
    static void fetch(final CollectorService svc, final String token,
                      final String name, final List<String> keys,
                      final List<String> cands) {
        if (!svc.attachBusy.compareAndSet(false, true)) {
            Log.w(TAG, "附件抓取已在进行，忽略本次指令：" + name);
            return;
        }
        svc.attachBusySince = System.currentTimeMillis();   // 让路宽限期计时
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                AttachmentFetcher f = new AttachmentFetcher(svc.attachBusy, keys, cands);
                try {
                    f.run(svc, token, name);
                } catch (Throwable e) {
                    lastStatus = "附件抓取异常：" + e;
                    Log.w(TAG, "附件抓取异常：" + name, e);
                    f.restoreQuietly(svc);          // 异常也必须回聊天页
                } finally {
                    svc.attachBusy.set(false);
                    svc.attachBusySince = 0L;
                }
            }
        }, "ddwx-attach");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------------ 主流程

    private void run(CollectorService svc, String token, String name) {
        // ① 目录可读吗？Android 11+ 没授「所有文件访问」时这里就会失败 ——
        //    那就整个功能休眠，绝不能让消息转发跟着受影响。
        Map<String, long[]> before = snapshotDir();
        if (before == null) {
            lastStatus = "读不到 " + DOWNLOAD_DIR.getPath()
                    + "（缺「所有文件访问」授权？）附件功能休眠";
            Log.w(TAG, lastStatus);
            return;                              // 没点击就没离开聊天页，无需恢复
        }

        // ①′ 先确认人在目标聊天页。用户可能正点开某张卡片的 H5 预览在看，
        //     这时屏上没有附件行 —— 旧逻辑直接「找不到就放弃」，白白烧掉
        //     一次下单（卡片还会再派第二次，来回跳页）。先把人拉回群再干活：
        //     restoreToChat 对「已在聊天页」是幂等的，代价只有一次判树。
        if (!inTargetChat(svc.getRootInActiveWindow())) {
            Log.i(TAG, "抓取开始前不在目标群，先回聊天页");
            restoreQuietly(svc);
            try {
                Thread.sleep(1200);          // 等切换动画，别在马上下结论
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            if (!inTargetChat(svc.getRootInActiveWindow())) {
                lastStatus = "不在目标群页面（回不到聊天页），放弃本次："
                        + clip(name);
                Log.w(TAG, lastStatus);
                return;
            }
        }

        // ② 候选链（用户 2026-11-01：「只要带明细字眼的都点开转发原文件」）。
        //    一张卡里可能既有「查看完整履约失败明细（Excel）」真按钮，又有
        //    「履约失败明细（10票）」表格标题。电脑侧排不出绝对正确的顺序，
        //    就把"像附件的行"排好序下发，这里挨个点：
        //      · 拿到文件 → 立刻上传收工（后面的候选不再碰）；
        //      · 点空了   → 收回聊天页，点下一行。
        //    每点一个都把结论记进 tried，失败时直接上报，不用再装包试。
        List<String> plan = new ArrayList<>();
        for (String c : cands) {
            if (c != null && !c.trim().isEmpty()) {
                plan.add(c.trim());
            }
        }
        if (plan.isEmpty()) {
            plan.add(name);
        }
        StringBuilder tried = new StringBuilder();
        for (int i = 0; i < plan.size(); i++) {
            String cand = plan.get(i);
            boolean lastCand = (i == plan.size() - 1);
            long waitMs = lastCand ? WAIT_MS : CAND_WAIT_MS;

            if (i > 0) {
                // 上一个候选可能点开了预览页/下载页（也可能什么都没发生）：
                // 不收回聊天页，下一行连找都找不到。
                if (!inTargetChat(svc.getRootInActiveWindow())) {
                    restoreQuietly(svc);
                    sleep(900);
                }
            }
            if (plan.size() > 1) {
                Log.i(TAG, "尝试候选 " + (i + 1) + "/" + plan.size() + "：" + cand);
            }

            // 找到屏上的节点。找不到先等布局停稳再找，还没有就按方向翻屏
            // （长卡片的附件行可能在视口上下很远的地方，坐标还会被钳）。
            Rect bounds = findAttachBoundsSearching(svc, cand);
            // 蓝字兜底（0.5.33）：文本找不到行时，截屏找「蓝色可点行」。
            // 用户 2026-11-01：卡片里的附件/明细链接全是蓝字。H5 页面的
            // 节点树常常导不出文本（自绘），但像素骗不了人 —— 蓝行就是
            // 那个按钮。宽 <180 的蓝行（「复制」w=87、「内部群」w=87）跳过。
            Rect blueB = null;
            if (bounds == null && CAN_SHOT) {
                blueB = firstBlueRow(svc);
            }
            if (bounds == null && blueB == null) {
                tried.append(cand).append("：没找到（上翻").append(scrolledUp)
                        .append("/下翻").append(scrolledDown).append("屏")
                        .append(blueTrace.isEmpty() ? "" : "；" + blueTrace)
                        .append("）；");
                Log.w(TAG, "候选「" + cand + "」屏上找不到，换下一个");
                continue;
            }
            boolean viaBlue = (bounds == null);

            long tapAt = System.currentTimeMillis();
            nudges = 0;                       // 每个候选独立计「立即下载」点击次数
            lastNudgeAt = 0L;
            nativePreview = false;            // 每个候选独立判定「原生预览」
            // 先试无障碍点击：直接对节点派发 ACTION_CLICK，完全不依赖坐标，
            // 绕开「节点宽 752px、文字只占左边一半，中心点戳在空白处」的问题。
            boolean clicked = false;
            if (hitNode != null) {
                try {
                    if (hitNode.isClickable()) {
                        clicked = hitNode.performAction(
                                AccessibilityNodeInfo.ACTION_CLICK);
                        if (clicked) {
                            Log.i(TAG, "无障碍点击命中附件行「" + cand + "」");
                        }
                    }
                } catch (Throwable t) {
                    Log.w(TAG, "无障碍点击抛异常，退回坐标点击：" + t);
                }
            }
            if (!clicked) {
                Rect aim = (bounds != null) ? bounds : blueB;
                clicked = tap(svc, aim.exactCenterX(), aim.exactCenterY());
            }
            if (!clicked) {
                tried.append(cand).append("：点击被拒；");
                Log.w(TAG, "候选「" + cand + "」点击被系统拒绝，换下一个");
                restoreQuietly(svc);
                continue;
            }
            Log.i(TAG, "已点击「" + cand + "」"
                    + (viaBlue ? "（蓝字兜底 y=" + blueB.top + "-" + blueB.bottom + "）" : "")
                    + "，等文件落盘（最多 " + waitMs / 1000 + " 秒）…");

            // ③ 轮询等新文件，一出现就立刻走人。预览页可能先弹「立即下载」，
            //    waitForNewFile 会顺手点掉（见该方法注释）。
            File file = waitForNewFile(svc, before, tapAt, waitMs);
            if (file == null) {
                tried.append(cand).append("：")
                        .append(nativePreview ? "原生预览拿不到文件" : waitMs / 1000 + "秒无文件")
                        .append("；");
                lastStatus = "候选「" + cand + "」"
                        + (nativePreview ? "是钉钉原生预览（这条路只有转发/链接，没有文件下载）"
                                         : "等了 " + waitMs / 1000 + " 秒没见到新文件");
                Log.w(TAG, lastStatus);
                restoreQuietly(svc);          // 点开的预览页必须收回来
                continue;
            }

            // ④ 读文件 + 上传。名字用**真正点中的那一行**：用户要的就是
            //    「屏上写什么就叫什么」，这样微信里收到的文件名和钉钉对得上。
            //    蓝字兜底点中的行读不到文本，用落盘文件名最诚实。
            String displayName = viaBlue ? file.getName() : cand;
            byte[] data;
            try {
                data = java.nio.file.Files.readAllBytes(file.toPath());
            } catch (Throwable e) {
                // 老设备没有 java.nio.file.Files（API 26 以下）——手读兜底
                data = readBytes(file);
            }
            if (data == null || data.length == 0) {
                tried.append(cand).append("：文件空；");
                lastStatus = "文件读不出来：" + file.getName();
                Log.w(TAG, lastStatus);
                restoreQuietly(svc);
                continue;
            }
            Uploader.Result r = Uploader.postFile(
                    Prefs.host(svc), Prefs.port(svc), token,
                    displayName, sha1(data), data);
            lastStatus = stamp() + " · " + (r.ok ? "已上传 " : "上传失败 ")
                    + file.getName() + "（" + data.length + " 字节）· " + r.detail
                    + (tried.length() == 0 ? "" : " · 之前试过：" + clip(tried.toString()));
            Log.i(TAG, "附件「" + displayName + "」：" + lastStatus);

            // ⑤ 马上回聊天页（用户定的：不要在预览里多待一秒）。
            boolean back = restoreToChat(svc);
            if (back) {
                svc.onAttachRestored();
            } else {
                // 兜底：巡检会把钉钉拉回目标群（走搜索框那条成熟老路）。
                Log.w(TAG, "附件后自动返回失败，交给巡检拉回");
                svc.runWatchNowPublic("附件返回");
            }
            return;
        }

        // 所有候选都点过、一个文件都没来。把每条的结论报上去 —— 这是下一轮
        // 排查的唯一线索（哪一行没找到、哪一行点了没文件）。
        lastStatus = "候选都试过了没拿到文件（" + clip(tried.toString())
                + (scrollTrace.isEmpty() ? "" : "；翻屏：" + clip(String.join(",", scrollTrace)))
                + "）";
        Log.w(TAG, lastStatus);
        Log.w(TAG, "现场：\n" + diagAttach(svc));
        // ⚠ 2026-11-01 实锤：派单时手机未必在聊天页（可能停在 H5/UC 下载页）。
        // 停在 H5 时放弃不恢复，手机永远卡在网页里，之后采集什么都看不见。
        restoreQuietly(svc);
    }

    /** 流程中途出异常时的恢复：尽力而为，不抛。 */
    private void restoreQuietly(CollectorService svc) {
        try {
            if (restoreToChat(svc)) {
                svc.onAttachRestored();
            } else {
                svc.runWatchNowPublic("附件返回");
            }
        } catch (Throwable e) {
            Log.w(TAG, "附件恢复现场失败：" + e);
        }
    }

    // ------------------------------------------------------------------ 找节点

    /**
     * 找附件行：先就地找（找不到等布局停稳再找一次），还没有就**翻屏**找。
     *
     * <p>为什么必须翻：无障碍树只给可见节点。长卡片刚发出来时聊天视口停在
     * 卡片<b>顶部</b>，「查看完整…（Excel）」这类按钮在卡片<b>底部</b>，
     * 树里根本没有它。旧实现只会「手指往下划看更早内容」，方向反了就永远
     * 找不到 —— 2026-11-01 18:07 履约晚报实锤：附件行在被钳成
     * {@code [212,3290][964,2018]}（在视口<b>下方</b> 1 屏多），手机却往上
     * 翻了 3 屏跑到上一条「拦截跟单日报」上，最后「等了 35 秒没见到新文件」。
     *
     * <p>现在的顺序：钳制坐标明确指向下方时先下翻、再上翻；其余情况先上翻、
     * 再下翻。两边都翻是因为「钳制坐标只能猜个大概」，但**绝不重复翻同一边**。
     */
    private Rect findAttachBoundsSearching(AccessibilityService svc, String name) {
        missDir = 0;
        missAnchor = null;
        missScroller = null;
        missNeedPx = 0;
        scrollTrace.clear();
        Rect r = findAttachBounds(svc, name);
        if (r != null) {
            return r;
        }
        try {
            Thread.sleep(ATTACH_FIND_SETTLE_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        }
        r = findAttachBounds(svc, name);
        if (r != null) {
            return r;
        }
        if (!Scroller.canSwipe(svc)) {
            return null;
        }
        // 方向提示只在「文本在树上、坐标被钳」时才有；真不在屏上（hint=0）时
        // 先**上翻**：历史消息的附件按钮在更早处，这是 0.5.27 之前验证过的
        // 主路径（2026-11-01 15:35 拦截跟单日报）。往下翻只在钳制坐标明确
        // 指向下方时才先做 —— 否则像 18:3x 那样先下翻 3 屏、翻到别的消息上，
        // 白白错过目标行。
        int hint = missDir;
        // ⓪ 按像素精确滚动：差多少滚多少，滚完重新 dump 校正（闭环收敛）。
        //    0.5.30 的教训是「按屏滚」在目标只差百来像素时会百分百越过它。
        if (Math.abs(missNeedPx) >= 60) {
            r = preciseScroll(svc, name, missNeedPx);
            if (r != null) {
                return r;
            }
        }
        // ① 锚点滚动：坐标坏掉时我们知道**那行自己的**节点和它的滚动容器，
        //    直接派发 ACTION_SCROLL_* 最可靠（不依赖手指落点）。
        if (missScroller != null) {
            r = anchorScroll(svc, name, hint >= 0 ? +1 : -1, ANCHOR_SCROLL_TRIES);
            if (r != null) {
                return r;
            }
            if (hint != 0) {
                r = anchorScroll(svc, name, hint > 0 ? -1 : +1, ANCHOR_SCROLL_TRIES);
                if (r != null) {
                    return r;
                }
            }
        }
        if (hint > 0) {                      // 明确在下方：先下后上
            r = scrollFind(svc, name, +1, ATTACH_SCROLL_TRIES);
            return r != null ? r : scrollFind(svc, name, -1, ATTACH_SCROLL_TRIES);
        }
        r = scrollFind(svc, name, -1, ATTACH_SCROLL_TRIES);
        if (r != null) {
            return r;
        }
        return scrollFind(svc, name, +1, ATTACH_SCROLL_TRIES);
    }

    /**
     * 朝一个方向翻屏找附件行。
     *
     * @param dir {@code +1} = 往更新处翻（手指上划），{@code -1} = 往更早处翻
     */
    private Rect scrollFind(AccessibilityService svc, String name, int dir, int tries) {
        boolean down = dir > 0;
        for (int i = 0; i < tries; i++) {
            AccessibilityNodeInfo list = findScrollable(svc.getRootInActiveWindow());
            if (list == null) {
                break;
            }
            if (!Scroller.swipe(svc, list, down)) {
                break;
            }
            Scroller.settle();
            if (down) {
                scrolledDown++;
            } else {
                scrolledUp++;
            }
            Rect r = findAttachBounds(svc, name);
            if (r != null) {
                Log.i(TAG, (down ? "下" : "上") + "翻第 "
                        + (down ? scrolledDown : scrolledUp) + " 屏后找到附件行");
                scrollTrace.add((down ? "下" : "上") + "翻第" + (i + 1) + "屏命中");
                return r;
            }
            scrollTrace.add((down ? "下" : "上") + "翻第" + (i + 1) + "屏:"
                    + firstText(svc));
        }
        return null;
    }

    /** 抓完（无论成败）把视口滚回最新处 —— 上翻过几屏就往回滚几屏。 */
    private void scrollBackToBottom(AccessibilityService svc) {
        // 净位移 = 下翻屏数 - 上翻屏数；>0 说明视口还停在起点上方，要下翻回来。
        int plan = scrolledUp - scrolledDown;
        int done = 0;
        for (int i = 0; i < plan; i++) {
            AccessibilityNodeInfo list =
                    findScrollable(svc.getRootInActiveWindow());
            if (list == null || !Scroller.swipe(svc, list, true)) {
                break;                  // true = 手指往上 = 回到更新的内容
            }
            Scroller.settle();
            done++;
        }
        Log.i(TAG, "附件抓取前上翻 " + scrolledUp + " 屏、下翻 " + scrolledDown
                + " 屏，已回滚 " + done + " 屏");
        scrolledUp = 0;
        scrolledDown = 0;
    }

    /** 找到消息列表那一层的可滚动节点（滚动要落在它身上才有效）。 */
    private static AccessibilityNodeInfo findScrollable(AccessibilityNodeInfo node) {
        if (node == null) {
            return null;
        }
        if (node.isScrollable()) {
            return node;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            AccessibilityNodeInfo hit = findScrollable(c);
            if (hit != null) {
                return hit;
            }
        }
        return null;
    }

    /**
     * 在当前树上找附件行文本节点，返回屏幕坐标。优先点中带附件名的那个。
     *
     * <p>⚠ 2026-11-01 现场实锤：钉钉 H5 卡片的节点坐标会在重绘时**剧烈抖动**——
     * 同一行「附件：运单明细」上一秒还是 {@code [212,569][964,640]}（h=71，正常），
     * 下一秒就变成 {@code [212,265][964,125]}（h=-140，top 被钳成聊天区顶端、bottom 变负）。
     * 手机恰好 dump 到坏帧时就会判「屏上找不到附件行」，然后去上翻、越翻越远，
     * 表现出来就是「必须人工把手机翻到那一行才行」。
     *
     * <p>所以这里区分两种「没找到」：
     * <ul>
     *   <li>树上连这行文本都没有 → 真的不在屏上，立刻返回 null（交给上翻）</li>
     *   <li>文本在、但坐标坏 → 只是这一帧画歪了，等一拍再 dump（抖动窗口通常几百毫秒）</li>
     * </ul>
     */
    private Rect findAttachBounds(AccessibilityService svc, String name) {
        String want = name == null ? "" : name;
        hitNode = null;
        for (int i = 0; i < BOUNDS_RETRY; i++) {
            AccessibilityNodeInfo root = svc.getRootInActiveWindow();
            if (root == null) {
                missDir = 0;
                return null;
            }
            Rect exact = new Rect();
            Rect fallback = new Rect();
            if (findAttachNode(root, want, exact, fallback, 0)) {
                return exact;
            }
            // ⚠ 只有「没指定名字」时才允许点 fallback（随便挑一行附件）。
            //
            // 2026-11-01 18:3x 血案：0.5.28 加了「（Excel）收尾即附件行」的兜底
            // 之后，抓「拦截风险明细.xlsx」时先往下翻了 3 屏，翻到最新那条晚报，
            // 屏上出现「查看完整履约失败明细（Excel）」—— 名字对不上、但 fallback
            // 记下了它，于是**点开的是另一个附件**，35 秒等不到 xlsx，报「等了 35
            // 秒没见到新文件」。指定了名字就必须点那一行，找不到宁可翻屏/放弃，
            // 绝不能"顺手点一个最像的"。
            if (want.isEmpty() && !fallback.isEmpty()) {
                return fallback;
            }
            Rect raw = new Rect();
            if (!probeAttachRect(root, want, raw, 0)) {
                missDir = 0;
                return null;                       // 文本都不在，重试没意义
            }
            // 文本在树上、坐标拿不到：分两种，处置完全不同
            //   · h<0  → **倒挂**。超长 H5 卡片把视口外的行钳成
            //            top=内容真实坐标 / bottom=列表底（或反过来），
            //            这一帧的形状是稳定的，再 dump 也是这样 —— 重试
            //            纯属白等 2.6 秒，直接把方向告诉外层去翻屏。
            //   · h>=0 → 瞬时抖动（0 高/极小高），等一拍通常会恢复。
            int dir = invertDir(root, raw);
            if (dir != 0) {
                missDir = dir;
                AccessibilityNodeInfo anchor = findTextNode(root, want, 0);
                missAnchor = anchor;
                missScroller = scrollAncestorOf(anchor);
                missNeedPx = needPixels(raw, missScroller, dir);
                Log.i(TAG, "附件行在视口" + (dir < 0 ? "上方" : "下方")
                        + "（钳制坐标 " + raw.toShortString() + "，差 "
                        + missNeedPx + "px），容器="
                        + (missScroller == null ? "无" : missScroller.getClassName())
                        + "：" + want);
                return null;
            }
            if (i == BOUNDS_RETRY - 1) {
                break;
            }
            try {
                Thread.sleep(BOUNDS_RETRY_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
        Log.w(TAG, "附件行文本在树上但坐标一直是坏的（" + want + "），本次放弃");
        return null;
    }

    /**
     * 从「被钳制」的 bounds 猜这行的真实位置在视口哪一侧。
     *
     * <p>真机 2026-11-01 18:06 的履约晚报（超长 H5 卡片，约 3 屏高）同一帧里
     * 三种形态并存，这是判据的全部依据：
     * <pre>
     *   屏内正常行  [212,729][964,796]     h=+67
     *   上方已滚出  [212,265][964,-231]    h=-496   ← top 被钳到列表顶
     *   下方未滚到  [212,3290][964,2018]   h=-1272  ← bottom 被钳到列表底
     * </pre>
     * 拿不到真正的可视区（root 是整窗，含标题栏和输入框），所以只用一条
     * 粗判：倒挂时 {@code top} 落在屏幕下半 → 在下方，否则在上方。判错也
     * 不致命 —— {@link #findAttachBoundsSearching} 两个方向都会翻一遍。
     *
     * @return {@code -1} 上方、{@code +1} 下方、{@code 0} 不是倒挂
     */
    private static int invertDir(AccessibilityNodeInfo root, Rect b) {
        if (b.height() >= 0) {
            return 0;
        }
        Rect r = new Rect();
        root.getBoundsInScreen(r);
        // 先按「钳制形态」判：bottom<0 = 整行在屏上方；top>屏高 = 整行在屏下方。
        // 这比只看 top 落在上下半准（2026-11-01 18:47 真机：目标行 [212,5417][964,1913]，
        // 屏高 2423，两类判据结论一致，但钳制形态在极端值下更不容易歧义）。
        if (b.bottom < 0) {
            return -1;
        }
        if (r.height() > 0 && b.top > r.height()) {
            return 1;
        }
        int mid = r.top + r.height() / 2;
        return b.top > mid ? 1 : -1;
    }

    /** 按文本找节点（不看坐标），给「锚点滚动」用。 */
    private AccessibilityNodeInfo findTextNode(AccessibilityNodeInfo node,
                                               String name, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return null;
        }
        CharSequence t = node.getText();
        if (t != null) {
            String s = t.toString().trim();
            if (isAttachLine(s) && (name.isEmpty() || s.contains(name))) {
                return node;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            AccessibilityNodeInfo hit = findTextNode(c, name, depth + 1);
            if (hit != null) {
                return hit;
            }
        }
        return null;
    }

    /**
     * 离目标节点最近的 {@code isScrollable()} 祖先 —— 也就是**真正管着这行
     * 能不能滚进视口**的那个容器。消息列表、H5 卡片内的 WebView，
     * 谁在链上更近就是谁，不用猜层级。
     */
    private static AccessibilityNodeInfo scrollAncestorOf(AccessibilityNodeInfo n) {
        AccessibilityNodeInfo cur = n;
        for (int i = 0; cur != null && i < 12; i++) {
            try {
                if (cur.isScrollable()) {
                    return cur;
                }
                cur = cur.getParent();
            } catch (Throwable t) {
                return null;
            }
        }
        return null;
    }

    /**
     * 朝 {@code dir} 方向，把附件行滚进可视区。
     *
     * <p>走无障碍 {@code ACTION_SCROLL_FORWARD/BACKWARD} 而不是坐标滑动：
     * 手势要落在「能接收滚动的那块区域」才有效，H5 卡片这一行宽 752px、
     * 上方还压着别的东西，落点一旦偏了就是白划 —— 0.5.29 连续 3 屏原地不动的
     * 根因。动作直接派发给容器自己，没有落点问题。
     *
     * @return 找到的坐标；没找到返回 {@code null}
     */
    private Rect anchorScroll(AccessibilityService svc, String name, int dir, int tries) {
        for (int i = 0; i < tries; i++) {
            AccessibilityNodeInfo sc = missScroller;
            if (sc == null) {
                return null;
            }
            boolean ok;
            try {
                ok = sc.performAction(dir > 0
                        ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                        : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
            } catch (Throwable t) {
                ok = false;
            }
            if (!ok) {
                scrollTrace.add((dir > 0 ? "下" : "上") + "锚点滚不动");
                return null;
            }
            Scroller.settle();
            if (dir > 0) {
                scrolledDown++;
            } else {
                scrolledUp++;
            }
            Rect r = findAttachBounds(svc, name);        // 会重设 missScroller
            if (r != null) {
                scrollTrace.add((dir > 0 ? "下" : "上") + "锚点第" + (i + 1) + "屏命中");
                return r;
            }
            if (missScroller == null) {
                missScroller = sc;
            }
            scrollTrace.add((dir > 0 ? "下" : "上") + "锚点第" + (i + 1) + "屏:"
                    + firstText(svc));
        }
        return null;
    }

    /**
     * 目标行离可视区多少像素。
     *
     * <p>靠的是这条实证规律（2026-11-01 19:0x，屏上 116 个节点全列过）：
     * <pre>
     *   屏内正常行   [318,377][471,528]    h=+59，坐标可信
     *   屏上方行     [265,-303]            top 被钳成列表顶，bottom 是真值
     *   屏下方行     [2255,2018]           bottom 被钳成列表底，top 是真值
     * </pre>
     * 也就是说「超出视口的那一侧被钳到边界」，另一侧保留真实值 —— 拿真实值
     * 减边界就是精确的滚动距离，不用再猜"翻几屏"。
     */
    private static int needPixels(Rect raw, AccessibilityNodeInfo list, int dir) {
        if (list == null) {
            return dir > 0 ? 400 : -400;
        }
        Rect lb = new Rect();
        try {
            list.getBoundsInScreen(lb);
        } catch (Throwable t) {
            return dir > 0 ? 400 : -400;
        }
        if (lb.height() <= 0) {
            return dir > 0 ? 400 : -400;
        }
        int pad = 90;                      // 多滚一点，保证整行（约 60px）露出来
        int need;
        if (dir > 0) {                     // 在下方：让 top 进到列表底之上
            need = raw.top - lb.bottom + pad;
        } else {                           // 在上方：让 bottom 进到列表顶之下
            need = raw.bottom - lb.top - pad;
        }
        // 夹在 ±一屏内，避免一次滚太远（0.5.30 的教训：滚多了等于没找到）
        int cap = Math.max(200, lb.height() - pad * 2);
        if (need > cap) {
            need = cap;
        }
        if (need < -cap) {
            need = -cap;
        }
        if (Math.abs(need) < 60) {         // 太近了手滑不动，给个最小步长
            need = dir > 0 ? 80 : -80;
        }
        return need;
    }

    /**
     * 按像素精确滚动，把目标行推进可视区。
     *
     * <p>与 {@link #anchorScroll} 的区别：那个按「一屏」翻，目标是离屏很远时
     * 合适；这个是「差 180px 就滚 180px」。每滚一次重新 dump 一次，用最新的
     * 钳制坐标重算距离 —— 闭环收敛，不会越滚越远。
     */
    private Rect preciseScroll(AccessibilityService svc, String name, int need) {
        int last = Integer.MIN_VALUE;
        for (int i = 0; i < PRECISE_TRIES && need != 0; i++) {
            AccessibilityNodeInfo list = missScroller != null
                    ? missScroller : findScrollable(svc.getRootInActiveWindow());
            if (list == null) {
                return null;
            }
            Rect lb = new Rect();
            list.getBoundsInScreen(lb);
            if (lb.height() <= 0) {
                return null;
            }
            int step = Math.max(-lb.height() / 2, Math.min(lb.height() / 2, need));
            if (step == 0) {
                return null;
            }
            float x = lb.exactCenterX();
            float from;
            float to;
            if (step > 0) {                // 要看下方内容：手指自下而上划
                from = lb.bottom - 40;
                to = Math.max(lb.top + 60, from - step);
            } else {                       // 要看上方内容：手指自上而下划
                from = lb.top + 40;
                to = Math.min(lb.bottom - 40, from - step);
            }
            if (Math.abs(to - from) < 30) {
                scrollTrace.add("精滚位移为0");
                return null;
            }
            if (!drag(svc, x, from, x, to)) {
                scrollTrace.add("精滚手势失败");
                return null;
            }
            long dragAt = System.currentTimeMillis();
            Scroller.settle();
            Rect r = findAttachBounds(svc, name);
            if (r != null) {
                scrollTrace.add("精滚" + Math.round(to - from) + "px命中(用"
                        + (System.currentTimeMillis() - dragAt) + "ms)");
                return r;
            }
            scrollTrace.add("精滚" + Math.round(to - from) + "px后差" + missNeedPx + "px");
            if (missNeedPx == last) {      // 滚了但距离没变 = 这段内容根本不滚
                scrollTrace.add("滚不动放弃");
                return null;
            }
            last = missNeedPx;
            need = missNeedPx;
        }
        return null;
    }

    /** 从 (x1,y1) 拖到 (x2,y2) 的一次手势。 */
    private boolean drag(AccessibilityService svc, float x1, float y1,
                         float x2, float y2) {
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        GestureDescription gd = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, DRAG_MS))
                .build();
        try {
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "拖动派发失败：" + t);
            return false;
        }
    }

    /** 当前屏第一段非空文本（前 8 字），用来在状态里判断「到底滚没滚」。 */
    private static String firstText(AccessibilityService svc) {        try {
            AccessibilityNodeInfo root = svc.getRootInActiveWindow();
            String s = firstTextOf(root, 0);
            return s == null ? "?" : (s.length() > 8 ? s.substring(0, 8) : s);
        } catch (Throwable t) {
            return "?";
        }
    }

    private static String firstTextOf(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 14) {
            return null;
        }
        CharSequence t = n.getText();
        if (t != null) {
            String s = t.toString().trim();
            if (s.length() >= 2) {
                return s;
            }
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            String s = firstTextOf(n.getChild(i), depth + 1);
            if (s != null) {
                return s;
            }
        }
        return null;
    }

    /** 树上有没有这行文本（完全不看坐标），顺手把这行的 bounds 带出来。 */
    private boolean hasAttachText(AccessibilityNodeInfo node, String name, int depth) {
        return probeAttachRect(node, name, null, depth);
    }

    private boolean probeAttachRect(AccessibilityNodeInfo node, String name,
                                    Rect out, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return false;
        }
        CharSequence t = node.getText();
        if (t != null) {
            String s = t.toString().trim();
            if (isAttachLine(s) && (name.isEmpty() || s.contains(name))) {
                if (out != null) {
                    node.getBoundsInScreen(out);
                }
                return true;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            if (probeAttachRect(c, name, out, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    /** 这段文本是不是附件行：以任一触发字眼开头（允许「明细」前缀）。 */
    private boolean isAttachLine(String s) {
        if (s == null) {
            return false;
        }
        for (String k : keys) {
            if (k.isEmpty()) {
                continue;
            }
            if (s.startsWith(k) || s.startsWith("明细" + k)) {
                return true;
            }
        }
        // 兜底：以「（Excel）」「(PDF)」这类**格式标记**收尾的短行，就是卡片的
        // 下载按钮。2026-11-01 18:06 履约晚报的「查看完整履约失败明细（Excel）」
        // 就是这样一行 —— 它不以任何配置的字眼开头时（换了文案模板），
        // 只靠字眼表就整条漏抓。限制长度 ≤40，避免把说明句当按钮。
        return s.length() <= 40 && FORMAT_TAIL.matcher(s).find();
    }

    private boolean findAttachNode(AccessibilityNodeInfo node, String name,
                                   Rect exactOut, Rect fallbackOut, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return false;
        }
        CharSequence text = node.getText();
        if (text != null) {
            String s = text.toString().trim();
            if (isAttachLine(s)) {
                Rect b = new Rect();
                node.getBoundsInScreen(b);
                if (b.width() > 40 && b.height() > 10) {
                    if (name.isEmpty() || s.contains(name)) {
                        exactOut.set(b);
                        hitNode = node;          // 供 ACTION_CLICK 用（见 fetch）
                        return true;
                    }
                    if (fallbackOut.isEmpty()) {
                        fallbackOut.set(b);
                        hitNode = node;
                    }
                }
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            boolean found = findAttachNode(c, name, exactOut, fallbackOut, depth + 1);
            if (found) {
                return true;
            }
        }
        return false;
    }

    /**
     * 找不到附件行时的现场描述：根节点是谁、树里走了多少节点、所有和
     * 「附件」沾边的文本长什么样（含 content-desc —— 有些卡片把说明挂在
     * desc 上）。只为日志，不影响流程。
     */
    private String diagAttach(AccessibilityService svc) {
        AccessibilityNodeInfo root = svc.getRootInActiveWindow();
        if (root == null) {
            return "root=null（无障碍树拿不到 —— 钉钉不在前台？）";
        }
        StringBuilder sb = new StringBuilder();
        sb.append("root=").append(root.getPackageName()).append('/')
                .append(root.getClassName());
        int[] stat = new int[2];                    // 0=节点数 1=最大深度
        List<String> hits = new ArrayList<>();
        diagWalk(root, sb, stat, hits, 0);
        sb.append(" · 节点 ").append(stat[0]).append(" 个 · 最大深度 ")
                .append(stat[1]).append(" · 沾边 ").append(hits.size());
        for (String h : hits) {
            sb.append("\n  ").append(h);
        }
        return sb.toString();
    }

    private void diagWalk(AccessibilityNodeInfo node, StringBuilder sb,
                          int[] stat, List<String> hits, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH || stat[0] > 8000) {
            return;
        }
        stat[0]++;
        if (depth > stat[1]) {
            stat[1] = depth;
        }
        CharSequence text = node.getText();
        CharSequence desc = node.getContentDescription();
        String s = text == null ? "" : text.toString().trim();
        String d = desc == null ? "" : desc.toString().trim();
        if ((s.contains("附件") || d.contains("附件")) && hits.size() < 5) {
            Rect b = new Rect();
            node.getBoundsInScreen(b);
            hits.add("深度" + depth + " bounds" + b.toShortString()
                    + " text=" + clip(s) + " desc=" + clip(d));
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c != null) {
                diagWalk(c, sb, stat, hits, depth + 1);
            }
        }
    }

    private static String clip(String s) {
        if (s == null) {
            return "";
        }
        return s.length() > 60 ? s.substring(0, 60) + "…" : s;
    }

    // ------------------------------------------------------------------ 文件

    /** 像素判色器：true = 这个像素算「目标颜色」。 */
    private interface PixelJudge {
        boolean hit(int r, int g, int b);
    }

    /** 钉钉链接/附件行的蓝字（#1E7BFF 一族）。真机 2026-11-01 校准。 */
    private static final PixelJudge BLUE = new PixelJudge() {
        @Override
        public boolean hit(int r, int g, int b) {
            return b >= 150 && b - r >= 60 && b - g >= 30 && g >= 60 && r <= 160;
        }
    };

    /** 原生预览「下载」绿胶囊（#07C160 一族）。 */
    private static final PixelJudge GREEN = new PixelJudge() {
        @Override
        public boolean hit(int r, int g, int b) {
            return g >= 150 && g - r >= 60 && g - b >= 60;
        }
    };

    /** 截一整屏（API 30+，本机 Android 12）。失败/超时返回 null。 */
    private android.graphics.Bitmap takeShot(AccessibilityService svc) {
        if (!CAN_SHOT) {
            return null;
        }
        final java.util.concurrent.CountDownLatch latch =
                new java.util.concurrent.CountDownLatch(1);
        final android.graphics.Bitmap[] out = new android.graphics.Bitmap[1];
        try {
            svc.takeScreenshot(android.view.Display.DEFAULT_DISPLAY,
                    java.util.concurrent.Executors.newSingleThreadExecutor(),
                    new AccessibilityService.TakeScreenshotCallback() {
                        @Override
                        public void onSuccess(
                                AccessibilityService.ScreenshotResult res) {
                            try {
                                if (res != null) {
                                    android.graphics.Bitmap hw =
                                            android.graphics.Bitmap.wrapHardwareBuffer(
                                                    res.getHardwareBuffer(),
                                                    res.getColorSpace());
                                    if (hw != null) {
                                        out[0] = hw.copy(
                                                android.graphics.Bitmap.Config.ARGB_8888,
                                                false);
                                        hw.recycle();
                                    }
                                }
                            } catch (Throwable t) {
                                Log.w(TAG, "截图读取失败：" + t);
                            } finally {
                                try {
                                    if (res != null) {
                                        res.getHardwareBuffer().close();
                                    }
                                } catch (Throwable ignore) {
                                    // 关不闭环也不影响主流程
                                }
                                latch.countDown();
                            }
                        }

                        @Override
                        public void onFailure(int errorCode) {
                            Log.w(TAG, "takeScreenshot 失败 errorCode=" + errorCode
                                    + "（截太频/没权限时会走这）");
                            latch.countDown();
                        }
                    });
        } catch (Throwable t) {
            Log.w(TAG, "takeScreenshot 调用失败：" + t);
            return null;
        }
        try {
            if (!latch.await(3, java.util.concurrent.TimeUnit.SECONDS)) {
                return null;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        }
        return out[0];
    }

    /**
     * 在截图里找「目标颜色」的文字/按钮条带（从上到下）。
     *
     * <p>聚法与电脑端 .tmp/blue.py 完全一致（真机校准过的判据）：行内命中
     * ≥3 像素算「含色行」，含色行之间空 ≤6 行归并成一条带；条带总量与
     * 宽度都过门槛才算。只扫 y 260~2100 —— 顶部状态栏/标题栏和底部
     * 输入栏的杂色不参与。
     */
    private java.util.List<Rect> colorBands(android.graphics.Bitmap bmp,
                                            PixelJudge judge,
                                            int minWidth, int minPx) {
        java.util.List<Rect> out = new ArrayList<>();
        if (bmp == null) {
            return out;
        }
        int w = bmp.getWidth(), h = bmp.getHeight();
        int y0 = Math.min(260, h);
        int y1 = Math.min(h, 2100);
        if (y1 - y0 < 10) {
            return out;
        }
        int rows = y1 - y0;
        int[] pix = new int[w * rows];
        bmp.getPixels(pix, 0, w, 0, y0, w, rows);
        int[] cnt = new int[rows];
        int[] xs0 = new int[rows];
        int[] xs1 = new int[rows];
        for (int r = 0; r < rows; r++) {
            int c = 0, mn = -1, mx = -1;
            int base = r * w;
            for (int x = 0; x < w; x++) {
                int p = pix[base + x];
                if (judge.hit((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF)) {
                    c++;
                    if (mn < 0) {
                        mn = x;
                    }
                    mx = x;
                }
            }
            cnt[r] = c;
            xs0[r] = mn;
            xs1[r] = mx;
        }
        int start = -1, gap = 0, px = 0, mn = Integer.MAX_VALUE, mx = -1, last = -1;
        for (int r = 0; r <= rows; r++) {
            if (r < rows && cnt[r] >= 3) {
                if (start < 0) {
                    start = r;
                    px = 0;
                    mn = Integer.MAX_VALUE;
                    mx = -1;
                }
                gap = 0;
                last = r;
                px += cnt[r];
                if (xs0[r] >= 0 && xs0[r] < mn) {
                    mn = xs0[r];
                }
                if (xs1[r] > mx) {
                    mx = xs1[r];
                }
            } else if (start >= 0) {
                gap++;
                if (gap > 6 || r == rows) {
                    if (px >= minPx && mx - mn >= minWidth) {
                        out.add(new Rect(mn, y0 + start, mx + 1, y0 + last + 1));
                    }
                    start = -1;
                }
            }
        }
        return out;
    }

    /**
     * 蓝字兜底：找不到文本行时，返回屏上第一条够宽的蓝色可点行。
     * 同时把所有蓝行记进 {@link #blueTrace}，失败时拼进状态上报。
     */
    private Rect firstBlueRow(AccessibilityService svc) {
        android.graphics.Bitmap bmp = takeShot(svc);
        if (bmp == null) {
            blueTrace = "截图失败";
            return null;
        }
        java.util.List<Rect> bands = colorBands(bmp, BLUE, BLUE_MIN_WIDTH, BLUE_MIN_PX);
        bmp.recycle();
        if (bands.isEmpty()) {
            blueTrace = "无蓝行";
            return null;
        }
        StringBuilder sb = new StringBuilder("蓝行");
        for (Rect r : bands) {
            sb.append("[y").append(r.top).append("-").append(r.bottom)
                    .append(" x").append(r.left).append("-").append(r.right).append("]");
        }
        blueTrace = sb.toString();
        Log.i(TAG, blueTrace);
        return bands.get(0);
    }

    /**
     * 找原生预览页的绿色「下载」按钮：只认**右上区域**（y 200~900、
     * 左缘过屏幕中线）的绿色块 —— 群聊页里其它绿色元素几乎不可能落在这。
     */
    private Rect firstGreenButton(AccessibilityService svc) {
        android.graphics.Bitmap bmp = takeShot(svc);
        if (bmp == null) {
            return null;
        }
        int half = bmp.getWidth() / 2;
        java.util.List<Rect> bands = colorBands(bmp, GREEN, GREEN_MIN_WIDTH, GREEN_MIN_PX);
        bmp.recycle();
        for (Rect r : bands) {
            if (r.top >= 200 && r.bottom <= 900 && r.left >= half) {
                return r;
            }
        }
        return null;
    }

    /** 目录快照：文件名 → (长度, mtime)。读不了返回 null（= 没授权）。 */
    private Map<String, long[]> snapshotDir() {
        File[] files = listAttachFiles();
        if (files == null) {
            return null;
        }
        Map<String, long[]> out = new HashMap<>();
        for (File f : files) {
            out.put(f.getName(), new long[]{f.length(), f.lastModified()});
        }
        return out;
    }

    private File[] listAttachFiles() {
        if (!DOWNLOAD_DIR.exists() && !DOWNLOAD_DIR.mkdirs()) {
            return null;
        }
        File[] files = DOWNLOAD_DIR.listFiles(new FilenameFilter() {
            @Override
            public boolean accept(File dir, String n) {
                String lower = n.toLowerCase(Locale.US);
                return lower.endsWith(".xlsx") || lower.endsWith(".xls")
                        || lower.endsWith(".csv");
            }
        });
        return files;                            // 目录存在时 listFiles 失败才返回 null
    }

    /**
     * 等一份「新」文件：名字是新的，或 mtime 比点击时刻新。
     * 等的同时盯着 UC 下载页的「立即下载」按钮 —— 出现就点一次。
     */
    private File waitForNewFile(AccessibilityService svc,
                                Map<String, long[]> before, long tapAt,
                                long waitMs) {
        long deadline = System.currentTimeMillis() + waitMs;
        while (System.currentTimeMillis() < deadline) {
            File[] files = listAttachFiles();
            if (files != null) {
                File best = null;
                for (File f : files) {
                    long[] old = before.get(f.getName());
                    boolean isNew = old == null
                            || f.lastModified() != old[1] || f.length() != old[0];
                    if (isNew && f.lastModified() >= tapAt - MTIME_SLACK_MS) {
                        if (best == null || f.lastModified() > best.lastModified()) {
                            best = f;
                        }
                    }
                }
                if (best != null) {
                    return best;
                }
            }
            // 「立即下载」→ UC 还可能弹「…已有相同文件,确定重新下载?」
            // （2026-10-27 真机 + 用户现场确认）。两个都要接：先点下载按钮，
            // 再点确认框里的「确定」。不接的话文件永远下不来。
            Rect btn = new Rect();
            AccessibilityNodeInfo root = svc.getRootInActiveWindow();
            long now = System.currentTimeMillis();
            if (root == null) {
                // nothing
            } else if (nudges < NUDGE_MAX && now - lastNudgeAt >= NUDGE_SPACING_MS
                    && findContainsAnywhere(svc, "立即下载", btn)) {
                // 用 contains 而不是精确匹配：按钮节点文本常是「立即下载\n
                // 手机剩余空间 30.62GB」（两行同一 TextView），精确比对永远
                // 失败 —— 2026-11-01 14:20 真机实锤，20 秒空等就是这么来的。
                // ⚠ 允许点两次：弹层偶尔要点第二下才开始下（2026-11-01
                //   15:50 真机：等满 20 秒没文件，手动再点一下立刻就好）。
                Log.i(TAG, "预览页出现「立即下载」（第 " + (nudges + 1)
                        + " 次），点它");
                tap(svc, btn.exactCenterX(), btn.exactCenterY());
                nudges++;
                lastNudgeAt = now;
            } else if (containsText(root, "转发另存", 0, 0)
                    && containsText(root, "复制链接", 0, 0)) {
                // 0.5.33：钉钉**原生**预览的招牌弹窗（点绿色「下载」按钮后弹）。
                // 它给的是「转发另存（分享链接）/复制链接」，没有文件本体 ——
                // 2026-11-01 20:0x 真机实验：转发出去的是网址不是文件。
                // 在这里等满 35 秒纯属浪费队列，立刻收队。绿按钮本身由下面的
                // 截图分支负责点（它不在节点树里，自绘的）。
                nativePreview = true;
                Log.w(TAG, "原生预览弹窗（转发另存/复制链接）：这条路拿不到文件");
                return null;
            } else if (nudges < NUDGE_MAX && now - lastNudgeAt >= NUDGE_SPACING_MS
                    && findExactAnywhere(svc, "下载", btn)) {
                // 0.5.33：有的下载按钮文本就是「下载」两个整字（不是
                // 「立即下载」）。精确匹配避免把正文里含「下载」的长句点掉。
                Log.i(TAG, "预览页出现「下载」按钮（第 " + (nudges + 1) + " 次），点它");
                tap(svc, btn.exactCenterX(), btn.exactCenterY());
                nudges++;
                lastNudgeAt = now;
            } else if (CAN_SHOT && now - lastNudgeAt >= 1500L) {
                // 0.5.33：原生预览页整页自绘，节点树里**没有**「下载」按钮
                // （uiautomator 只能看到标题和「更多」）。按钮是绿色胶囊，
                // 截屏找绿色块直接点 —— 点了会弹「转发另存/复制链接」，
                // 由上面的原生预览分支立刻收队。万一有的卡片绿按钮真能
                // 直接下载，那就赚到文件了。
                Rect green = firstGreenButton(svc);
                if (green != null) {
                    Log.i(TAG, "截图发现绿色「下载」按钮 y=" + green.top
                            + "-" + green.bottom + "，点它");
                    tap(svc, green.exactCenterX(), green.exactCenterY());
                    lastNudgeAt = now;
                }
            } else if (findContainsAnywhere(svc, "确定", btn)
                    && (containsText(root, "已有相同文件", 0, 0)
                        || containsText(root, "文件已存在", 0, 0)
                        || containsText(root, "重新下载", 0, 0))) {
                Log.i(TAG, "UC 确认重新下载，点「确定」");
                tap(svc, btn.exactCenterX(), btn.exactCenterY());
            }
            try {
                Thread.sleep(POLL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
        return null;
    }

    /**
     * 在<b>所有</b>窗口里找含某文字的节点。
     *
     * <p>为什么不能只看 {@code getRootInActiveWindow()}：钉钉的「下载文件」
     * 底部弹层是<b>独立窗口</b>，活动窗口的树里未必有它 —— 2026-11-01
     * 15:50 真机：弹层明明开着（手动点「立即下载」立刻能下），程序却等满
     * 20 秒一个文件都没等到，就是 nudge 找错了地方。
     */
    private boolean findContainsAnywhere(AccessibilityService svc, String want,
                                         Rect out) {
        if (findContainsText(svc.getRootInActiveWindow(), want, out, 0)) {
            return true;
        }
        try {
            List<android.view.accessibility.AccessibilityWindowInfo> wins =
                    svc.getWindows();
            if (wins == null) {
                return false;
            }
            for (android.view.accessibility.AccessibilityWindowInfo w : wins) {
                AccessibilityNodeInfo r = w == null ? null : w.getRoot();
                if (r != null && findContainsText(r, want, out, 0)) {
                    return true;
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "遍历窗口找「" + want + "」失败：" + t);
        }
        return false;
    }

    /**
     * 在<b>所有</b>窗口里找文本**恰好等于** want 的节点（含 content-desc 的
     * 短按钮）。与 {@link #findContainsAnywhere} 同一套多窗口逻辑，只是
     * 比对用 {@code contentEquals} —— 0.5.33 的「下载」两字按钮用精确匹配，
     * 避免把正文里含「下载」的长句误当按钮点掉。
     */
    private boolean findExactAnywhere(AccessibilityService svc, String want,
                                      Rect out) {
        if (findExactTextOrDesc(svc.getRootInActiveWindow(), want, out, 0)) {
            return true;
        }
        try {
            List<android.view.accessibility.AccessibilityWindowInfo> wins =
                    svc.getWindows();
            if (wins == null) {
                return false;
            }
            for (android.view.accessibility.AccessibilityWindowInfo w : wins) {
                AccessibilityNodeInfo r = w == null ? null : w.getRoot();
                if (r != null && findExactTextOrDesc(r, want, out, 0)) {
                    return true;
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "遍历窗口找「" + want + "」失败：" + t);
        }
        return false;
    }

    /** {@link #findExactText} 的 desc 版：按钮常把文字放 content-desc 里。 */
    private boolean findExactTextOrDesc(AccessibilityNodeInfo node, String want,
                                        Rect out, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return false;
        }
        CharSequence text = node.getText();
        CharSequence desc = node.getContentDescription();
        boolean hit = (text != null && want.contentEquals(text.toString().trim()))
                || (desc != null && want.contentEquals(desc.toString().trim()));
        if (hit) {
            Rect b = new Rect();
            node.getBoundsInScreen(b);
            if (b.width() > 0 && b.height() > 0) {
                out.set(b);
                return true;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            if (findExactTextOrDesc(c, want, out, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    private byte[] readBytes(File f) {
        try {
            java.io.FileInputStream in = new java.io.FileInputStream(f);
            try {
                byte[] buf = new byte[(int) f.length()];
                int off = 0;
                while (off < buf.length) {
                    int n = in.read(buf, off, buf.length - off);
                    if (n < 0) {
                        break;
                    }
                    off += n;
                }
                return buf;
            } finally {
                in.close();
            }
        } catch (Throwable t) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 恢复现场

    /**
     * 从 UC 预览回到钉钉聊天页。返回 true = 已确认在目标群。
     *
     * <p>循环而不是固定步数：预览的层级会变（Observed：4 返回 → 退出确认框
     * → 钉钉 WebView 层 → 1 返回 → 聊天页）。每步先看「是不是已经在目标群」，
     * 是了立刻停 —— 最快路径就是 0 步（本来就回来了）。
     */
    private boolean restoreToChat(CollectorService svc) {
        for (int step = 0; step < RESTORE_MAX_STEPS; step++) {
            AccessibilityNodeInfo root = svc.getRootInActiveWindow();
            String pkg = root == null ? "" : String.valueOf(root.getPackageName());
            if (DINGTALK_PKG.equals(pkg)) {
                if (inTargetChat(root)) {
                    // 上翻找附件行挪动了视口：回聊天页后必须滚回最新处，
                    // 否则基线错位，下一条新消息会被判「锚点丢失」停转。
                    scrollBackToBottom(svc);
                    return true;
                }
                // 在钉钉但不是目标群（WebView 层/别的页面）→ 继续退。原来是
                // 「退一次不成就放弃」—— 钉钉内的层级不止一层，2026-10-31
                // 真机在 H5 层退一次后还停在群里嵌的网页页面上。循环里继续
                // 退（步数有 RESTORE_MAX_STEPS 封顶），退穿为止。
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK);
                sleep(900);
                continue;
            }
            // 还在 UC：先看有没有「退出UC吗？」确认框 —— 有就点「退出」，
            // 否则 BACK。⚠ 点第一张卡片的时代教训：这里必须按**文本精确找**，
            // 绝不能点屏幕上第一个可点的东西。
            if (root != null && tapExitIfDialog(root, svc)) {
                sleep(1200);
                continue;
            }
            svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK);
            sleep(900);
        }
        return false;
    }

    /**
     * 树里找得到目标群名（或聊天页输入区）才算回到了聊天页。
     *
     * <p>⚠ 输入区提示语要打地鼠（与电脑端 CHAT_HINTS / SelfHeal 同一教训）：
     * 文字模式「发消息」、**语音模式「按住 说话」（中间带空格！）**、禁言群
     * 「禁言中」。2026-10-31 真机：本机常驻语音模式，只认「发消息」时这个
     * 判定永远失败 —— 手机明明已经回到聊天页，程序却判「没回去」，多按一次
     * 返回把聊天页又退出去，再触发一次没必要的兜底巡检。
     */
    private boolean inTargetChat(AccessibilityNodeInfo root) {
        // 输入区是**最硬**的特征：UC/下载页无论怎么镜像聊天内容，都不会有
        // 「按住 说话 / 发消息」输入条 —— 有它必是聊天页。
        if (containsText(root, "按住 说话", 0, 0)     // 语音模式（带空格）
                || containsText(root, "按住说话", 0, 0)  // 同上的无空格变体
                || containsText(root, "发消息", 0, 0)    // 文字模式
                || containsText(root, "禁言中", 0, 0)) { // 禁言群
            return true;
        }
        String group = CollectorService.watchGroup;
        if (group == null || group.isEmpty()
                || !containsText(root, group, 0, 0)) {
            return false;
        }
        // 2026-11-01 实锤：点「下载附件」会跳进 UC（com.UCMobile
        // InnerUCMobile），UC 把聊天记录整个镜像渲染 —— 树里照样有群名。
        // 旧判据「有群名就算回到聊天页」在这里永远成立 → restoreToChat
        // 直接报成功，手机留在下载页，之后采集端从 UC 页里什么都看不见。
        // 下载页的三个按钮在聊天页不可能出现，拿它们当指纹排除。
        return !(containsText(root, "极速保存至网盘", 0, 0)
                || containsText(root, "直接打开", 0, 0)
                || containsText(root, "下载文件", 0, 0));
    }

    private boolean containsText(AccessibilityNodeInfo node, String want,
                                 int depth, int hits) {
        if (want == null || want.isEmpty() || node == null || depth > 30) {
            return false;
        }
        CharSequence text = node.getText();
        if (text != null && text.toString().contains(want)) {
            return true;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            if (containsText(c, want, depth + 1, hits)) {
                return true;
            }
        }
        return false;
    }

    /** 屏上是「退出UC吗？」确认框时点「退出」，返回是否点到了。 */
    private boolean tapExitIfDialog(AccessibilityNodeInfo root,
                                    AccessibilityService svc) {
        Rect dialog = new Rect();
        if (!findExactText(root, "退出UC吗", dialog, 0)) {
            return false;
        }
        Rect exit = new Rect();
        if (!findExactText(root, "退出", exit, 0)) {
            return false;
        }
        return tap(svc, exit.exactCenterX(), exit.exactCenterY());
    }

    private boolean findExactText(AccessibilityNodeInfo node, String want,
                                  Rect out, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return false;
        }
        CharSequence text = node.getText();
        if (text != null && want.contentEquals(text.toString().trim())) {
            Rect b = new Rect();
            node.getBoundsInScreen(b);
            if (b.width() > 0 && b.height() > 0) {
                out.set(b);
                return true;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            if (findExactText(c, want, out, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    /**
     * 找**文本包含** want 的节点（取第一个有尺寸的）。
     *
     * 为什么需要它：UC 的下载弹窗按钮是「立即下载」+ 副标题「手机剩余空间
     * 30.62GB」挤在同一个 TextView 里（2026-11-01 14:20 真机截图实锤），
     * findExactText 的 contentEquals 永远不成立 —— 于是点了附件链接、弹窗
     * 摆在屏上，却 20 秒没人点「立即下载」，每张卡片都白跑一趟。
     */
    private boolean findContainsText(AccessibilityNodeInfo node, String want,
                                     Rect out, int depth) {
        if (node == null || depth > MAX_WALK_DEPTH) {
            return false;
        }
        CharSequence text = node.getText();
        if (text != null && text.toString().contains(want)) {
            Rect b = new Rect();
            node.getBoundsInScreen(b);
            if (b.width() > 0 && b.height() > 0) {
                out.set(b);
                return true;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) {
                continue;
            }
            if (findContainsText(c, want, out, depth + 1)) {
                return true;
            }
        }
        return false;
    }

    // ------------------------------------------------------------------ 手势

    /**
     * 坐标点一下。为什么用 dispatchGesture 而不是 ACTION_CLICK：附件行是
     * H5 里的文字，叶子节点 clickable=false，ACTION_CLICK 不生效
     * （SelfHeal.tap 同款结论）。没给手势权限时返回 false，不静默失效。
     */
    private boolean tap(AccessibilityService svc, float x, float y) {
        Path p = new Path();
        p.moveTo(x, y);
        GestureDescription gd = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, 60L))
                .build();
        try {
            return svc.dispatchGesture(gd, null, null);
        } catch (Throwable t) {
            Log.w(TAG, "手势派发失败：" + t);
            return false;
        }
    }

    // ------------------------------------------------------------------ 工具

    private static String sha1(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] d = md.digest(data);
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte b : d) {
                sb.append(String.format(Locale.US, "%02x", b));
            }
            return sb.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    private static String stamp() {
        return new java.text.SimpleDateFormat(
                "MM-dd HH:mm:ss", Locale.CHINA).format(new java.util.Date());
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
