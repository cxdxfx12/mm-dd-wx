package com.hbdxm.ddprobe;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.graphics.Rect;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 探测用无障碍服务：把钉钉窗口的节点树 dump 出来。
 *
 * <p>存在的唯一目的，是回答「钉钉安卓客户端到底肯不肯把消息文本暴露给无障碍服务」
 * 这个问题。PC 端是有实物证据的（消息区控件树为空，钉钉 PC 用自研 DUI 自绘），
 * 安卓端是原生 Android 视图体系、大概率能读，但没实测过就不能算数。
 *
 * <p>这个服务**只读不写**：不点击、不输入、不修改任何东西，只遍历节点树并落盘。
 * 真正的采集 App（阶段 1）应当在这个骨架上另建，而不是直接改这里。
 *
 * <p>每次抓取写三个文件（都在 Android/data/com.hbdxm.ddprobe/files/ddprobe/ 下）：
 * <ul>
 *   <li>{@code dump_<时间戳>.txt} —— 人看的：文本清单 + 可滚动容器 + 缩进节点树</li>
 *   <li>{@code latest.xml} —— 机器看的：uiautomator 同格式，直接喂给
 *       {@code tools/android_probe/analyze_uiautomator.py} 出判定结论</li>
 *   <li>{@code latest.txt} —— 上面那个 .txt 的副本，路径固定方便 pull</li>
 * </ul>
 */
public class ProbeService extends AccessibilityService {

    public static final String TAG = "DDPROBE";

    /** 钉钉安卓包名。 */
    private static final String TARGET_PKG = "com.alibaba.android.rimet";

    /** 同一秒内钉钉会连发很多个事件，节流一下，免得把消息列表 dump 到卡。 */
    private static final long DEBOUNCE_MS = 400L;

    private static final int MAX_DEPTH = 40;
    private static final int MAX_NODES = 5000;
    private static final int MAX_CHILD_PREVIEW = 30;

    // 给界面读的运行时状态。探测工具，不追求并发严谨。
    public static volatile boolean connected = false;
    public static volatile int dumps = 0;
    public static volatile String lastFile = "";
    public static volatile String lastXml = "";
    public static volatile String lastHead = "";

    private static volatile ProbeService instance;

