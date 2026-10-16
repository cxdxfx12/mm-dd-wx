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

    /** 钉钉安卓包名。 */
    static final String DINGTALK_PKG = "com.alibaba.android.rimet";

    private static final String DEF_HOST = "192.168.1.209";
    private static final int DEF_PORT = 8765;
    private static final int DEF_INTERVAL = 2000;

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
