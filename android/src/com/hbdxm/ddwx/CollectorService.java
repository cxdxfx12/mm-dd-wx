package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.text.TextUtils;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 采集服务。钉钉界面一变就读一次无障碍节点树，画面真的变了才上报。
 *
 * <p><b>它是有意做「笨」的</b>：不解析消息、不判新老、不去重、不管时间窗。
 * 所有业务判断都留在电脑侧的 Python 里（那边能调试、能加日志、能写回归测试），
 * 手机端只负责「把当前这一屏的树原样送出去」。这样一来：
 * <ul>
 *   <li>改解析规则不用重新编译、重新装手机；</li>
 *   <li>「不转发历史」那六道关卡与现役 OCR 路径共用同一套代码，不会两条路各写一份；</li>
 *   <li>手机端出错的面只有「读取」和「网络」两件事，好排查。</li>
 * </ul>
 *
 * <p>两道节流：
 * <ol>
 *   <li>事件节流（{@link #EVENT_THROTTLE_MS}）—— 钉钉一次滚动会连发几十个事件；</li>
 *   <li>内容去重（md5）—— 只有 XML 真变了才上报。注意 md5 算的是<b>整棵树的 XML</b>，
 *       所以「时间戳文案变了」「输入框光标变了」这类噪声也会触发上报。
 *       这是刻意接受的代价：宁可多发几个包，也不能因为指纹太粗而漏掉新消息 ——
 *       漏发是业务事故，多发只是流量。</li>
 * </ol>
 */
public class CollectorService extends AccessibilityService {

    public static final String TAG = "DDWX";

    /** 无障碍事件的最小间隔。 */
    private static final long EVENT_THROTTLE_MS = 400L;

    /**
     * 心跳间隔。必须显著小于电脑侧的失联阈值（默认 300 秒），
     * 否则网络稍微一抖就被判失联；60 秒既够密，又不至于让待机耗电变差。
     */
    private static final long HEARTBEAT_MS = 60000L;

    private static volatile CollectorService instance;

    // ---- 给设置页读的运行时状态。都是 volatile，探测/展示用不追求事务性。 ----
    public static volatile boolean connected = false;
    public static volatile long captures = 0L;
    public static volatile long posts = 0L;
    public static volatile long skips = 0L;
    public static volatile long failures = 0L;
    public static volatile String lastResult = "尚未开始";
    public static volatile String lastAt = "";
    public static volatile String lastHead = "";
    public static volatile int lastTexts = 0;
    public static volatile int lastBytes = 0;
    public static volatile String lastMd5 = "";
    /** 心跳结果，设置页直接显示 —— 「电脑说手机失联」时第一眼要看的就是它。 */
    public static volatile boolean heartbeatOk = false;
    public static volatile String lastHeartbeat = "尚未发送";
    public static volatile long heartbeats = 0L;

    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> timer;
    /**
     * 心跳用**独立的**单线程调度器。与抓取共用一个池的话，一次上报超时（12 秒）
     * 会把心跳一起排到后面，电脑侧就可能因为「抓取卡住」而误判手机失联。
     */
    private ScheduledExecutorService hbScheduler;
    private ScheduledFuture<?> hbTimer;
    /**
     * 执行抓取的线程池。与 {@link #scheduler} 分开是有原因的：定时器也往同一个池里投任务的话，
     * 一次慢抓取（比如网络超时 12 秒）会把后续 tick 全排在它后面，定时就失去意义了。
     */
    private ExecutorService pool;
    private final AtomicBoolean busy = new AtomicBoolean(false);

    /** 上一次成功上报的 md5。只有它变了才发，避免网络白跑。 */
    private volatile String sentMd5 = "";
    private long lastEventAt = 0L;

    public static CollectorService get() {
        return instance;
    }

    // ------------------------------------------------------------------ 生命周期

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        connected = true;

        AccessibilityServiceInfo info = getServiceInfo();
        if (info != null) {
            // 这三个 flag 是「能不能读到消息正文」的关键，见 accessibility_service_config.xml
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                    | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            setServiceInfo(info);
        }
        Log.i(TAG, "采集服务已连接");
        // 顺带把前台保活服务拉起来：无障碍服务被系统解绑时，它是唯一还在的
        // 「喊话口」（常驻通知会把状态改成警示）。放在这里而不是只放在设置页，
        // 是因为用户很少主动打开设置页 —— 手机重启后更是根本不会打开。
        KeepAliveService.start(this);
        startTimer();
        captureAndSend("service-connected");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) {
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastEventAt < EVENT_THROTTLE_MS) {
            return;
        }
        lastEventAt = now;
        captureAndSend(AccessibilityEvent.eventTypeToString(event.getEventType()));
    }

    @Override
    public void onInterrupt() {
        // 被系统中断不用做什么：重新开启时会再走一次 onServiceConnected
        Log.w(TAG, "采集服务被系统中断");
    }

    @Override
    public void onDestroy() {
        stopTimer();
        instance = null;
        connected = false;
        super.onDestroy();
    }

    // ------------------------------------------------------------------ 对外动作

    /** 设置页改了参数后调用：按新间隔重排定时器。 */
    public void restart() {
        startTimer();
        sentMd5 = "";
        captureAndSend("settings-changed");
    }

    /** 立即抓一次（设置页按钮）。 */
    public void captureNow() {
        sentMd5 = "";
        captureAndSend("manual");
    }

    /** 只落快照，不上报（排障用）。返回可读路径或 null。 */
    public String snapshotNow() {
        AccessibilityNodeInfo root = safeRoot();
        if (root == null) {
            return null;
        }
        UiTree tree = UiTree.capture(root);
        String path = Uploader.saveSnapshot(this, "latest.xml", tree.xml.toString());
        lastResult = path == null ? "快照写入失败" : ("快照已存到 " + path);
        return path;
    }

    // ------------------------------------------------------------------ 定时器

    private void startTimer() {
        stopTimer();
        int ms = Prefs.interval(this);
        scheduler = Executors.newSingleThreadScheduledExecutor();
        pool = Executors.newSingleThreadExecutor();
        timer = scheduler.scheduleWithFixedDelay(new Runnable() {
            @Override
            public void run() {
                captureAndSend("tick");
            }
        }, ms, ms, TimeUnit.MILLISECONDS);
        Log.i(TAG, "定时抓取已启动，间隔 " + ms + "ms");
        heartbeatStart();
    }

    private void stopTimer() {
        heartbeatStop();
        if (timer != null) {
            timer.cancel(false);
            timer = null;
        }
        if (scheduler != null) {
            scheduler.shutdownNow();
            scheduler = null;
        }
        if (pool != null) {
            pool.shutdownNow();
            pool = null;
        }
    }

    // ------------------------------------------------------------------ 心跳

    /**
     * 定时告诉电脑「我还活着」。
     *
     * <p>不做这件事的话，电脑侧无法区分「群里没人说话」和「手机端已经死了」——
     * 两者在它看来都是「没有新消息」。而后者意味着一条都不再转发，属于必须
     * 主动告警的故障。
     */
    private void heartbeatStart() {
        heartbeatStop();
        hbScheduler = Executors.newSingleThreadScheduledExecutor();
        hbTimer = hbScheduler.scheduleWithFixedDelay(new Runnable() {
            @Override
            public void run() {
                try {
                    heartbeatOnce();
                } catch (Throwable t) {
                    heartbeatOk = false;
                    lastHeartbeat = "心跳内部错误：" + t;
                }
            }
        }, 0L, HEARTBEAT_MS, TimeUnit.MILLISECONDS);
    }

    private void heartbeatStop() {
        if (hbTimer != null) {
            hbTimer.cancel(false);
            hbTimer = null;
        }
        if (hbScheduler != null) {
            hbScheduler.shutdownNow();
            hbScheduler = null;
        }
    }

    private void heartbeatOnce() {
        String token = Prefs.token(this);
        if (TextUtils.isEmpty(token)) {
            heartbeatOk = false;
            lastHeartbeat = "没填 token，心跳发不出去";
            return;
        }
        Uploader.Result r = Uploader.heartbeat(Prefs.host(this), Prefs.port(this), token);
        heartbeatOk = r.ok;
        heartbeats++;
        lastHeartbeat = stamp() + " · " + (r.ok ? "正常" : r.detail);
        if (!r.ok) {
            // 心跳失败不弹通知：一次网络抖动很常见，连续失联由电脑侧告警兜底。
            Log.w(TAG, "心跳失败：" + r.detail);
        }
    }

    // ------------------------------------------------------------------ 抓取

    /** 真正的读取与上报都丢到工作线程：读写文件、发网络请求都不能占主线程。 */
    private void captureAndSend(final String reason) {
        if (!busy.compareAndSet(false, true)) {
            return;
        }
        ExecutorService w = pool;
        if (w == null || w.isShutdown()) {
            // 定时器还没起来（或刚被 restart 拆掉）时的兜底：临时开一个池，
            // 抓完就关。宁可多建一个线程，也不能让「刚启动那一下」的抓取丢掉。
            w = Executors.newSingleThreadExecutor();
        }
        w.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    doCapture(reason);
                } catch (Throwable t) {
                    failures++;
                    lastResult = "内部错误：" + t;
                    Log.w(TAG, "抓取出错", t);
                } finally {
                    busy.set(false);
                }
            }
        });
    }

    private void doCapture(String reason) {
        AccessibilityNodeInfo root = safeRoot();
        if (root == null) {
            lastResult = "拿不到窗口根节点";
            return;
        }

        UiTree tree = UiTree.capture(root);
        captures++;
        lastTexts = tree.texts.size();
        lastBytes = tree.xml.length();
        lastHead = tree.texts.isEmpty() ? "(无文本)"
                : tree.texts.get(tree.texts.size() - 1);
        lastAt = stamp();

        String target = Prefs.target(this);
        if (!TextUtils.isEmpty(target) && !target.equals(tree.rootPackage)) {
            // 前台不是钉钉（用户在设置页、桌面、别的应用）。这时候读到的树跟业务无关，
            // 发上去只会污染电脑侧的画面序列。
            skips++;
            lastResult = "前台是 " + tree.rootPackage + "，不是钉钉，跳过";
            return;
        }
        if (tree.texts.isEmpty()) {
            skips++;
            lastResult = "钉钉在前台但读到 0 条文本 —— 请确认停在具体群聊页（不是会话列表）";
            return;
        }

        String xml = tree.xml.toString();
        String md5 = md5(xml);
        if (md5.equals(sentMd5)) {
            skips++;
            lastResult = "画面未变化，未重复上报";
            return;
        }
        lastMd5 = md5;

        if (Prefs.snapshot(this)) {
            Uploader.saveSnapshot(this, "latest.xml", xml);
        }

        String token = Prefs.token(this);
        if (TextUtils.isEmpty(token)) {
            lastResult = "读到 " + tree.texts.size() + " 条文本，但没填 token，只存了快照";
            return;
        }

        Uploader.Result r = Uploader.postScreen(Prefs.host(this), Prefs.port(this), token,
                xml, md5, tree.texts.size(), reason);
        if (r.ok) {
            // 只有成功才记住指纹，否则一次网络抖动会把这一屏永久跳过
            sentMd5 = md5;
            posts++;
            lastResult = "已上报：" + r.detail;
            Log.i(TAG, "上报成功 #" + posts + " 文本 " + tree.texts.size()
                    + " 字节 " + xml.length());
            maybeFillTruncated(r.body, token);
            // 2026-10-18：不再调 maybeHoldConfirm —— 电脑侧改成「先攒后发」，
            // 不再下发 hold 指令（理由见该方法的注释）。
        } else {
            failures++;
            lastResult = "上报失败：" + r.detail;
            Log.w(TAG, "上报失败：" + r.detail);
        }
    }

    // -------------------------------------------------------------- 长消息补全

    /** 同一屏最多补一次。重复补会把屏幕来回拉，还可能把基线搞乱。 */
    private String filledFor = "";

    /**
     * 正在补全中。补全会自己触发一串无障碍事件（滚动、重绘），
     * 不挡住的话会递归回到 {@link #captureAndSend}，在补全过程中再补一次。
     */
    private final AtomicBoolean filling = new AtomicBoolean(false);

    /**
     * 电脑侧说「这一屏有消息没显示全」时，往上翻一屏、把那一屏也报上去、再翻回来。
     *
     * <p>为什么由电脑侧发起而不是手机自己判断：截断的判据（消息越出不越出列表
     * 可视区）在 Python 那边有现成的解析和回归测试，手机端再写一份必然会漂移。
     * 手机只做「听话」这一件事。
     *
     * <p>三个必须守住的点：
     * <ol>
     *   <li><b>翻完一定要回来</b> —— 不回来基线就错位，表现为锚点丢失、整屏停转；</li>
     *   <li><b>回来后重置 sentMd5</b> —— 否则回到原位时 md5 跟 sentMd5 相同，
     *       新消息不会被发现（实际不会同，但留一手更稳）；</li>
     *   <li><b>同一屏不重复补</b> —— 用触发时的 md5 记账。</li>
     * </ol>
     */
    private void maybeFillTruncated(String resp, String token) {
        if (!resp.contains("\"scroll\"")) {
            return;
        }
        if (lastMd5.equals(filledFor)) {
            return;                       // 这一屏已经补过
        }
        if (!filling.compareAndSet(false, true)) {
            return;                       // 上一次补全还没收尾
        }
        try {
            doFillTruncated(token);
        } finally {
            filling.set(false);
        }
    }

    // -------------------------------------------------------------- H5 渲染等待

    /**
     * 电脑侧说「这一帧里有 H5 卡片还在渲染（hold: 毫秒）」，等一会儿再报一帧。
     *
     * <p>钉钉机器人消息是网页卡片，内容分批渲染：第一帧往往只有标题，表格
     * 单元格要再过几百毫秒才长全。电脑侧扣住新内容不放，等这里重报的帧里
     * 内容稳定了才投递 —— 手机只做「听话」，怎么判稳是电脑的事。
     *
     * <p><b>2026-10-18 起电脑侧已不再返回 hold</b>：改成「先攒后发」——
     * 每帧解析结果按 (发送者, 时间窗) 归并、只留最全的版本，静默几秒再投。
     * 原因是旧机制的判据（重报帧指纹一致）在 H5 卡片上不成立：卡片每重绘
     * 一次整批指纹都变，于是「第二次看到」永远等不到（真机全天 14 次扣住、
     * 0 次进入第二眼）。
     *
     * <p>这里**只删了调用点，方法体留着**：升级过渡期用户可能还装着旧版
     * 电脑端（会继续下发 hold），留着不会有害；旧版也没坏到必须马上停用。
     * 等确认所有用户都升到 2026-10-18 以后的版本，这个方法可以整段删掉。
     */
    @SuppressWarnings("unused")
    private void maybeHoldConfirm(String resp, String token) {
        if (!resp.contains("\"hold\"")) {
            return;
        }
        final long ms = parseHoldMs(resp);
        final String tok = token;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Thread.sleep(ms);
                } catch (InterruptedException ignored) {
                }
                postCurrentFrame(tok, "hold-confirm");
            }
        });
        t.setName("ddwx-hold");
        t.setDaemon(true);
        t.start();
    }

    /** 从响应里抠 hold 的毫秒数，抠不到或离谱就用默认 800。 */
    private static long parseHoldMs(String resp) {
        java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("\"hold\"\\s*:\\s*(\\d+)").matcher(resp);
        if (m.find()) {
            long v = Long.parseLong(m.group(1));
            if (v > 0 && v <= 5000) {
                return v;
            }
        }
        return 800L;
    }

    private void doFillTruncated(String token) {
        filledFor = lastMd5;
        AccessibilityNodeInfo root = safeRoot();
        if (root == null) {
            return;
        }
        AccessibilityNodeInfo list = findScrollable(root);
        if (list == null) {
            lastResult += " ｜ 想补全长消息但没找到可滚动区域";
            return;
        }
        if (!Scroller.canSwipe(this)) {
            lastResult += " ｜ 系统不支持注入手势，长消息补不了";
            return;
        }

        // ① 往下滑 = 内容往回走（露出更早的消息）。聊天页默认停在最新处，
        //    被切掉的正是最新那条消息的前半截，它在上面。
        boolean ok = Scroller.swipe(this, list, false);
        if (!ok) {
            lastResult += " ｜ 滑动被系统拒绝（没给 canPerformGestures 权限）";
            return;
        }
        Scroller.settle();
        postCurrentFrame(token, "scroll-fill");

        // ② 回到底：再反方向滑一次。这一步不能省 —— 不回来基线就错位，
        //    下一条新消息到来时会判「锚点丢失」，整屏停转。
        AccessibilityNodeInfo root2 = safeRoot();
        AccessibilityNodeInfo list2 = root2 == null ? null : findScrollable(root2);
        if (list2 != null) {
            Scroller.swipe(this, list2, true);
            Scroller.settle();
        }
        // 回到原位后画面必然还是老样子，把指纹清掉让下一帧照常上报。
        sentMd5 = "";
        lastResult += " ｜ 已上翻一屏补全长消息并复位";
    }

    /** 把「当前这一屏」原样发上去（不判重、不动 sentMd5 —— 由调用方决定）。 */
    private void postCurrentFrame(String token, String reason) {
        AccessibilityNodeInfo root = safeRoot();
        if (root == null) {
            return;
        }
        UiTree tree = UiTree.capture(root);
        if (tree.texts.isEmpty()) {
            return;
        }
        String xml = tree.xml.toString();
        Uploader.postScreen(Prefs.host(this), Prefs.port(this), token,
                xml, md5(xml), tree.texts.size(), reason);
    }

    /** 找到消息列表那一层的可滚动节点（滚动要落在它身上才有效）。 */
    private AccessibilityNodeInfo findScrollable(AccessibilityNodeInfo node) {
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
            c.recycle();          // 没命中的子节点必须还回去，否则遍历大界面会泄漏
        }
        return null;
    }

    private AccessibilityNodeInfo safeRoot() {
        try {
            return getRootInActiveWindow();
        } catch (Throwable t) {
            Log.w(TAG, "取根节点失败：" + t);
            return null;
        }
    }

    // ------------------------------------------------------------------ 工具

    private static String stamp() {
        return new SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA).format(new Date());
    }

    static String md5(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] d = md.digest(s.getBytes("UTF-8"));
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte b : d) {
                int v = b & 0xFF;
                if (v < 16) {
                    sb.append('0');
                }
                sb.append(Integer.toHexString(v));
            }
            return sb.toString();
        } catch (Throwable t) {
            // 算不出摘要时返回长度+哈希码，至少还能起到「变了没」的判断作用
            return "x" + s.length() + "-" + s.hashCode();
        }
    }
}