    private final SimpleDateFormat stamp =
            new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA);
    private long lastDumpAt = 0L;

    public static ProbeService get() {
        return instance;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        connected = true;

        AccessibilityServiceInfo info = getServiceInfo();
        if (info != null) {
            // flagReportViewIds            拿 resource-id，用来定位消息列表容器
            // flagIncludeNotImportantViews 消息气泡常被标成「对无障碍不重要」，
            //                              不加这个 flag 很可能只看到一棵空树
            // flagRetrieveInteractiveWindows 允许拿到多窗口内容
            info.flags |= AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                    | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            setServiceInfo(info);
        }
        Log.i(TAG, "无障碍服务已连接，flags=" + (info == null ? -1 : info.flags));
        dump("service-connected");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) {
            return;
        }
        CharSequence pkg = event.getPackageName();
        if (pkg == null || !TARGET_PKG.contentEquals(pkg)) {
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastDumpAt < DEBOUNCE_MS) {
            return;
        }
        lastDumpAt = now;
        dump(AccessibilityEvent.eventTypeToString(event.getEventType())
                + "/" + event.getClassName());
    }

    @Override
    public void onInterrupt() {
        // 探测服务被系统中断，无事可做 —— 重新开启时会再次 onServiceConnected
    }

    @Override
    public void onDestroy() {
        connected = false;
        instance = null;
        super.onDestroy();
    }

    /** 抓一次当前窗口。 */
    public void dump(String why) {
        AccessibilityNodeInfo root;
        try {
            root = getRootInActiveWindow();
        } catch (Throwable t) {
            Log.w(TAG, "取窗口根节点失败：" + t);
            return;
        }
        if (root == null) {
            Log.w(TAG, "拿不到窗口根节点 —— 钉钉不在前台？");
            return;
        }

        Collector c = new Collector();
        try {
            walk(root, 0, c);
        } catch (Throwable t) {
            Log.w(TAG, "遍历节点树出错（已保留抓到的部分）：" + t);
        }

        StringBuilder out = new StringBuilder();
        out.append("==== DDPROBE dump ").append(stamp.format(new Date()))
                .append("  why=").append(why).append(" ====\n")
                .append("节点总数 ").append(c.nodes)
                .append("，带文本 ").append(c.texts.size()).append("\n\n");
        out.append("-- 文本节点（按出现顺序）--\n");
        for (int i = 0; i < c.texts.size(); i++) {
            out.append(i).append(". ").append(c.texts.get(i)).append('\n');
        }
        out.append("\n-- 可滚动容器（消息列表候选）--\n");
        if (c.scrollers.isEmpty()) {
            out.append("(无)\n");
        } else {
            for (String s : c.scrollers) {
                out.append(s).append('\n');
            }
        }
        out.append("\n-- 完整节点树 --\n").append(c.tree);
        out.append("\n提示：把 latest.xml 拉到电脑，跑\n")
                .append("  python tools/android_probe/analyze_uiautomator.py latest.xml\n")
                .append("会直接给出「能不能读、能不能按一条消息分组」的判定。\n");

        String[] paths = persist(out.toString(), c.xml.toString());
        dumps++;
        lastFile = paths[0] == null ? "" : paths[0];
        lastXml = paths[1] == null ? "" : paths[1];
        lastHead = c.texts.isEmpty() ? "(没读到任何文本)" : c.texts.get(c.texts.size() - 1);

        Log.i(TAG, "dump #" + dumps + " 节点 " + c.nodes + " 文本 " + c.texts.size()
                + " → " + lastXml);
        if (c.texts.isEmpty()) {
            Log.w(TAG, "这次没读到任何文本 —— 请确认钉钉停在**具体群聊页**（不是会话列表）");
        } else {
            Log.i(TAG, "最后一条文本：" + lastHead);
        }
    }

    // ------------------------------------------------------------------ 遍历

    /** 一次抓取的产物。抽出来是为了让 walk 的签名别失控。 */
    private static final class Collector {
        final StringBuilder tree = new StringBuilder();
        final StringBuilder xml = new StringBuilder("<?xml version='1.0' encoding='UTF-8'?>\n"
                + "<hierarchy rotation=\"0\">\n");
        final List<String> texts = new ArrayList<String>();
        final List<String> scrollers = new ArrayList<String>();
        int depth = 0;
        int nodes = 0;
    }

    private void walk(AccessibilityNodeInfo node, int depth, Collector c) {
        if (node == null || depth > MAX_DEPTH || c.nodes >= MAX_NODES) {
            return;
        }
        c.nodes++;

        String cls = str(node.getClassName());
        String label = str(node.getText());
        String desc = str(node.getContentDescription());
        String pkg = str(node.getPackageName());
        String vid = node.getViewIdResourceName();
        Rect r = new Rect();
        node.getBoundsInScreen(r);
        boolean scrollable = isScrollContainer(cls) || node.isScrollable();

        // ---- 人看的缩进树 ----
        for (int i = 0; i < Math.min(depth, 24); i++) {
            c.tree.append("  ");
        }
        c.tree.append(simple(cls));
        if (!label.isEmpty()) {
            c.tree.append(" text=\"").append(oneLine(label)).append('"');
        }
        if (!desc.isEmpty()) {
            c.tree.append(" desc=\"").append(oneLine(desc)).append('"');
        }
        if (vid != null && !vid.isEmpty()) {
            c.tree.append(" id=").append(vid);
        }
        c.tree.append(" @").append(r.left).append(',').append(r.top)
                .append('-').append(r.right).append(',').append(r.bottom).append('\n');

        // ---- 机器看的 XML（uiautomator 同格式）----
        for (int i = 0; i < depth; i++) {
            c.xml.append("  ");
        }
        c.xml.append("<node index=\"").append(c.nodes - 1).append('"')
                .append(" text=\"").append(esc(label)).append('"')
                .append(" resource-id=\"").append(esc(vid)).append('"')
                .append(" class=\"").append(esc(cls)).append('"')
                .append(" package=\"").append(esc(pkg)).append('"')
                .append(" content-desc=\"").append(esc(desc)).append('"')
                .append(" scrollable=\"").append(scrollable).append('"')
                .append(" bounds=\"[").append(r.left).append(',').append(r.top)
                .append("][").append(r.right).append(',').append(r.bottom)
                .append("]\">\n");

        // 展示用文本：优先正文，没有正文就用 content-desc（图片/文件消息只有 desc）
        String shown = !label.isEmpty() ? label : desc;
        if (!shown.isEmpty()) {
            c.texts.add(shown);
        }
        if (scrollable) {
            c.scrollers.add(describeContainer(node, cls, vid, r));
        }

        int n;
        try {
            n = node.getChildCount();
        } catch (Throwable t) {
            n = 0;
        }
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo child;
            try {
                child = node.getChild(i);
            } catch (Throwable t) {
                continue;
            }
            walk(child, depth + 1, c);
        }

        for (int i = 0; i < depth; i++) {
            c.xml.append("  ");
        }
        c.xml.append("</node>\n");
    }

    /** 消息列表候选：列出它的直接子节点（每个子节点应是一条消息，或一个分隔条）。 */
    private String describeContainer(AccessibilityNodeInfo node, String cls,
                                     String vid, Rect r) {
        StringBuilder sb = new StringBuilder();
        sb.append(simple(cls))
                .append(' ').append(r.width()).append('x').append(r.height())
                .append(" @").append(r.left).append(',').append(r.top)
                .append("  子节点=").append(node.getChildCount());
        if (vid != null && !vid.isEmpty()) {
            sb.append("  id=").append(vid);
        }
        sb.append('\n');
        int n = Math.min(node.getChildCount(), MAX_CHILD_PREVIEW);
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) {
                continue;
            }
            sb.append("    [").append(i).append("] ")
                    .append(simple(str(child.getClassName()))).append("  ")
                    .append(oneLine(gather(child, 0, new StringBuilder())))
                    .append('\n');
        }
        return sb.toString();
    }

    /** 把一个子树的文本拼起来，用于判断「这个子节点是不是一条消息」。 */
    private StringBuilder gather(AccessibilityNodeInfo node, int depth, StringBuilder acc) {
        if (node == null || depth > 8 || acc.length() > 240) {
            return acc;
        }
        String t = str(node.getText());
        if (t.isEmpty()) {
            t = str(node.getContentDescription());
        }
        if (!t.isEmpty()) {
            if (acc.length() > 0) {
                acc.append(" | ");
            }
            acc.append(t);
        }
        int n = node.getChildCount();
        for (int i = 0; i < n; i++) {
            gather(node.getChild(i), depth + 1, acc);
        }
        return acc;
    }

    // ------------------------------------------------------------------ 工具

    private static boolean isScrollContainer(String cls) {
        return cls.contains("RecyclerView") || cls.contains("ListView")
                || cls.contains("GridView") || cls.contains("ScrollView");
    }

    private static String str(CharSequence cs) {
        return cs == null ? "" : cs.toString().trim();
    }

    private static String simple(String cls) {
        int i = cls.lastIndexOf('.');
        return i >= 0 ? cls.substring(i + 1) : (cls.isEmpty() ? "?" : cls);
    }

    private static String oneLine(String s) {
        return s.replace('\n', ' ').replace('\r', ' ').trim();
    }

    /** XML 属性值转义 —— 消息正文里常有 & < > 和引号。 */
    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder b = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '&': b.append("&amp;"); break;
                case '<': b.append("&lt;"); break;
                case '>': b.append("&gt;"); break;
                case '"': b.append("&quot;"); break;
                case '\'': b.append("&apos;"); break;
                case '\n': b.append(' '); break;
                case '\r': b.append(' '); break;
                default:
                    // 控制字符会让 XML 解析器报错，直接丢掉
                    if (ch >= 0x20) {
                        b.append(ch);
                    }
            }
        }
        return b.toString();
    }

    /** 返回 {txt 路径, xml 路径}，失败的位置为 null。 */
    private String[] persist(String text, String xml) {
        String[] out = new String[]{null, null};
        File dir = new File(getExternalFilesDir(null), "ddprobe");
        if (!dir.exists() && !dir.mkdirs()) {
            Log.e(TAG, "建不了输出目录：" + dir);
            return out;
        }
        String ts = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.CHINA)
                .format(new Date());
        String closed = xml + "</hierarchy>\n";
        try {
            File f = new File(dir, "dump_" + ts + ".txt");
            writeUtf8(f, text);
            out[0] = f.getAbsolutePath();
            // 固定路径的那两份：adb pull 时不用去翻时间戳
            writeUtf8(new File(dir, "latest.txt"), text);
            File x = new File(dir, "latest.xml");
            writeUtf8(x, closed);
            out[1] = x.getAbsolutePath();
        } catch (Exception e) {
            Log.e(TAG, "写文件失败：" + e);
        }
        return out;
    }

    private static void writeUtf8(File f, String content) throws Exception {
        Writer w = new OutputStreamWriter(new FileOutputStream(f), StandardCharsets.UTF_8);
        try {
            w.write(content);
            w.flush();
        } finally {
            try {
                w.close();
            } catch (Exception ignored) {
                // 关闭失败不值得报错
            }
        }
    }
}
