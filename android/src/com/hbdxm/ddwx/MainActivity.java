package com.hbdxm.ddwx;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 设置页。四件事：看服务状态、配电脑端地址、手动抓一次、测连通性。
 *
 * <p>没有做成「一个开关全自动」的形态，是因为现场排障必须能看到
 * 「到底读到几条文本、上报成功没有、失败原因是什么」——
 * 这类信息藏在日志里等于没有，得直接摆在屏幕上。
 */
public class MainActivity extends Activity {

    private TextView status;
    private EditText etHost;
    private EditText etPort;
    private EditText etToken;
    private EditText etInterval;
    private EditText etTarget;
    private CheckBox cbSnapshot;

    private final Handler handler = new Handler();
    private final ExecutorService bg = Executors.newSingleThreadExecutor();

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            refresh();
            handler.postDelayed(this, 1000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        status = findViewById(R.id.status);
        etHost = findViewById(R.id.et_host);
        etPort = findViewById(R.id.et_port);
        etToken = findViewById(R.id.et_token);
        etInterval = findViewById(R.id.et_interval);
        etTarget = findViewById(R.id.et_target);
        cbSnapshot = findViewById(R.id.cb_snapshot);

        loadPrefs();

        // ---- 最顶上的一对大按钮：白天干活、晚上收工 --------------------
        //
        // 「启动」优先走编程开启（adb 给过 WRITE_SECURE_SETTINGS 就能一键
        // 拉起，不用进系统设置翻菜单）；没给过就跳无障碍设置页手动开。
        // 「退出」是把采集彻底停干净 —— 抓取、心跳、巡检、保活通知全停，
        // 并且 disableSelf() 把无障碍服务自己关掉。用户场景：晚上拿手机
        // 看别的东西，不想被 10 分钟一轮的巡检不停切走前台。
        Button btnStart = findViewById(R.id.btn_start);
        btnStart.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (CollectorService.get() != null && CollectorService.connected) {
                    toast("采集已经在运行了，不用重复启动。要收工点下面的「退出」。");
                    return;
                }
                if (tryEnableA11y()) {
                    toast("已启动，几秒内服务连上后抓取自动恢复。");
                }
                // tryEnableA11y 走设置页兜底时，提示语它自己已经给了
            }
        });

        Button btnExit = findViewById(R.id.btn_exit);
        btnExit.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                CollectorService svc = CollectorService.get();
                if (svc == null) {
                    // 服务本身没绑：把可能还挂着的保活通知也撤掉，保证「彻底安静」
                    KeepAliveService.stop(MainActivity.this);
                    toast("采集本来就是停止的，通知栏也清干净了。");
                    return;
                }
                svc.shutdownForNight();
                toast("已彻底退出：抓取、心跳、巡检全停了，夜里不会再被切屏。"
                        + "电脑端一会儿会显示离线，这是正常的。");
            }
        });

        Button access = findViewById(R.id.btn_access);
        access.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            }
        });

        Button capture = findViewById(R.id.btn_capture);
        capture.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                CollectorService svc = CollectorService.get();
                if (svc == null) {
                    toast("服务没开。先点上面第一个按钮去系统设置里开启本应用的无障碍服务。");
                    return;
                }
                svc.captureNow();
                toast("已触发一次抓取，看下面的状态。");
            }
        });

        // 「立即查找要转发的群」= 用户点名要的那个按钮：不管巡检开没开、
        // 间隔是几分钟，点一下就当场把钉钉切到目标群（chat_names 第一个）。
        //
        // 和下面的「立即巡检一次」的区别只在于**不要求巡检已开启**：
        // 巡检关着的时候用户照样要能验证「群名配对没配对」，
        // 而旧逻辑会因为 watchMinutes<=0 直接拦掉，等于没有这个能力。
        Button findGroup = findViewById(R.id.btn_find_group);
        findGroup.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                CollectorService svc = CollectorService.get();
                if (svc == null) {
                    toast("服务没开。先点上面「打开系统无障碍设置」把本服务打开。");
                    return;
                }
                String want = CollectorService.watchGroup;
                if (want == null || want.trim().isEmpty()) {
                    toast("还没收到目标群名。先在电脑上点「开始运行」，等手机连上再试。");
                    return;
                }
                svc.findGroupNow();
                toast("正在切到「" + want + "」，几秒后看下面的状态。");
            }
        });

        // 立即巡检：等于把「每 10 分钟那一轮」当场跑一次。
        // 存在的理由很实际 —— 等一个间隔（最长 30 分钟）才能知道群名配对没配对，
        // 用户改配置时就完全无法当场验证。
        Button watch = findViewById(R.id.btn_watch);
        watch.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                CollectorService svc = CollectorService.get();
                if (svc == null) {
                    toast("服务没开。先去系统设置里开启本应用的无障碍服务。");
                    return;
                }
                svc.findGroupNow();
                toast("已触发一次巡检，几秒后看下面的状态。");
            }
        });

        // 「后台弹出界面」权限。巡检要靠它把钉钉拉到前台。
        // 没有这个开关，startActivity 会被系统直接拒掉（SecurityException），
        // 表现就是「点了立即巡检，手机一点反应都没有」。
        Button overlay = findViewById(R.id.btn_overlay);
        overlay.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (canDrawOverlays()) {
                    toast("已经允许了，不用再开。");
                    return;
                }
                openOverlaySettings();
            }
        });

        Button snapshot = findViewById(R.id.btn_snapshot);
        snapshot.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                final CollectorService svc = CollectorService.get();
                if (svc == null) {
                    toast("服务没开，抓不到界面。");
                    return;
                }
                bg.execute(new Runnable() {
                    @Override
                    public void run() {
                        final String p = svc.snapshotNow();
                        post("快照：" + (p == null
                                ? "失败（拿不到界面或写盘被拒）"
                                : "已存到 " + p));
                    }
                });
            }
        });

        Button ping = findViewById(R.id.btn_ping);
        ping.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                final String host = text(etHost);
                final int port = intOf(text(etPort), 8765);
                final String token = text(etToken);
                post("正在测试 " + host + ":" + port + " …");
                bg.execute(new Runnable() {
                    @Override
                    public void run() {
                        Uploader.Result r = Uploader.ping(host, port, token);
                        post("连通性：" + (r.ok ? "✔ " : "✘ ") + r.detail);
                    }
                });
            }
        });

        Button save = findViewById(R.id.btn_save);
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                savePrefs();
            }
        });

        Button battery = findViewById(R.id.btn_battery);
        battery.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openBatteryWhitelist();
            }
        });

        Button startup = findViewById(R.id.btn_startup);
        startup.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openStartupManager();
            }
        });

        // 拉起前台保活服务：屏幕上的常驻通知同时承担两件事 ——
        // 抬高进程优先级，以及在无障碍服务被关掉时立刻改文案提醒。
        KeepAliveService.start(this);
        requestNoticePermission();
    }

    // ------------------------------------------------------------------ 保活

    /**
     * 编程开启无障碍服务。
     *
     * <p>正常路径需要 `WRITE_SECURE_SETTINGS` 权限 —— 这个权限给不了应用
     * 商店用户，但我们的采集端是 adb 装的，装完 `pm grant` 一次即可（见
     * installer 说明）。授过权就能一键拉起，不用进系统设置翻菜单；
     * 没授过就退回「跳系统设置页手动开」的老路。
     *
     * @return true = 已编程开启（或本来就开着）；false = 跳了设置页等手动
     */
    private boolean tryEnableA11y() {
        String want = new ComponentName(this, CollectorService.class).flattenToString();
        // Android 11+ 查权限用 checkSelfPermission；老系统这个权限反正也拿不到
        boolean granted = Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(android.Manifest.permission.WRITE_SECURE_SETTINGS)
                   == PackageManager.PERMISSION_GRANTED;
        if (granted) {
            try {
                String cur = Settings.Secure.getString(getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
                boolean has = cur != null && (cur + ":").contains(want + ":");
                if (!has) {
                    String nv = (cur == null || cur.trim().isEmpty())
                            ? want : cur + ":" + want;
                    Settings.Secure.putString(getContentResolver(),
                            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, nv);
                }
                Settings.Secure.putInt(getContentResolver(),
                        Settings.Secure.ACCESSIBILITY_ENABLED, 1);
                return true;
            } catch (Throwable t) {
                Log.w(CollectorService.TAG, "编程开启无障碍失败：" + t);
                // 落到下面走设置页兜底
            }
        }
        try {
            startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            toast("在「无障碍 → 已安装的服务」里打开「喵喵采集端·读钉钉」"
                    + "（想以后一键启动：adb 给本应用授权 WRITE_SECURE_SETTINGS）。");
        } catch (Throwable t) {
            toast("请到系统设置 → 无障碍 里手动开启本服务。");
        }
        return false;
    }


    /**
     * 申请「忽略电池优化」。
     *
     * <p>不做这一步的典型现场：手机锁屏十分钟后，系统进入深度省电，把采集端
     * 连同无障碍服务一起冻住，恢复亮屏也不自动解冻 —— 表现是「一条都不转了」。
     */
    /** 有没有「后台弹出界面 / 显示在其他应用上层」权限。 */
    private boolean canDrawOverlays() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.M) {
            return true;               // 6.0 以前这个权限不存在，视为已允许
        }
        return android.provider.Settings.canDrawOverlays(this);
    }

    /**
     * 跳到系统的「显示在其他应用上层」开关页。
     *
     * <p>⚠ 必须先申请 `SYSTEM_ALERT_WINDOW`（清单里要有），否则这个设置项
     * 在系统里根本不存在，跳过去只会看到一片空白 —— 用户会以为程序跳错了。
     * 2026-10-20 就是因为清单漏了这条，用户被提示「去开后台弹出界面」，
     * 而他手机设置里压根找不到那个开关。
     */
    private void openOverlaySettings() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.M) {
            toast("这个系统版本不需要设置这一项。");
            return;
        }
        try {
            Intent i = new Intent(
                    android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    android.net.Uri.parse("package:" + getPackageName()));
            startActivity(i);
            toast("在这个页面里打开「允许显示在其他应用上层」。");
        } catch (Throwable t) {
            toast("跳不过去，请手动去 设置 → 应用 → 喵喵采集端 → 特殊权限 里打开。");
        }
    }

    private void openBatteryWhitelist() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            toast("这个系统版本不需要设电池白名单。");
            return;
        }
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) {
                toast("已经是「不受电池优化限制」了，这项不用再设。");
                return;
            }
        } catch (Throwable ignored) {
            // 查不了就直接往下走：能打开页面比查到结论更重要
        }
        try {
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
            toast("在弹出的对话框里选「允许」。");
        } catch (Throwable t) {
            // 部分厂商 ROM 没有这个直达页，退回电池优化列表让用户手动找
            try {
                startActivity(new Intent(
                        Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
                toast("找到本应用，设为「不限制」/「允许后台活动」。");
            } catch (Throwable t2) {
                toast("打不开电池设置。请手动到「设置 → 电池 → 更多电池设置」里允许后台活动。");
            }
        }
    }

    /**
     * 打开厂商的「应用启动管理」。
     *
     * <p>华为自己的启动管理是**第二道**独立的限制：即使关了电池优化，它仍会
     * 在「应用启动管理」里自动管理，锁屏后清理后台。所以两项都要设。
     *
     * <p>这些 Activity 名字都是厂商私有的、随时可能改，所以逐个 try，
     * 全失败就退回系统应用详情页 —— 那里至少有「自启动」入口。
     */
    private void openStartupManager() {
        String[][] pages = {
                {"com.huawei.systemmanager",
                 "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager",
                 "com.huawei.systemmanager.optimize.process.ProtectActivity"},
                {"com.miui.securitycenter",
                 "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                {"com.coloros.safecenter",
                 "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                {"com.vivo.permissionmanager",
                 "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
        };
        for (String[] p : pages) {
            try {
                Intent i = new Intent();
                i.setComponent(new ComponentName(p[0], p[1]));
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                toast("在打开的页面里找到「喵喵采集端」，改为「手动管理」并允许自启动、"
                        + "允许后台活动。");
                return;
            } catch (Throwable ignored) {
                // 这个 ROM 没有这个页面，试下一个
            }
        }
        try {
            Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
            toast("没找到厂商的启动管理页。请在这个页面里手动允许自启动与后台活动。");
        } catch (Throwable t) {
            toast("打不开启动管理。请到「设置 → 应用 → 喵喵采集端」里手动允许自启动。");
        }
    }

    /** Android 13 起常驻通知要用户授权，否则前台服务的通知不显示（服务本身仍在跑）。 */
    private void requestNoticePermission() {
        if (Build.VERSION.SDK_INT < 33) {
            return;
        }
        try {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(
                        new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
            }
        } catch (Throwable t) {
            // 授权失败不影响采集，只是看不到常驻通知
            Log.w(CollectorService.TAG, "申请通知权限失败：" + t);
        }
    }

    private void savePrefs() {
        String host = text(etHost);
        if (TextUtils.isEmpty(host)) {
            toast("电脑 IP 不能为空。");
            return;
        }
        int port = intOf(text(etPort), 8765);
        int interval = intOf(text(etInterval), 2000);
        if (interval < 500) {
            interval = 500;
            etInterval.setText(String.valueOf(interval));
        }
        Prefs.save(this, host, port, text(etToken), interval,
                text(etTarget), cbSnapshot.isChecked());

        CollectorService svc = CollectorService.get();
        if (svc != null) {
            svc.restart();
            toast("已保存，采集按新参数重跑。");
        } else {
            toast("已保存。提示：无障碍服务还没开启，采集不会开始。");
        }
    }

    private void loadPrefs() {
        etHost.setText(Prefs.host(this));
        etPort.setText(String.valueOf(Prefs.port(this)));
        etToken.setText(Prefs.token(this));
        etInterval.setText(String.valueOf(Prefs.interval(this)));
        etTarget.setText(Prefs.target(this));
        cbSnapshot.setChecked(Prefs.snapshot(this));
    }

    private void refresh() {
        CollectorService svc = CollectorService.get();
        if (svc == null || !CollectorService.connected) {
            status.setText("无障碍服务：未开启（已退出或没开过）\n\n"
                    + "1. 点最上面的「▶ 启动采集」按钮（能自动开启；开不了就跳系统设置）\n"
                    + "2. 填好下面的电脑 IP / 端口 / token，点「保存设置」\n"
                    + "3. 回到钉钉，进入目标群聊页，保持亮屏\n\n"
                    + "提示：晚上收工点「⏹ 退出」，巡检就不会再抢你的手机。\n\n"
                    + "注意：采集只读节点树，不点击、不输入，不会改动钉钉里的任何内容。");
            return;
        }
        StringBuilder sb = new StringBuilder(400);
        sb.append("版本 ").append(Prefs.appVersion(this)).append('\n')
                .append("无障碍服务：已开启\n")
                .append("抓取 ").append(CollectorService.captures)
                .append(" · 上报 ").append(CollectorService.posts)
                .append(" · 跳过 ").append(CollectorService.skips)
                .append(" · 失败 ").append(CollectorService.failures)
                .append('\n')
                .append("心跳 ").append(CollectorService.heartbeatOk ? "✔ 正常" : "✘ 异常")
                .append("（共 ").append(CollectorService.heartbeats).append(" 次）\n")
                .append("　").append(CollectorService.lastHeartbeat).append('\n')
                // 巡检状态单独一行且**总是显示**：这一项开着会抢前台、关着会让
                // 采集链路断了不自愈，两种都有后果，不能让用户猜。
                .append("自动巡检 ")
                .append(CollectorService.watchMinutes > 0
                        ? ("每 " + CollectorService.watchMinutes + " 分钟 → "
                           + CollectorService.watchGroup)
                        : "关闭")
                .append('\n')
                .append("　").append(CollectorService.lastWatch).append('\n')
                .append("读到文本 ").append(CollectorService.lastTexts)
                .append(" 条 · ").append(CollectorService.lastBytes).append(" 字节\n\n")
                .append("最后动作（").append(CollectorService.lastAt).append("）：\n")
                .append(CollectorService.lastResult).append("\n\n")
                .append("最后一条文本：\n")
                .append(CollectorService.lastHead).append("\n\n")
                .append("指纹 ").append(CollectorService.lastMd5).append('\n')
                .append("上报地址 http://").append(Prefs.host(this)).append(':')
                .append(Prefs.port(this)).append("/screen");
        status.setText(sb.toString());
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(tick);
        bg.shutdownNow();
        super.onDestroy();
    }

    // ------------------------------------------------------------------ 小工具

    private static String text(EditText e) {
        return e.getText() == null ? "" : e.getText().toString().trim();
    }

    private static int intOf(String s, int def) {
        try {
            return Integer.parseInt(s);
        } catch (Throwable t) {
            return def;
        }
    }

    private void toast(final String s) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                Toast.makeText(MainActivity.this, s, Toast.LENGTH_LONG).show();
            }
        });
    }

    private void post(final String s) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                status.setText(s + "\n\n" + "（1 秒后恢复完整状态）");
            }
        });
    }
}
