package com.hbdxm.ddprobe;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

/**
 * 探测界面：只做三件事 —— 显示服务状态、跳系统无障碍设置、手动触发一次抓取。
 * 抓到的文件在 Android/data/com.hbdxm.ddprobe/files/ddprobe/ 下。
 */
public class MainActivity extends Activity {

    private TextView status;
    private final Handler handler = new Handler();
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

        Button openSystem = findViewById(R.id.btn_settings);
        openSystem.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            }
        });

        Button grab = findViewById(R.id.btn_grab);
        grab.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                ProbeService svc = ProbeService.get();
                if (svc == null) {
                    status.setText("服务没连上。\n\n先去「无障碍」里打开本应用的服务，"
                            + "再回到钉钉并进入目标群聊页。");
                    return;
                }
                svc.dump("manual");
            }
        });
    }

    private void refresh() {
        ProbeService svc = ProbeService.get();
        if (svc == null) {
            status.setText("无障碍服务：未开启\n\n"
                    + "1. 点下面的按钮去系统设置\n"
                    + "2. 在「无障碍 → 已下载的服务」里找到本应用并开启\n"
                    + "3. 回到钉钉，进入目标群聊页（要看到消息气泡）\n"
                    + "4. 等几秒，或回到本页点「立即抓取一次」");
            return;
        }
        status.setText("无障碍服务：已开启\n"
                + "抓取次数：" + ProbeService.dumps + "\n\n"
                + "最后一条文本：\n" + ProbeService.lastHead + "\n\n"
                + "拉回电脑并出判定结论：\n"
                + "adb pull /sdcard/Android/data/com.hbdxm.ddprobe/files/ddprobe/latest.xml .\n"
                + "python tools/android_probe/analyze_uiautomator.py latest.xml\n\n"
                + "最新文件：\n" + ProbeService.lastXml);
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
}
