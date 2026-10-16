/**
 * 免编译探测脚本（AutoX.js / Auto.js 开源版）
 * ==========================================
 *
 * 这个脚本不需要 Android Studio，直接在手机上跑：装一个 AutoX.js，把本文件
 * 拷到手机的脚本目录（或在 App 里新建脚本粘贴进去），授予无障碍权限后运行。
 * 它和 ProbeService.java 看的是同一棵树，只是换了个入口。
 *
 * ⚠ 未经真机验证 —— 本仓库的开发机上没有安卓设备，下面用的是 AutoX.js 最通用
 *   的一套 API（UiObject.childCount / child / text / className / bounds / depth）。
 *   如果你的版本对不上，按报错信息调对应方法名即可，思路不变。
 *
 * ⚠ 权限提醒：AutoX.js 自己需要无障碍权限，拿到后它能读你屏幕上的**一切**。
 *   请在专用测试机上跑，用完就撤权。
 *
 * 用法：
 *   1. 手机上打开钉钉，进入目标群聊页，让消息气泡显示出来
 *   2. 回到 AutoX.js 运行本脚本
 *   3. 结果写到 /sdcard/ddprobe_dump.xml 并打印到控制台
 *   4. adb pull /sdcard/ddprobe_dump.xml . 之后，
 *      用 python tools/android_probe/analyze_uiautomator.py ddprobe_dump.xml 出判定
 */

var TARGET_PKG = "com.alibaba.android.rimet";
var OUT = "/sdcard/ddprobe_dump.xml";
var MAX_DEPTH = 40;
var MAX_NODES = 5000;

auto.waitFor();

console.show();
console.log("当前前台应用：" + currentPackage());
if (currentPackage() !== TARGET_PKG) {
    console.log("⚠ 钉钉不在前台。请先切到钉钉、进入目标群聊页，再运行本脚本。");
    console.log("  （也可以无视这条继续，但抓到的就是别的应用了）");
}

var lines = [];
var texts = [];
var idx = 0;

function esc(s) {
    return String(s === null || s === undefined ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&apos;")
        .replace(/[\r\n]/g, " ");
}

function walk(node, depth) {
    if (!node || depth > MAX_DEPTH || idx >= MAX_NODES) {
        return;
    }
    idx++;

    var cls = "";
    var txt = "";
    var desc = "";
    var pkg = "";
    var rect = null;
    try { cls = node.className() || ""; } catch (e) { }
    try { txt = node.text() || ""; } catch (e) { }
    try { desc = node.desc() || ""; } catch (e) { }
    try { pkg = node.packageName() || ""; } catch (e) { }
    try { rect = node.bounds(); } catch (e) { }

    var left = rect ? rect.left : 0;
    var top = rect ? rect.top : 0;
    var right = rect ? rect.right : 0;
    var bottom = rect ? rect.bottom : 0;

    var pad = "";
    for (var i = 0; i < Math.min(depth, 24); i++) {
        pad += "  ";
    }
    var show = txt !== "" ? txt : desc;
    if (show !== "") {
        texts.push(show);
        console.log(pad + cls + "  " + show);
    }

    lines.push(pad + '<node index="' + idx + '" text="' + esc(txt) +
        '" resource-id="" class="' + esc(cls) + '" package="' + esc(pkg) +
        '" content-desc="' + esc(desc) +
        '" scrollable="false" bounds="[' + left + ',' + top + '][' + right + ',' + bottom + ']">');
    lines.push(pad + '</node>');

    var n = 0;
    try { n = node.childCount(); } catch (e) { n = 0; }
    for (var k = 0; k < n; k++) {
        var child = null;
        try { child = node.child(k); } catch (e) { child = null; }
        walk(child, depth + 1);
    }
}

console.log("开始抓取…");
var root = className("android.widget.FrameLayout").findOnce();
if (!root) {
    // 兜底：有的版本直接拿不到 FrameLayout，就找任意一个最外层节点
    root = classNameMatches(/.+/).findOnce();
}
if (!root) {
    console.log("✗ 拿不到根节点。确认无障碍权限已开启，且钉钉在前台。");
    exit();
}
walk(root, 0);

var content = '<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">\n' +
    lines.join("\n") + "\n</hierarchy>\n";
files.write(OUT, content);

console.log("");
console.log("==== 小结 ====");
console.log("节点数：" + idx);
console.log("带文本的节点：" + texts.length);
console.log("输出文件：" + OUT);
if (texts.length === 0) {
    console.log("✗ 没读到任何文本 —— 请确认钉钉停在具体群聊页（不是会话列表）。");
} else {
    console.log("最后一条文本：" + texts[texts.length - 1]);
    console.log("把它拉回电脑后跑：");
    console.log("  python tools/android_probe/analyze_uiautomator.py ddprobe_dump.xml");
}
