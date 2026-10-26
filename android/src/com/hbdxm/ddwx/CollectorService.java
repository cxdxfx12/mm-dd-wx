package com.hbdxm.ddwx;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.text.TextUtils;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
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

    /**
     * 巡检首轮的延迟（秒）。见 {@link #startWatch()} 里的说明。
     * 5 秒是留给无障碍服务稳定拿到窗口根节点的时间 —— 更早调用
     * {@code getRootInActiveWindow()} 经常返回 null，那一轮就白跑了。
     */
    private static final long FIRST_WATCH_DELAY_S = 5L;

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
    /** 自动巡检：当前盯的群名（空 = 没开）。设置页直接显示，避免「以为开着」。 */
    public static volatile String watchGroup = "";
    /** 自动巡检：间隔（分钟，0 = 关）。由电脑端下发，本地也留一份兜底。 */
    public static volatile int watchMinutes = 0;
    /** 自动巡检：上一轮的结果与时间。 */
    public static volatile String lastWatch = "尚未巡检";
    public static volatile String lastWatchAt = "";
    /** 附件抓取（0.5.11）最近一句话状态。设置页/排障看这里。 */
    public static volatile String lastAttach = "尚未抓取过附件";
    /** 最近一次巡检失败时的现场快照（前台包名、标题、可见 id）。只用于排查。 */
    public static volatile String lastProbe = "";
    public static volatile long watchRuns = 0L;
    public static volatile long watchHeals = 0L;

    /**
     * 上报给电脑侧的累计轮次 —— **必须是持久化的那个值**。
     *
     * <p>踩坑（2026-10-20）：原来报的是 {@link #watchRuns}，而它是随进程归零的。
     * 用户在「服务刚重开、第一轮巡检（连上 5 秒）还没到」的那一分钟里去查
     * ``/ping``，看到 ``watch_runs: 0``，合理判断成「定时器根本没转」——
     * 实际上只是这一轮还没到点。重启后归零的计数器不能当「有没有在转」的证据。
     *
     * <p>改成读 Spiffs 里持久累计的那个：跨重启单调递增，归零就真的是清过数据。
     */
    private long watchTotalRuns() {
        return Prefs.watchRuns(this);
    }

    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> timer;
    /**
     * 心跳用**独立的**单线程调度器。与抓取共用一个池的话，一次上报超时（12 秒）
     * 会把心跳一起排到后面，电脑侧就可能因为「抓取卡住」而误判手机失联。
     */
    private ScheduledExecutorService hbScheduler;
    /** 心跳线程（手写循环而不是 scheduleWithFixedDelay，理由见 heartbeatStart）。 */
    private Thread hbThread;
    /** 有人按了「立刻发心跳」时为 true；配合 {@code synchronized(this).wait/notifyAll}。 */
    private boolean hbPoke;
    /**
     * 执行抓取的线程池。与 {@link #scheduler} 分开是有原因的：定时器也往同一个池里投任务的话，
     * 一次慢抓取（比如网络超时 12 秒）会把后续 tick 全排在它后面，定时就失去意义了。
     */
    private ExecutorService pool;
    private final AtomicBoolean busy = new AtomicBoolean(false);

    /**
     * 自动巡检用的独立调度器。**不能和抓取共用**：一轮巡检最长要 8 秒
     * （拉起 2.6s + 搜索 1.4s + 打开 1.2s + 读标题 1.2s），共用的话这段时间
     * 的抓取 tick 全被排在后面，而巡检做完正好需要立刻抓一帧回报结果。
     */
    private ScheduledExecutorService watchScheduler;
    private ScheduledFuture<?> watchTimer;
    private final AtomicBoolean watching = new AtomicBoolean(false);

    /** 附件抓取进行中。巡检看到它就让路 —— 两个流程同时动界面会互相点掉对方。 */
    public final AtomicBoolean attachBusy = new AtomicBoolean(false);

    /** 上一次成功上报的 md5。只有它变了才发，避免网络白跑。 */
    private volatile String sentMd5 = "";

    /** 「瞎了」诊断帧的节流：同一原因 2 分钟内只报一次（见 reportBlind）。 */
    private static final long BLIND_REPORT_MS = 120000L;
    private volatile String lastBlindWhy = "";
    private volatile long lastBlindAt = 0L;
    private long lastEventAt = 0L;

    /**
     * 最近一次「联系上电脑」的时刻（帧上报成功或心跳成功都算），0 = 从未。
     * 巡检定时器据此自动暂停：电脑端停了还把钉钉拽到前台，用户看到的就是
     * 「手机自己乱跳」（2026-10-27 用户定的铁律）。宽限期取 3 个心跳周期。
     */
    private static final long PC_DOWN_WATCH_GRACE_MS = 3 * 60000L;
    private volatile long lastPcOkAt = 0L;

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
        // 巡检的兜底值从本地读：换电脑、网关还没连上时，手机也得能自己站起来。
        // 真正的间隔与群名等电脑端第一次下发再覆盖（见 applyWatch）。
        watchMinutes = Prefs.watchInterval(this);
        watchGroup = Prefs.watchGroup(this);
        startTimer();
        startWatch();
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
        stopWatch();
        instance = null;
        connected = false;
        super.onDestroy();
    }

    // ------------------------------------------------------------------ 对外动作

    /** 设置页改了参数后调用：按新间隔重排定时器。 */
    public void restart() {
        startTimer();
        startWatch();
        sentMd5 = "";
        captureAndSend("settings-changed");
    }

    /** 立即抓一次（设置页按钮）。 */
    public void captureNow() {
        sentMd5 = "";
        captureAndSend("manual");
    }

    /** 立即巡检一轮（设置页按钮，用来当场验证群名填得对不对）。 */
    public void watchNow() {
        runWatchNow("手动");
    }

    /**
     * 设置页「立即查找要转发的群」按钮。
     *
     * <p>与 {@link #watchNow()} 走同一条执行路径（同一把 {@code watching} 锁防重入），
     * 唯一的区别是**不看巡检间隔**：巡检关着（{@code watchMinutes == 0}）时
     * 用户照样需要当场验证群名配对没配对。之前这个按钮借用了 watchNow，
     * 结果被 {@code watchMinutes <= 0} 挡在门外，表现就是「点了没反应」。
     *
     * <p>为什么合并成同一个方法而不是各写一份：{@link SelfHeal#ensureGroup}
     * 会真的动界面，两个线程同时跑会互相点掉对方的页面。
     */
    public void findGroupNow() {
        runWatchNow("手动找群");
    }

    /** 给 {@link SelfHeal} 用：它需要根节点，但不该去碰私有方法。 */
    AccessibilityNodeInfo safeRootPublic() {
        return safeRoot();
    }

    /**
     * 设置页「退出」按钮：把采集**彻底**停干净。
     *
     * <p>用户场景（2026-10-26）：晚上拿手机看别的东西，10 分钟一轮的巡检
     * 不停把钉钉拉到前台，烦得不行。所以退出必须做到四件事，缺一件都会
     * 「退出以后还来打扰」：
     *   1. 停抓取定时器 + 心跳（电脑侧随后会显示离线，这是预期的）；
     *   2. 停自动巡检（就是夜里抢前台的那个元凶）；
     *   3. 停掉保活前台服务，撤掉常驻通知；
     *   4. {@link #disableSelf()} 把无障碍服务自己关掉 —— 系统解绑，
     *      设置里的开关自动回关，事件一个都不会再进来。
     *
     * <p>重新启动走设置页的「启动」按钮：有 WRITE_SECURE_SETTINGS（adb
     * 授过）就编程拉起；没有就跳系统设置让用户手动开。无论哪条路，
     * 服务重新连接后 {@link #onServiceConnected()} 会把一切原样跑起来，
     * 不需要任何「恢复现场」的额外状态。
     */
    public void shutdownForNight() {
        Log.w(TAG, "用户点了退出：停抓取/心跳 → 停巡检 → 停保活通知 → 关无障碍服务");
        stopTimer();
        stopWatch();
        connected = false;
        instance = null;
        KeepAliveService.stop(this);
        disableSelf();
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

    // ------------------------------------------------------------------ 自动巡检

    /**
     * 按间隔把钉钉拉回目标群。间隔 0 = 不巡检。
     *
     * <p><b>为什么是定时而不是「发现掉了就立刻拉」</b>（用户 2026-10-20 定）：
     * 立刻拉会和用户抢前台 —— 回个消息、看别的群，程序就跳出来。定时的行为
     * 可预期，代价是**最多有一个间隔的盲区**，这段时间的消息不会补回来。
     *
     * <p><b>首轮为什么是 5 秒而不是一个间隔</b>（用户 2026-10-20 改定）：
     * 这台手机是专用采集机，服务重开时钉钉往往并不在目标群 —— 若首轮也等满
     * 一个间隔（10 分钟），这 10 分钟里一帧都抓不到（实测现象：
     * ``screen.received=false``，网关只看到心跳）。用户的原话是「我就是要程序
     * 自动定位钉钉并切换到相应群聊」，所以连上就接管。
     * 5 秒是留给 AccessibilityService 稳定拿到窗口的时间，
     * 早于这个点 {@link #safeRoot()} 常常返回 null。
     */
    private void startWatch() {
        stopWatch();
        final int minutes = watchMinutes;
        if (minutes <= 0) {
            Log.i(TAG, "自动巡检未开启");
            return;
        }
        watchScheduler = Executors.newSingleThreadScheduledExecutor();
        // 固定延迟而不是固定频率：一轮巡检本身要几秒，固定频率会在慢的时候堆积。
        watchTimer = watchScheduler.scheduleWithFixedDelay(new Runnable() {
            @Override
            public void run() {
                // 电脑端停了（网关不在）→ 巡检必须自动停（2026-10-27 用户定的）。
                // 不停的话，电脑都不在了手机还每隔几分钟把钉钉拽到前台，
                // 用户只看到「手机自己乱跳」。判定只认「多久没联系上电脑」：
                // 心跳 60 秒一次，3 次没成功就算失联。电脑恢复后帧响应里
                // 会重新带上巡检配置，这里自然恢复，不用人工干预。
                long since = System.currentTimeMillis() - lastPcOkAt;
                if (lastPcOkAt == 0L || since > PC_DOWN_WATCH_GRACE_MS) {
                    lastWatch = stamp() + " · 电脑端失联，巡检暂停"
                            + (lastPcOkAt == 0L ? "（还没联系上电脑）"
                                               : "（已 " + since / 1000 + " 秒）");
                    Log.w(TAG, lastWatch);
                    return;
                }
                runWatchNow("定时");
            }
        }, FIRST_WATCH_DELAY_S, minutes * 60L, TimeUnit.SECONDS);
        Log.i(TAG, "自动巡检已启动：连接后 " + FIRST_WATCH_DELAY_S + " 秒先跑一轮，"
                + "之后每 " + minutes + " 分钟检查一次，目标群「" + watchGroup + "」");
    }

    private void stopWatch() {
        if (watchTimer != null) {
            watchTimer.cancel(false);
            watchTimer = null;
        }
        if (watchScheduler != null) {
            watchScheduler.shutdownNow();
            watchScheduler = null;
        }
    }

    /**
     * 电脑端下发「盯哪个群、多久查一次」。
     *
     * <p>幂等：值没变就什么都不做 —— 手机每帧上报都会带这个字段，
     * 每次都重建调度器会让巡检永远等不到第一次触发。
     */
    private void applyWatch(final String group, final int minutes) {
        final int fixed = Prefs.clampWatch(minutes);
        if (fixed == watchMinutes && TextUtils.equals(group, watchGroup)) {
            return;
        }
        watchGroup = group == null ? "" : group;
        watchMinutes = fixed;
        Prefs.saveWatch(this, fixed, watchGroup);
        lastWatch = fixed <= 0 ? "自动巡检已关闭"
                : ("自动巡检已设为每 " + fixed + " 分钟一次，目标群「" + watchGroup + "」");
        Log.i(TAG, lastWatch);
        // restart() 会顺带重排抓取定时器，这里不需要 —— 只动巡检。
        startWatch();
    }

    private void runWatchNow(final String why) {
        if (attachBusy.get()) {
            // 附件抓取正在动界面（点附件、UC 预览、退出确认框），这时候巡检
            // 挤进来会把预览页点掉、或被预览页点掉 —— 两边都残。等下一轮。
            lastWatch = "附件抓取中，巡检本轮让路";
            return;
        }
        if (TextUtils.isEmpty(watchGroup)) {
            // 没配置群名就绝不会去动界面：认错群比不动作危险得多。
            lastWatch = "没配置目标群，不巡检";
            return;
        }
        if (!watching.compareAndSet(false, true)) {
            return;                       // 上一轮还没跑完
        }
        ScheduledExecutorService w = watchScheduler;
        if (w == null || w.isShutdown()) {
            w = Executors.newSingleThreadScheduledExecutor();
        }
        w.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    doWatch(why);
                } catch (Throwable t) {
                    lastWatch = "巡检内部错误：" + t;
                    Log.w(TAG, "巡检出错", t);
                } finally {
                    watching.set(false);
                }
            }
        });
    }

    private void doWatch(String why) {
        String group = watchGroup;
        watchRuns++;
        Prefs.bumpWatchRuns(this);          // 持久累计，跨重启不归零
        lastWatchAt = stamp();
        SelfHeal.Result r = SelfHeal.ensureGroup(this, group);
        lastWatch = lastWatchAt + " · " + why + " · " + r;
        if (!r.ok) {
            // 失败时把「当时钉钉停在哪一页」一起记下来（2026-10-20）。
            // 光有结论没用：同一句「读不到标题栏」背后可能是被拉到桌面、
            // 停在没有标题的页、或者根本不在钉钉。华为屏蔽 logcat，
            // 事后想再加诊断就得重装 APK —— 所以宁可现在就多存一行。
            lastProbe = SelfHeal.probe(this);
            Log.w(TAG, "巡检失败现场：" + lastProbe);
        }
        if (r.ok) {
            Log.i(TAG, "巡检（" + why + "）" + r);
        } else {
            // WARNING 会给用户造成「要不要处理」的压力，所以只在真的失败了才打。
            // ⚠ 但别升级成弹通知：一次读不到标题栏很常见，下一轮自己就好了。
            Log.w(TAG, "巡检（" + why + "）" + r);
        }
        if (r.acted) {
            watchHeals++;
            // 界面被动过 → 立刻抓一帧。不这样做的话，电脑侧要等到下次画面变化
            // 才知道手机回来了，中间这段会因为「锚点丢失」把新消息丢掉。
            sentMd5 = "";
            captureAndSend(r.ok ? "self-heal" : "self-heal-failed");
        }
        // 巡检结果搭心跳的车送到电脑侧。等下一个 60 秒太慢：失败时用户多半
        // 正盯着日志找原因，让他多等一分钟纯属折磨。
        heartbeatOnce();
    }

    // ------------------------------------------------------------------ 心跳

    /**
     * 定时告诉电脑「我还活着」。
     *
     * <p>不做这件事的话，电脑侧无法区分「群里没人说话」和「手机端已经死了」——
     * 两者在它看来都是「没有新消息」。而后者意味着一条都不再转发，属于必须
     * 主动告警的故障。
     */
    /**
     * 心跳线程：每 {@link #HEARTBEAT_MS} 发一次，但可以被 {@link #pokeHeartbeat()}
     * 提前叫醒。
     *
     * <p>为什么不用 scheduleWithFixedDelay：电脑端点「切到钉钉群」时，指令是搭
     * 心跳回程车下发的。固定 60 秒周期意味着用户点完按钮要盯着屏幕等最多一分钟
     * —— 那和没有这个按钮差不多。抓取帧（/screen，几百毫秒一次）同样能收到响应，
     * 手机在那里发现「电脑端有新指令」就可以把心跳提前戳醒。
     */
    private void heartbeatStart() {
        heartbeatStop();
        hbScheduler = Executors.newSingleThreadScheduledExecutor();
        hbThread = new Thread(new Runnable() {
            @Override
            public void run() {
                while (hbScheduler != null && !hbScheduler.isShutdown()) {
                    try {
                        heartbeatOnce();
                    } catch (Throwable t) {
                        heartbeatOk = false;
                        lastHeartbeat = "心跳内部错误：" + t;
                    }
                    // 分片等待：既能被 pokeHeartbeat() 提前叫醒，
                    // 也能在 shutdown 后及时退出。
                    long until = System.currentTimeMillis() + HEARTBEAT_MS;
                    while (System.currentTimeMillis() < until) {
                        synchronized (CollectorService.this) {
                            if (hbPoke) {
                                hbPoke = false;
                                break;
                            }
                            long left = until - System.currentTimeMillis();
                            if (left <= 0) {
                                break;
                            }
                            try {
                                CollectorService.this.wait(left);
                            } catch (InterruptedException e) {
                                Thread.currentThread().interrupt();
                                return;
                            }
                        }
                    }
                }
            }
        }, "ddwx-heartbeat");
        hbThread.setDaemon(true);
        hbThread.start();
    }

    /** 让心跳线程别等满周期、马上发一次。可以重复调用。 */
    private void pokeHeartbeat() {
        synchronized (this) {
            hbPoke = true;
            notifyAll();
        }
    }

    private void heartbeatStop() {
        if (hbScheduler != null) {
            hbScheduler.shutdownNow();
            hbScheduler = null;
        }
        if (hbThread != null) {
            hbThread = null;
        }
        pokeHeartbeat();          // 叫醒还在等的线程，让它看到 shutdown 退出
    }

    private void heartbeatOnce() {
        String token = Prefs.token(this);
        if (TextUtils.isEmpty(token)) {
            heartbeatOk = false;
            lastHeartbeat = "没填 token，心跳发不出去";
            return;
        }
        Uploader.Result r = Uploader.heartbeat(Prefs.host(this), Prefs.port(this), token,
                Prefs.appVersion(this), lastWatch, watchTotalRuns(), lastProbe);
        heartbeatOk = r.ok;
        heartbeats++;
        lastHeartbeat = stamp() + " · " + (r.ok ? "正常" : r.detail);
        if (r.ok) {
            lastPcOkAt = System.currentTimeMillis();
        } else {
            // 心跳失败不弹通知：一次网络抖动很常见，连续失联由电脑侧告警兜底。
            Log.w(TAG, "心跳失败：" + r.detail);
            return;
        }
        // 电脑端按钮点的「立刻切回目标群」。心跳是手机唯一规律发出的请求，
        // 指令只能搭它的回程车（手机没有常驻服务端，电脑主动连不进来）。
        if (jsonInt(r.body, "watch_now", 0) > 0) {
            Log.i(TAG, "收到电脑端指令：立刻切回目标群");
            lastWatch = "电脑端要求立刻切群，正在执行…";
            runWatchNow("电脑端");
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

    /**
     * 上报一次「为什么这一屏没东西可报」。
     *
     * <p>正常业务帧走 {@link #captureAndSend}，只发钉钉群里读到的节点树。
     * 但「读不到」这件事本身也必须让电脑侧知道 —— 否则网关只会显示
     * ``screen.received=false（还没收到任何画面）``，而这个状态同时对应
     * 「钉钉不在前台」「无障碍没开」「App 被杀了」「网络断了」四种完全不同的
     * 情况，排查时只能靠人猜（2026-10-20 为此白查两轮）。
     *
     * <p><b>不会污染业务数据</b>：正文是一棵刻意写的最小 XML，包名是
     * ``com.hbdxm.ddwx.diag``（不是钉钉的包名），网关侧的前台校验会把这个包名
     * 认成「不是钉钉」，于是不会进解析、不会进基线，只在 ``/ping`` 的
     * ``screen.reason`` 里留下一句原因。
     *
     * <p>节流：同一种原因 2 分钟内只报一次。抓取是每几百毫秒一次的，
     * 不节流会把电脑侧刷满。
     */
    private void reportBlind(String why) {
        long now = System.currentTimeMillis();
        if (why.equals(lastBlindWhy) && now - lastBlindAt < BLIND_REPORT_MS) {
            return;
        }
        lastBlindWhy = why;
        lastBlindAt = now;

        String token = Prefs.token(this);
        if (TextUtils.isEmpty(token)) {
            return;                       // 没填 token 连业务帧都发不出去，诊断帧更没意义
        }
        String xml = "<diag package=\"com.hbdxm.ddwx.diag\" why=\"" + why
                + "\" ver=\"" + Prefs.appVersion(this) + "\"/>";
        // 故意不走 sentMd5：诊断帧不代表「看到了新的业务画面」，
        // 记进去会让用户切回钉钉后第一帧被当成「未变化」跳过。
        Uploader.postScreen(Prefs.host(this), Prefs.port(this), token,
                xml, md5(xml), 0, "blind:" + why, Prefs.appVersion(this));
    }

    private void doCapture(String reason) {
        AccessibilityNodeInfo root = safeRoot();
        if (root == null) {
            lastResult = "拿不到窗口根节点";
            reportBlind("no-root");
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
            // 发上去只会污染电脑侧的画面序列 —— 所以正文照旧不发。
            // 但要**报一次「瞎了」的原因**：否则电脑侧只看到「还没收到任何画面」，
            // 分不清是「钉钉不在前台」「App 死了」还是「网络断了」
            //（2026-10-20 就为此白查了两轮）。
            skips++;
            lastResult = "前台是 " + tree.rootPackage + "，不是钉钉，跳过";
            reportBlind("not-dingtalk:" + tree.rootPackage);
            return;
        }
        if (tree.texts.isEmpty()) {
            skips++;
            lastResult = "钉钉在前台但读到 0 条文本 —— 请确认停在具体群聊页（不是会话列表）";
            reportBlind("empty-texts");
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
                xml, md5, tree.texts.size(), reason, Prefs.appVersion(this));
        if (r.ok) {
            lastPcOkAt = System.currentTimeMillis();
            // 只有成功才记住指纹，否则一次网络抖动会把这一屏永久跳过
            sentMd5 = md5;
            posts++;
            lastResult = "已上报：" + r.detail;
            Log.i(TAG, "上报成功 #" + posts + " 文本 " + tree.texts.size()
                    + " 字节 " + xml.length());
            maybeFillTruncated(r.body, token);
            maybeApplyWatch(r.body);
            maybeFetchAttachment(r.body, token);
            // ⚠ 每一帧都看有没有「立刻切群」指令，不只在心跳里看：
            //    心跳 60 秒一次，而用户在电脑上点完按钮就盯着屏幕等。
            //    抓取帧几百毫秒一次，在这里看到就顺手把心跳戳醒，让它马上去
            //    /ping 取指令 —— 最坏延迟从 60 秒压到 1 秒内。
            if (jsonInt(r.body, "watch_now_pending", 0) > 0) {
                pokeHeartbeat();
            }
            // 2026-10-18：不再调 maybeHoldConfirm —— 电脑侧改成「先攒后发」，
            // 不再下发 hold 指令（理由见该方法的注释）。
        } else {
            failures++;
            lastResult = "上报失败：" + r.detail;
            Log.w(TAG, "上报失败：" + r.detail);
        }
    }

    // -------------------------------------------------------------- 自动巡检指令

    /**
     * 电脑侧在每一帧的响应里带 {@code watch_group} / {@code watch_min}，
     * 告诉手机「盯哪个群、多久查一次」。
     *
     * <p>为什么由电脑端下发而不是在手机上填：目标群是 <b>config.yaml 里的
     * dingtalk.chat_names</b>，改群名要在电脑上改，手机上再填一遍必然出现
     * 「两边不一致」—— 而这里不一致的后果是每轮巡检都点错，或者永远点不回来。
     *
     * <p>解析刻意手写而不是上 JSON 库：整个 App 没有依赖，为两个字段引
     * org.json 不划算（Android 自带的那份行为还不一致，旧 ROM 上会抛）。
     */
    /**
     * 电脑侧在帧响应里发现「附件：xxx」时下发 fetch_attach 指令。
     * 这里只做转发给 {@link AttachmentFetcher}，判断全在电脑侧。
     */
    private void maybeFetchAttachment(String resp, String token) {
        if (TextUtils.isEmpty(resp)) {
            return;
        }
        String name = jsonString(resp, "fetch_attach");
        if (TextUtils.isEmpty(name)) {
            return;
        }
        // 触发字眼随指令一起下发（dingtalk.attach_keys，逗号连接）。电脑上
        // 改了配置，手机端找附件行就用新字眼 —— 两边永远一致。没带就回落默认。
        List<String> keys = new ArrayList<>();
        String keysRaw = jsonString(resp, "fetch_keys");
        if (!TextUtils.isEmpty(keysRaw)) {
            for (String k : keysRaw.split(",")) {
                k = k.trim();
                if (!k.isEmpty()) {
                    keys.add(k);
                }
            }
        }
        Log.i(TAG, "收到附件抓取指令：" + name + "，字眼 " + keys);
        AttachmentFetcher.fetch(this, token, name, keys);
    }

    /**
     * 附件抓取结束后由 {@link AttachmentFetcher} 调用：回聊天页了就立刻
     * 抓一帧（离开期间可能攒了新消息）+ 戳醒心跳把状态带上去。
     */
    public void onAttachRestored() {
        sentMd5 = "";
        captureAndSend("attach-restored");
        pokeHeartbeat();
    }

    /** 给 AttachmentFetcher 用的巡检兜底入口（跑一轮「拉回目标群」）。 */
    public void runWatchNowPublic(String why) {
        runWatchNow(why);
    }

    private void maybeApplyWatch(String resp) {
        if (TextUtils.isEmpty(resp)) {
            return;
        }
        String grp = jsonString(resp, "watch_group");
        int min = jsonInt(resp, "watch_min", Integer.MIN_VALUE);
        if (grp == null && min == Integer.MIN_VALUE) {
            return;                       // 旧版电脑端不下发，保持本地设置不动
        }
        applyWatch(grp == null ? watchGroup : grp,
                min == Integer.MIN_VALUE ? watchMinutes : min);
    }

    /** 取顶层字符串字段；没有就返回 null（不是空串 —— 空串是「明确要求关掉」）。 */
    static String jsonString(String json, String key) {
        if (json == null || key == null) {
            return null;
        }
        String pat = "\"" + key + "\"";
        int i = json.indexOf(pat);
        if (i < 0) {
            return null;
        }
        int c = json.indexOf(':', i + pat.length());
        if (c < 0) {
            return null;
        }
        int q = json.indexOf('"', c + 1);
        if (q < 0) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        for (int k = q + 1; k < json.length(); k++) {
            char ch = json.charAt(k);
            if (ch == '\\' && k + 1 < json.length()) {
                char nx = json.charAt(k + 1);
                if (nx == 'n') {
                    sb.append('\n');
                } else if (nx == 't') {
                    sb.append('\t');
                } else if (nx == 'u' && k + 5 < json.length()) {
                    try {
                        sb.append((char) Integer.parseInt(
                                json.substring(k + 2, k + 6), 16));
                        k += 4;
                    } catch (NumberFormatException ignore) {
                        // 坏转义就当没转义，总比整段丢掉强
                    }
                } else {
                    sb.append(nx);
                }
                k++;
            } else if (ch == '"') {
                break;
            } else {
                sb.append(ch);
            }
        }
        return sb.toString();
    }

    /** 取顶层整数字段；没有就用 {@code def}。 */
    static int jsonInt(String json, String key, int def) {
        if (json == null || key == null) {
            return def;
        }
        String pat = "\"" + key + "\"";
        int i = json.indexOf(pat);
        if (i < 0) {
            return def;
        }
        int c = json.indexOf(':', i + pat.length());
        if (c < 0) {
            return def;
        }
        int k = c + 1;
        while (k < json.length() && Character.isWhitespace(json.charAt(k))) {
            k++;
        }
        int start = k;
        if (k < json.length() && (json.charAt(k) == '-' || json.charAt(k) == '+')) {
            k++;
        }
        while (k < json.length() && Character.isDigit(json.charAt(k))) {
            k++;
        }
        if (k == start) {
            return def;
        }
        try {
            return Integer.parseInt(json.substring(start, k));
        } catch (NumberFormatException e) {
            return def;
        }
    }

    // -------------------------------------------------------------- 长消息补全

    /**
     * 已经连续补了几轮（每轮 = 往上翻一屏）。
     *
     * <p>2026-10-18 之前是「同一屏最多补一次」（用 md5 记账），因为那时电脑侧
     * 只拼两截，翻多了没地方放。用户反馈「1.6 屏不够，要 3 屏才够，有的消息
     * 很长的」之后改成多轮：电脑侧每轮判「这条长消息的顶边还被切着」就再下发
     * 一次 scroll，手机翻一屏报一帧，直到电脑侧不再要为止。
     *
     * <p>所以这里的防抖从「同一屏只补一次」换成「转圈保护」：连续两轮翻完
     * 手机上看到的还是同一屏内容（md5 一样），说明已经到列表顶了、翻不动了，
     * 立刻停下 —— 否则会和电脑侧的轮数上限赛跑，白翻十几次。
     */
    private String filledFor = "";
    private int fillRounds = 0;

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
     * <p>四个必须守住的点：
     * <ol>
     *   <li><b>翻完一定要回来</b> —— 不回来基线就错位，表现为锚点丢失、整屏停转；</li>
     *   <li><b>回来后重置 sentMd5</b> —— 否则回到原位时 md5 跟 sentMd5 相同，
     *       新消息不会被发现（实际不会同，但留一手更稳）；</li>
     *   <li><b>不能无限翻</b> —— 电脑侧有轮数上限（<code>FILL_MAX_ROUNDS</code>），
     *       手机侧再用「连续两轮同屏」兜一道；</li>
     *   <li><b>补全帧本身不再触发补全</b> —— 靠 {@link #filling}，
     *       否则一次 scroll 会递归出一串。</li>
     * </ol>
     */
    private void maybeFillTruncated(String resp, String token) {
        if (!resp.contains("\"scroll\"")) {
            // 电脑侧不再要补全 = 这条长消息已经收尾，轮数清零，下一个从头算
            fillRounds = 0;
            filledFor = "";
            return;
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
        // 转圈保护：翻完这一屏，如果画面上来的还是和上一轮完全一样的内容，
        // 说明已经到列表顶（或这一屏根本滚不动），再翻也是白翻。
        // ⚠ 这一条不能省：电脑侧的轮数上限是 6，手机要是真到顶了还在翻，
        //   就是六次白滑 —— 每次 700ms，期间读到的都是中间态帧。
        boolean stuck = lastMd5.equals(filledFor) && fillRounds > 0;
        filledFor = lastMd5;
        if (stuck) {
            fillRounds = 0;
            lastResult += " ｜ 已到列表顶部，不再上翻";
            return;
        }
        fillRounds++;

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
        lastResult += " ｜ 已上翻一屏补全长消息并复位（第 " + fillRounds + " 轮）";
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
                xml, md5(xml), tree.texts.size(), reason, Prefs.appVersion(this));
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
