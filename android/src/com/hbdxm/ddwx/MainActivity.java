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
     * 申请「忽略电池优化」。
     *
     * <p>不做这一步的典型现场：手机锁屏十分钟后，系统进入深度省电，把采集端
     * 连同无障碍服务一起冻住，恢复亮屏也不自动解冻 —— 表现是「一条都不转了」。
     */
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
            status.setText("无障碍服务：未开启\n\n"
                    + "1. 点上面第一个按钮去系统设置\n"
                    + "2. 在「无障碍 → 已安装的服务」里找到「喵喵采集端·读钉钉」并打开\n"
                    + "3. 填好下面的电脑 IP / 端口 / token，点「保存设置」\n"
                    + "4. 回到钉钉，进入目标群聊页，保持亮屏\n\n"
                    + "注意：采集只读节点树，不点击、不输入，不会改动钉钉里的任何内容。");
            return;
        }
        StringBuilder sb = new StringBuilder(400);
        sb.append("无障碍服务：已开启\n")
                .append("抓取 ").append(CollectorService.captures)
                .append(" · 上报 ").append(CollectorService.posts)
                .append(" · 跳过 ").append(CollectorService.skips)
                .append(" · 失败 ").append(CollectorService.failures)
                .append('\n')
                .append("心跳 ").append(CollectorService.heartbeatOk ? "✔ 正常" : "✘ 异常")
                .append("（共 ").append(CollectorService.heartbeats).append(" 次）\n")
                .append("　").append(CollectorService.lastHeartbeat).append('\n')
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
