package com.hbdxm.ddwx;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

/**
 * 常驻通知的前台服务。两件事：
 *
 * <ol>
 *   <li><b>抬高进程优先级。</b>华为/小米这类系统在内存紧张时会按「应用」整体回收，
 *       而采集服务是无障碍服务 —— 被回收后再也不会自己回来。挂一个前台服务
 *       （屏幕上有常驻通知）能让进程从「后台」变成「前台」，被清理的概率显著降低。</li>
 *   <li><b>盯住无障碍服务是否还活着。</b>无障碍被系统关掉时，界面上没有任何提示，
 *       唯一的迹象是「群里不再有新消息」——而那要很久以后才会被注意到。
 *       这里每 30 秒查一次，掉了就把通知改成警示文案。</li>
 * </ol>
 *
 * <p><b>它做不到的事，别指望</b>：无障碍服务是系统绑定的，一个普通应用没有权限
 * 把它重新打开（那需要 WRITE_SECURE_SETTINGS，得用 adb 授权）。所以这里只能
 * <b>提醒</b>，重开必须人工点一下通知回到设置页。把它写成「自动恢复」是骗人的。
 */
public class KeepAliveService extends Service {

    public static final String TAG = "DDWX";

    private static final String CHANNEL = "ddwx_keepalive";
    private static final int NOTI_ID = 7211;
    /** 检查间隔。30 秒足够快，又不至于让待机耗电明显变差。 */
    private static final long CHECK_MS = 30000L;

    /**
     * 唤醒锁单次时长。带超时而不是无限期 acquire，两个理由：
     * ① Android 把「没有超时的 WakeLock」当作滥用信号；
     * ② 万一本服务被系统干掉而 onDestroy 没跑到，锁最多多耗 10 分钟就自己放开，
     *    不会变成永久漏电。每 30 秒的检查里会续期，实际效果是持续持有。
     */
    private static final long LOCK_TIMEOUT_MS = 10 * 60 * 1000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean wasAlive = true;
    private PowerManager.WakeLock wakeLock;
    /** 供常驻通知显示当前是否在「灭屏续采」状态。 */
    private static volatile boolean holdingLock = false;

    private final Runnable check = new Runnable() {
        @Override
        public void run() {
            boolean alive = CollectorService.get() != null && CollectorService.connected;
            if (alive != wasAlive) {
                wasAlive = alive;
                Log.w(TAG, alive ? "无障碍服务已恢复" : "无障碍服务已断开");
            }
            // 顺手续期唤醒锁：超时释放后这里会重新拿到，等于「一直持有」，
            // 但任何时刻都有上限，不会永久漏电。
            acquireWakeLock();
            updateNotice(alive);
            handler.postDelayed(this, CHECK_MS);
        }
    };

    /** 从 Activity 或无障碍服务里拉起。失败只记日志：拉不起前台服务不该让采集一起崩。 */
    public static void start(Context c) {
        try {
            Intent i = new Intent(c, KeepAliveService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                c.startForegroundService(i);
            } else {
                c.startService(i);
            }
        } catch (Throwable t) {
            Log.w(TAG, "前台保活服务起不来：" + t);
        }
    }

