package com.hbdxm.ddwx;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 设置项的读写。用 SharedPreferences 而不是配置文件 —— 手机端没有可写的配置目录，
 * 而且这些值要在设置页里当场改、当场生效。
 *
 * <p>默认值里 <b>host 刻意留成示例值</b>而不是自动探测：自动探测要么扫网段（慢且有
 * 打扰），要么读网关（拿不到），不如让用户从电脑界面上抄一次。填错了也安全 ——
 * token 不对时上报会被电脑侧 401 掉，不会误发消息。
 */
final class Prefs {

    private static final String FILE = "ddwx";

    private static final String K_HOST = "host";
    private static final String K_PORT = "port";
    private static final String K_TOKEN = "token";
    private static final String K_INTERVAL = "interval_ms";
    private static final String K_TARGET = "target_pkg";
    private static final String K_SNAPSHOT = "snapshot";
    private static final String K_WATCH_INTERVAL = "watch_interval_min";
    private static final String K_WATCH_GROUP = "watch_group";
    /** 累计巡检轮次（跨重启单调递增）。见 {@link #watchRuns} 的说明。 */
    private static final String K_WATCH_RUNS = "watch_runs_total";
    /**
     * 电脑端最后一次「请你重发一帧」的请求编号（电脑的墙上时间秒）。
     *
     * <p>它必须持久化：心跳 60 秒一次，同一编号会在多次心跳里重复出现（电脑侧
     * 节流就是靠这个），不记住的话每次心跳都要重发一整屏节点树 —— 那是几百 KB。
     */
    private static final String K_FRAME_REQ = "pc_frame_req_at";

    /** 钉钉安卓包名。 */
    static final String DINGTALK_PKG = "com.alibaba.android.rimet";

    private static final String DEF_HOST = "192.168.1.209";
    private static final int DEF_PORT = 8765;
    private static final int DEF_INTERVAL = 2000;
    /** 默认不巡检：装完 App 的人不该被程序突然抢前台，等他明确打开这一项。 */
    private static final int DEF_WATCH_INTERVAL = 0;

    private Prefs() {
    }

    static SharedPreferences of(Context c) {
        return c.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    static String host(Context c) {
        String v = of(c).getString(K_HOST, DEF_HOST);
        return v == null || v.trim().isEmpty() ? DEF_HOST : v.trim();
    }

    static int port(Context c) {
        int v = of(c).getInt(K_PORT, DEF_PORT);
        return v <= 0 || v > 65535 ? DEF_PORT : v;
    }

    static String token(Context c) {
        String v = of(c).getString(K_TOKEN, "");
        return v == null ? "" : v.trim();
    }

    static String target(Context c) {
        String v = of(c).getString(K_TARGET, DINGTALK_PKG);
        return v == null || v.trim().isEmpty() ? DINGTALK_PKG : v.trim();
    }

    /** 抓取间隔，毫秒。夹在 500~60000，防手滑填出 0（会把电池烧干）。 */
    static int interval(Context c) {
        int v = of(c).getInt(K_INTERVAL, DEF_INTERVAL);
        if (v < 500) {
            return 500;
        }
        return Math.min(v, 60000);
    }

    static boolean snapshot(Context c) {
        return of(c).getBoolean(K_SNAPSHOT, true);
    }

    // ------------------------------------------------------------ 自动巡检
    //
    // 下面两项是**手机端的兜底值**，真正生效的以电脑端下发的为准（见
    // CollectorService.applyWatch）。放在本地是因为：换了一台电脑、网关还没
    // 连上时，手机不该因为「没收到指令」就完全不巡检 —— 那正好是最需要它
    // 自己站起来的时候。用户在设置页改过就一直记着。

    static int watchInterval(Context c) {
        int v = of(c).getInt(K_WATCH_INTERVAL, DEF_WATCH_INTERVAL);
        return clampWatch(v);
    }

    static String watchGroup(Context c) {
        String v = of(c).getString(K_WATCH_GROUP, "");
        return v == null ? "" : v.trim();
    }

    static void saveWatch(Context c, int interval, String group) {
        of(c).edit()
                .putInt(K_WATCH_INTERVAL, clampWatch(interval))
                .putString(K_WATCH_GROUP, group == null ? "" : group.trim())
                .apply();
    }

    /**
     * 累计巡检轮次 —— 跨重启保留。
     *
     * <p><b>为什么不能用内存里的计数器</b>（2026-10-20 真机踩到）：那个计数器
     * 随服务重启归零。用户在「刚重开、第一轮巡检（连上 5 秒）还没跑」的那
     * 一分钟里查电脑侧 ``/ping``，看到 ``watch_runs: 0``，就会判定成
     * 「定时器根本没转」，然后去重装 —— 查错方向。
     * 持久累计之后，它不涨就真的说明有问题。
     */
    static long watchRuns(Context c) {
        return of(c).getLong(K_WATCH_RUNS, 0L);
    }

    static long bumpWatchRuns(Context c) {
        long next = watchRuns(c) + 1L;
        of(c).edit().putLong(K_WATCH_RUNS, next).apply();
        return next;
    }

    /** 电脑端最后一次「请你重发一帧」的编号（0 = 还没被要求过）。 */
    static long frameReq(Context c) {
        return of(c).getLong(K_FRAME_REQ, 0L);
    }

    static void setFrameReq(Context c, long req) {
        of(c).edit().putLong(K_FRAME_REQ, req).apply();
    }

    /** 巡检间隔只允许用户给的几档：0（关）/ 5 / 10 / 15 / 30 分钟。 */
    static int clampWatch(int minutes) {
        if (minutes <= 0) {
            return 0;
        }
        if (minutes <= 5) {
            return 5;
        }
        if (minutes <= 10) {
            return 10;
        }
        if (minutes <= 15) {
            return 15;
        }
        return 30;
    }

    /**
     * 本 App 的版本号（如 "0.4.0"）。
     *
     * <p>工程是手工用 aapt2/javac 编的、不走 Gradle，所以没有 BuildConfig，
     * 只能从 PackageManager 读。读不到返回 "?"，**绝不能返回空串** ——
     * 空串在日志里和「旧版没这个字段」分不开，而这个字段存在的唯一意义
     * 就是排「装没装上新包」。
     */
    static String appVersion(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0)
                    .versionName;
        } catch (Throwable t) {
            return "?";
        }
    }

    static void save(Context c, String host, int port, String token, int interval,
                     String target, boolean snapshot) {
        of(c).edit()
                .putString(K_HOST, host == null ? "" : host.trim())
                .putInt(K_PORT, port)
                .putString(K_TOKEN, token == null ? "" : token.trim())
                .putInt(K_INTERVAL, interval)
                .putString(K_TARGET, target == null ? "" : target.trim())
                .putBoolean(K_SNAPSHOT, snapshot)
                .apply();
    }
}
