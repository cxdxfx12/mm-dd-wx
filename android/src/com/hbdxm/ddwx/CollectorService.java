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

    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> timer;
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
    }

    private void stopTimer() {
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
        } else {
            failures++;
            lastResult = "上报失败：" + r.detail;
            Log.w(TAG, "上报失败：" + r.detail);
        }
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