    /**
     * 用户点了「退出」：连常驻通知一起撤干净。
     *
     * <p>只 stopService 不够 —— 前台服务的通知不会自动消失，通知栏上
     * 挂着「采集端运行中」会让人以为没退干净。onDestroy 里统一 cancel。
     */
    public static void stop(Context c) {
        try {
            c.stopService(new Intent(c, KeepAliveService.class));
        } catch (Throwable t) {
            Log.w(TAG, "停止保活服务失败：" + t);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel(this);
        // 必须立刻 startForeground：Android 8+ 用 startForegroundService 起的服务
        // 若 5 秒内没有 startForeground，系统会抛 ANR 级异常把应用干掉。
        startForeground(NOTI_ID, build(KeepAliveService.this, true));
        // 前台服务只抬高进程优先级，**管不住 CPU 休眠** —— 灭屏后 CPU 一进
        // suspend，定时抓帧和心跳的回调就集体停摆。要真的一直上报，必须持锁。
        acquireWakeLock();
        handler.postDelayed(check, CHECK_MS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // START_STICKY：被系统杀掉后由系统重拉（Intent 为 null 也能活）。
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(check);
        releaseWakeLock();
        // 前台服务的常驻通知不会随 stopService 自动消失，必须自己撤。
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.cancel(NOTI_ID);
            }
        } catch (Throwable t) {
            Log.w(TAG, "撤常驻通知失败：" + t);
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------ 唤醒锁

    /**
     * 申请 CPU 唤醒锁（只保 CPU 不休眠，不亮屏、不点屏）。
     *
     * <p><b>为什么非它不可</b>：采集全靠「定时抓帧 + 心跳」两条定时回调
     * （{@link CollectorService} 的 {@code ScheduledExecutorService} 和心跳线程）。
     * 手机一待机，CPU 进 suspend，这些回调就集体停摆 —— 手机没掉线、钉钉里也真
     * 有新消息，电脑侧却一个字都收不到。2026-11-01 真机实测：最后一帧 16:08:51、
     * 最后一次心跳 16:22:28，之后 13 分钟一片空白；人一碰手机，积压的帧和指令
     * 一次性涌上来 —— 用户看到的就是「我必须翻一下手机它才转发」。
     *
     * <p>前台服务 <b>不等于</b> 不休眠：它只把进程的 oom_adj 调高、降低被回收
     * 的概率，对 CPU 电源状态没有任何约束。这两件事以前被混为一谈，所以
     * 「有常驻通知」和「真的还在上报」可以同时不成立。
     */
    private void acquireWakeLock() {
        try {
            if (!Prefs.keepAwake(this)) {
                holdingLock = false;
                return;
            }
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
                if (pm == null) {
                    Log.w(TAG, "拿不到 PowerManager，灭屏续采不可用");
                    holdingLock = false;
                    return;
                }
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ddwx:keepalive");
                // 不计数：多次 acquire 只算一次，避免续期把计数越堆越高、
                // 最后 release 一次放不掉。
                wakeLock.setReferenceCounted(false);
            }
            if (!wakeLock.isHeld()) {
                wakeLock.acquire(LOCK_TIMEOUT_MS);
                Log.i(TAG, "已持有 CPU 唤醒锁 —— 灭屏后仍按时上报（每 "
                        + (LOCK_TIMEOUT_MS / 1000) + " 秒自动续期）");
            }
            holdingLock = true;
        } catch (Throwable t) {
            holdingLock = false;
            Log.w(TAG, "唤醒锁申请失败：" + t);
        }
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
                Log.i(TAG, "已释放 CPU 唤醒锁");
            }
        } catch (Throwable t) {
            Log.w(TAG, "释放唤醒锁失败：" + t);
        } finally {
            holdingLock = false;
        }
    }

    // ------------------------------------------------------------------ 通知

    private void updateNotice(boolean alive) {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.notify(NOTI_ID, build(this, alive));
            }
        } catch (Throwable t) {
            Log.w(TAG, "更新常驻通知失败：" + t);
        }
    }

    private static Notification build(Context c, boolean alive) {
        PendingIntent open = PendingIntent.getActivity(
                c, 0, new Intent(c, MainActivity.class),
                pendingFlags());

        String title = alive ? "采集端运行中" : "⚠ 采集已停止";
        // 「灭屏续采」写进通知文案：用户一眼能看出这台机器现在会不会在待机时
        // 静默。没有这一行时，「有常驻通知」和「其实已经不上报了」长得一模一样。
        String text = alive
                ? ("已上报 " + CollectorService.posts + " 条 · 心跳 "
                   + CollectorService.lastHeartbeat
                   + (holdingLock ? " · 灭屏续采中" : " · 灭屏即静默"))
                : "无障碍服务被系统关闭了，现在一条消息都不会转发。点这里重新开启。";

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(c, CHANNEL)
                : new Notification.Builder(c);
        b.setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(open)
                .setOngoing(true)              // 常驻，划不掉 —— 划掉就等于关掉保活
                .setOnlyAlertOnce(true);
        return b.build();
    }

    private static int pendingFlags() {
        // Android 12 起 PendingIntent 必须显式声明可变性
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        }
        return PendingIntent.FLAG_UPDATE_CURRENT;
    }

    static void createChannel(Context c) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager nm =
                (NotificationManager) c.getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(
                CHANNEL, "采集端保活", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("采集端运行状态与异常提醒");
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }
}
