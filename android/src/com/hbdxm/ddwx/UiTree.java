package com.hbdxm.ddwx;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;

import java.util.ArrayList;
import java.util.List;

/**
 * 把无障碍节点树序列化成 uiautomator 同格式的 XML。
 *
 * <p>格式刻意与 {@code adb shell uiautomator dump} 完全一致（属性名、bounds 写法
 * {@code [l,t][r,b]}、节点嵌套方式）。这样带来三个好处：
 * <ol>
 *   <li>电脑侧已经有的 {@code tools/android_probe/analyze_uiautomator.py} 能直接吃，不用改；</li>
 *   <li>将来万一 adb 通了，两条路的输出可以逐字节对比，便于定位差异；</li>
 *   <li>解析规则只有一套，不会出现「手机换了个写法、电脑侧解析器跟着崩」。</li>
 * </ol>
 *
 * <p>一次抓取会同时产出三样东西：XML 全文、按顺序排列的文本清单、以及可滚动容器
 * 的摘要。文本清单是给设置页显示「最后读到什么」用的；可滚动容器摘要用来快速判断
 * 消息列表在哪一层。
 */
final class UiTree {

    /** 深度和节点数都要设上限：极端情况下钉钉的树能有上万节点，遍历要花掉几百毫秒。 */
    private static final int MAX_DEPTH = 40;
    private static final int MAX_NODES = 6000;

    final StringBuilder xml = new StringBuilder(96 * 1024);
    final List<String> texts = new ArrayList<String>();
    final List<String> scrollers = new ArrayList<String>();

    /** 根节点的包名 —— 用它判断「当前前台到底是不是钉钉」。 */
    String rootPackage = "";
    int nodes = 0;

    private UiTree() {
    }

    static UiTree capture(AccessibilityNodeInfo root) {
        UiTree t = new UiTree();
        t.xml.append("<?xml version='1.0' encoding='UTF-8'?>\n<hierarchy rotation=\"0\">\n");
        if (root != null) {
            CharSequence pkg = root.getPackageName();
            t.rootPackage = pkg == null ? "" : pkg.toString();
            t.walk(root, 0);
        }
        t.xml.append("</hierarchy>\n");
        return t;
    }

    private void walk(AccessibilityNodeInfo node, int depth) {
        if (node == null || depth > MAX_DEPTH || nodes >= MAX_NODES) {
            return;
        }
        int index = nodes++;

        String cls = str(node.getClassName());
        String label = str(node.getText());
        String desc = str(node.getContentDescription());
        String pkg = str(node.getPackageName());
        String vid = node.getViewIdResourceName();

        Rect r = new Rect();
        try {
            node.getBoundsInScreen(r);
        } catch (Throwable ignored) {
            // 拿不到边界就当 0 尺寸，别让一个坏节点毁掉整棵树
        }

        boolean scrollable = isScrollContainer(cls) || node.isScrollable();

        indent(depth).append("<node index=\"").append(index).append('"')
                .append(" text=\"").append(esc(label)).append('"')
                .append(" resource-id=\"").append(esc(vid)).append('"')
                .append(" class=\"").append(esc(cls)).append('"')
                .append(" package=\"").append(esc(pkg)).append('"')
                .append(" content-desc=\"").append(esc(desc)).append('"')
                .append(" clickable=\"").append(node.isClickable()).append('"')
                .append(" enabled=\"").append(node.isEnabled()).append('"')
                .append(" scrollable=\"").append(scrollable).append('"')
                .append(" bounds=\"[").append(r.left).append(',').append(r.top)
                .append("][").append(r.right).append(',').append(r.bottom).append("]\">\n");

        // 展示用文本：优先正文，没有正文就退回 content-desc
        // —— 图片、文件、语音这类消息只有 content-desc，没有 text
        String shown = !label.isEmpty() ? label : desc;
        if (!shown.isEmpty()) {
            texts.add(shown);
        }
        if (scrollable) {
            scrollers.add(describe(cls, vid, r, safeChildCount(node)));
        }

        int n = safeChildCount(node);
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo child;
            try {
                child = node.getChild(i);
            } catch (Throwable t) {
                continue;
            }
            walk(child, depth + 1);
        }

        indent(depth).append("</node>\n");
    }

    /** 可滚动容器的一行摘要：尺寸、位置、子节点数、resource-id。 */
    private static String describe(String cls, String vid, Rect r, int children) {
        StringBuilder sb = new StringBuilder(96);
        sb.append(simple(cls)).append(' ')
                .append(r.width()).append('x').append(r.height())
                .append(" @").append(r.left).append(',').append(r.top)
                .append("  子节点=").append(children);
        if (vid != null && !vid.isEmpty()) {
            sb.append("  id=").append(vid);
        }
        return sb.toString();
    }

    private StringBuilder indent(int depth) {
        for (int i = 0; i < depth; i++) {
            xml.append("  ");
        }
        return xml;
    }

    private static int safeChildCount(AccessibilityNodeInfo node) {
        try {
            return node.getChildCount();
        } catch (Throwable t) {
            return 0;
        }
    }

    private static boolean isScrollContainer(String cls) {
        return cls.contains("RecyclerView") || cls.contains("ListView")
                || cls.contains("GridView") || cls.contains("ScrollView");
    }

    private static String str(CharSequence cs) {
        return cs == null ? "" : cs.toString().trim();
    }

    private static String simple(String cls) {
        if (cls == null || cls.isEmpty()) {
            return "?";
        }
        int i = cls.lastIndexOf('.');
        return i >= 0 ? cls.substring(i + 1) : cls;
    }

    /**
     * XML 属性值转义 —— 消息正文里 &lt; &amp; &gt; 引号都常见（订单号、链接、表情）。
     * 换行一律压成空格：属性值里的换行在解析侧会被规范化，留着只会造成两边不一致。
     * 控制字符直接丢掉，它们会让 XML 解析器直接报错。
     */
    private static String esc(String s) {
        if (s == null || s.isEmpty()) {
            return "";
        }
        StringBuilder b = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '&':
                    b.append("&amp;");
                    break;
                case '<':
                    b.append("&lt;");
                    break;
                case '>':
                    b.append("&gt;");
                    break;
                case '"':
                    b.append("&quot;");
                    break;
                case '\'':
                    b.append("&apos;");
                    break;
                case '\n':
                case '\r':
                case '\t':
                    b.append(' ');
                    break;
                default:
                    if (ch >= 0x20) {
                        b.append(ch);
                    }
            }
        }
        return b.toString();
    }
}
