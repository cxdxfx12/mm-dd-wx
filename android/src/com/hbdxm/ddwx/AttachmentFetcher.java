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

    /** 等文件出现的最长时间。实测 4KB 文件 2 秒内落盘；放大到 20 秒容忍网络慢。 */
    private static final long WAIT_MS = 20000L;
    private static final long POLL_MS = 600L;
    /** 点击时刻往前的容差：下载完成时间可能略早于我们记录的时刻。 */
    private static final long MTIME_SLACK_MS = 2000L;
    /** 恢复循环的最多步数。实测 6 步（4 返回 + 1 退出 + 1 返回）；留余量。 */
    private static final int RESTORE_MAX_STEPS = 12;

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

    /** 最近一次附件抓取的一句话状态。设置页/日志看这里。 */
    public static volatile String lastStatus = "尚未抓取过附件";

    /** 执行中的标志。抓取期间巡检必须让路（两个都在动界面会互相点掉对方）。 */
    private final AtomicBoolean busy;

    private AttachmentFetcher(AtomicBoolean busy, List<String> keys) {
        this.busy = busy;
        this.keys = (keys == null || keys.isEmpty()) ? DEFAULT_KEYS : keys;
    }

    // ------------------------------------------------------------------ 入口

    /**
     * 收到电脑侧 fetch_attach 指令时调用。已有抓取在跑就直接放弃 ——
     * 电脑侧有 10 分钟冷却，并发抓取只可能是异常重叠，宁可少抓一份
     * （卡片重渲染还会再给机会）也不冒两个流程互相干扰的险。
     */
    static void fetch(final CollectorService svc, final String token,
                      final String name, final List<String> keys) {
        if (!svc.attachBusy.compareAndSet(false, true)) {
            Log.w(TAG, "附件抓取已在进行，忽略本次指令：" + name);
            return;
        }
        svc.attachBusySince = System.currentTimeMillis();   // 让路宽限期计时
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                AttachmentFetcher f = new AttachmentFetcher(svc.attachBusy, keys);
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

        // ② 找到屏上的附件节点并点它。找不到多半是卡片已被滚走 —— 放弃本次，
        //    冷却会让下一张卡片再给机会。失败时把「这棵树里和『附件』沾边
        //    的节点」原样打出来，否则每换一种渲染形态都得重装 APK 才能定位。
        Rect bounds = findAttachBounds(svc, name);
        if (bounds == null) {
            lastStatus = "屏上找不到附件行（" + name + "），放弃本次";
            Log.w(TAG, lastStatus);
            Log.w(TAG, "现场：\n" + diagAttach(svc));
            return;
        }

        long tapAt = System.currentTimeMillis();
        if (!tap(svc, bounds.exactCenterX(), bounds.exactCenterY())) {
            lastStatus = "点击附件被系统拒绝（无手势权限？）";
            Log.w(TAG, lastStatus);
            return;                              // 没点开，无需恢复
        }
        Log.i(TAG, "已点击附件「" + name + "」，等文件落盘…");

        // ③ 轮询等新文件。**一出现就立刻走人**——多待一秒多漏一条推送。
        //    （预览页可能先弹「立即下载」按钮 —— 2026-10-27 真机实测：并非
        //    总是静默下载，UC 会先停在下载页等人点。等文件的同时盯着这个
        //    按钮，出现就点一下。）
        File file = waitForNewFile(svc, before, tapAt);
        if (file == null) {
            lastStatus = "等了 " + WAIT_MS / 1000 + " 秒没见到新文件（" + name + "）";
            Log.w(TAG, lastStatus);
            restoreQuietly(svc);                 // 超时也必须马上回去
            return;
        }

        // ④ 读文件 + 上传。
        byte[] data;
        try {
            data = java.nio.file.Files.readAllBytes(file.toPath());
        } catch (Throwable e) {
            // 老设备没有 java.nio.file.Files（API 26 以下）——手读兜底
            data = readBytes(file);
        }
        if (data == null || data.length == 0) {
            lastStatus = "文件读不出来：" + file.getName();
            Log.w(TAG, lastStatus);
            restoreQuietly(svc);
            return;
        }
        Uploader.Result r = Uploader.postFile(
                Prefs.host(svc), Prefs.port(svc), token,
                name, sha1(data), data);
        lastStatus = stamp() + " · " + (r.ok ? "已上传 " : "上传失败 ")
                + file.getName() + "（" + data.length + " 字节）· " + r.detail;
        Log.i(TAG, "附件「" + name + "」：" + lastStatus);

        // ⑤ 马上回聊天页（用户定的：不要在预览里多待一秒）。
        boolean back = restoreToChat(svc);
        if (back) {
            svc.onAttachRestored();
        } else {
            // 兜底：巡检会把钉钉拉回目标群（走搜索框那条成熟老路）。
            Log.w(TAG, "附件后自动返回失败，交给巡检拉回");
            svc.runWatchNowPublic("附件返回");
        }
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

    /** 在当前树上找附件行文本节点，返回屏幕坐标。优先点中带附件名的那个。 */
    private Rect findAttachBounds(AccessibilityService svc, String name) {
        AccessibilityNodeInfo root = svc.getRootInActiveWindow();
        if (root == null) {
            return null;
        }
        Rect exact = new Rect();
        Rect fallback = new Rect();
        if (findAttachNode(root, name == null ? "" : name, exact, fallback, 0)) {
            return exact;
        }
        return fallback.isEmpty() ? null : fallback;
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
        return false;
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
                        return true;
                    }
                    if (fallbackOut.isEmpty()) {
                        fallbackOut.set(b);
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
                                Map<String, long[]> before, long tapAt) {
        long deadline = System.currentTimeMillis() + WAIT_MS;
        boolean nudged = false;
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
            if (root == null) {
                // nothing
            } else if (!nudged && findExactText(root, "立即下载", btn, 0)) {
                Log.i(TAG, "预览页出现「立即下载」，点它");
                tap(svc, btn.exactCenterX(), btn.exactCenterY());
                nudged = true;
            } else if (findExactText(root, "确定", btn, 0)
                    && containsText(root, "已有相同文件", 0, 0)) {
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
                    return true;
                }
                // 在钉钉但不是目标群（WebView 层/别的页面）→ 再退一次
                svc.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK);
                sleep(900);
                root = svc.getRootInActiveWindow();
                if (root != null && DINGTALK_PKG.equals(
                        String.valueOf(root.getPackageName()))
                        && inTargetChat(root)) {
                    return true;
                }
                return false;                    // 交给巡检兜底
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

    /** 树里找得到目标群名（或「发消息」输入区）才算回到了聊天页。 */
    private boolean inTargetChat(AccessibilityNodeInfo root) {
        String group = CollectorService.watchGroup;
        return containsText(root, group, 0, 0)
                || containsText(root, "发消息", 0, 0);
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
