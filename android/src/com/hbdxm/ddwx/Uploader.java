package com.hbdxm.ddwx;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UnsupportedEncodingException;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * 上报通道：把节点树 XML 送到电脑上的网关，另外提供一份落盘兜底。
 *
 * <p>为什么不只用 HTTP：手机连不上电脑的原因太多了（端口没开、Windows 防火墙、
 * 手机连的是另一张网卡、IP 填错），而这些情况在手机上都表现为「上报失败」四个字。
 * 所以关键帧额外写一份到公共 Download 目录 —— 那是 MTP 能直接翻到的位置，
 * 排障时哪怕网络全断，也能把文件拷出来看。
 *
 * <p>写盘走 MediaStore 而不是直接 File：Android 10 起分区存储，
 * 普通应用碰不到公共目录，只有通过 MediaStore 插入才会真的落在 Download 下并且
 * 对 MTP 可见。
 */
final class Uploader {

    static final String REL_DIR = "Download/mmddwx/";

    private static final int CONNECT_MS = 6000;
    private static final int READ_MS = 12000;
    private static final int MAX_BODY = 600;

    private Uploader() {
    }

    /** 一次请求的结果。detail 是给人看的一句话，直接显示在设置页上。 */
    static final class Result {
        final boolean ok;
        final int code;
        final String detail;
        /**
         * 服务端原样返回的 JSON 正文。上位机要**回话**给手机（比如「这一屏有消息
         * 没显示全，请往上翻一屏」），只靠 {@link #detail} 里截断过的可读串解析
         * 太脆 —— 日志文案一改就断。所以原始 body 单独留着。
         */
        final String body;

        Result(boolean ok, int code, String detail) {
            this(ok, code, detail, "");
        }

        Result(boolean ok, int code, String detail, String body) {
            this.ok = ok;
            this.code = code;
            this.detail = detail;
            this.body = body == null ? "" : body;
        }
    }

    // ------------------------------------------------------------------ 上报

    /** POST /screen —— 正文就是 XML 本身，元信息全放在 HTTP 头里（保持 XML 格式纯净）。 */
    static Result postScreen(String host, int port, String token, String xml,
                             String md5, int texts, String reason, String ver) {
        HttpURLConnection conn = null;
        try {
            byte[] body = xml.getBytes(StandardCharsets.UTF_8);
            conn = open("http://" + host + ":" + port + "/screen", "POST");
            conn.setFixedLengthStreamingMode(body.length);
            conn.setRequestProperty("Content-Type", "text/plain; charset=utf-8");
            // 用网关文档规定的头名。网关也认 X-Auth-Token 作别名，但自己人当然
            // 走正名，免得将来别名一删就断。
            conn.setRequestProperty("X-Bridge-Token", token);
            conn.setRequestProperty("X-Ddwx-Md5", md5);
            conn.setRequestProperty("X-Ddwx-Texts", String.valueOf(texts));
            conn.setRequestProperty("X-Ddwx-Device", Build.MODEL == null ? "" : Build.MODEL);
            // 版本号随每帧带上：换没换新包是排障的第一个问题，而机型名
            // （X-Ddwx-Device）同一个型号永远不变，区分不出 0.3.0 和 0.4.0。
            conn.setRequestProperty("X-Ddwx-Ver", ver == null ? "" : ver);
            conn.setRequestProperty("X-Ddwx-Reason", reason == null ? "" : reason);

            OutputStream os = conn.getOutputStream();
            try {
                os.write(body);
                os.flush();
            } finally {
                closeQuietly(os);
            }

            int code = conn.getResponseCode();
            String resp = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code >= 200 && code < 300) {
                return new Result(true, code, "HTTP " + code + " " + trim(resp, 120)
                        + "（" + body.length + " 字节）", resp);
            }
            return new Result(false, code, "HTTP " + code + " " + trim(resp, MAX_BODY), resp);
        } catch (Exception e) {
            return new Result(false, 0, describe(e));
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /**
     * POST /ping —— 心跳。
     *
     * <p>它看起来是多余的（抓取本来就在跑），但电脑侧<b>只能</b>靠它判断手机还在不在：
     * 抓取只在画面变化时上报，群里半天没人说话时一个包都不发，电脑侧看到的现象
     * 和「App 被系统杀了」完全一样。没有心跳就只能等用户自己发现「群里怎么没消息了」。
     *
     * <p>body 刻意是空对象而不是空串：网关对 /ping 只做「读掉请求体」，
     * 但空串会让某些代理/防火墙当成畸形请求而丢掉。
     */
    static Result heartbeat(String host, int port, String token, String ver) {
        HttpURLConnection conn = null;
        try {
            byte[] body = "{}".getBytes(StandardCharsets.UTF_8);
            conn = open("http://" + host + ":" + port + "/ping", "POST");
            conn.setFixedLengthStreamingMode(body.length);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("X-Bridge-Token", token);
            conn.setRequestProperty("X-Ddwx-Device",
                    Build.MODEL == null ? "" : Build.MODEL);
            conn.setRequestProperty("X-Ddwx-Ver", ver == null ? "" : ver);

            OutputStream os = conn.getOutputStream();
            try {
                os.write(body);
                os.flush();
            } finally {
                closeQuietly(os);
            }

            int code = conn.getResponseCode();
            String resp = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code >= 200 && code < 300) {
                return new Result(true, code, "心跳正常 " + trim(resp, 80));
            }
            if (code == 401) {
                return new Result(false, code,
                        "心跳被拒（401）：App 里的 token 与电脑端不一致");
            }
            return new Result(false, code, "HTTP " + code + " " + trim(resp, MAX_BODY));
        } catch (Exception e) {
            return new Result(false, 0, describe(e));
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /** GET /ping?token=... —— 设置页的「测试连通性」。 */
    static Result ping(String host, int port, String token) {
        HttpURLConnection conn = null;
        try {
            String q;
            try {
                q = URLEncoder.encode(token == null ? "" : token, "UTF-8");
            } catch (UnsupportedEncodingException e) {
                q = token == null ? "" : token;
            }
            conn = open("http://" + host + ":" + port + "/ping?token=" + q, "GET");
            int code = conn.getResponseCode();
            String resp = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code >= 200 && code < 300) {
                return new Result(true, code, "通了：" + trim(resp, 200));
            }
            return new Result(false, code, "HTTP " + code + " " + trim(resp, MAX_BODY));
        } catch (Exception e) {
            return new Result(false, 0, describe(e));
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ------------------------------------------------------------------ 落盘

    /** 把 XML 写到公共 Download/mmddwx/<name>，返回可读路径；失败返回 null。 */
    static String saveSnapshot(Context ctx, String name, String content) {
        byte[] data = content.getBytes(StandardCharsets.UTF_8);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentResolver cr = ctx.getContentResolver();
            Uri collection = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
            Uri target = null;

            Cursor cu = null;
            try {
                cu = cr.query(collection, new String[]{MediaStore.Downloads._ID},
                        MediaStore.Downloads.DISPLAY_NAME + "=? AND "
                                + MediaStore.Downloads.RELATIVE_PATH + "=?",
                        new String[]{name, REL_DIR}, null);
                if (cu != null && cu.moveToFirst()) {
                    target = ContentUris.withAppendedId(collection, cu.getLong(0));
                }
            } catch (Throwable ignored) {
                // 查不到就当没有，下面走插入
            } finally {
                closeQuietly(cu);
            }

            if (target == null) {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.Downloads.DISPLAY_NAME, name);
                cv.put(MediaStore.Downloads.MIME_TYPE, "text/xml");
                cv.put(MediaStore.Downloads.RELATIVE_PATH, REL_DIR);
                try {
                    target = cr.insert(collection, cv);
                } catch (Throwable t) {
                    return null;
                }
            }
            if (target == null) {
                return null;
            }

            OutputStream os = null;
            try {
                // "wt" = 截断重写。不截断就会在旧内容后面追加，读出来是两棵树拼在一起。
                os = cr.openOutputStream(target, "wt");
                if (os == null) {
                    return null;
                }
                os.write(data);
                os.flush();
            } catch (Throwable t) {
                return null;
            } finally {
                closeQuietly(os);
            }
            return REL_DIR + name;
        }

        // Android 9 及以下：分区存储还没上，公共 Download 可以直接写
        try {
            File dir = new File(Environment.getExternalStorageDirectory(), "Download/mmddwx");
            if (!dir.exists() && !dir.mkdirs()) {
                return null;
            }
            FileOutputStream fo = new FileOutputStream(new File(dir, name));
            try {
                fo.write(data);
                fo.flush();
            } finally {
                closeQuietly(fo);
            }
            return dir.getAbsolutePath() + File.separator + name;
        } catch (Throwable t) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 工具

    private static HttpURLConnection open(String spec, String method) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new java.net.URL(spec).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(CONNECT_MS);
        conn.setReadTimeout(READ_MS);
        conn.setUseCaches(false);
        conn.setDoInput(true);
        if ("POST".equals(method)) {
            conn.setDoOutput(true);
        }
        return conn;
    }

    private static String readBody(InputStream in) {
        if (in == null) {
            return "";
        }
        try {
            byte[] buf = new byte[512];
            StringBuilder sb = new StringBuilder(256);
            int n;
            while (sb.length() < MAX_BODY && (n = in.read(buf)) > 0) {
                sb.append(new String(buf, 0, n, StandardCharsets.UTF_8));
            }
            return sb.toString();
        } catch (Throwable t) {
            return "";
        } finally {
            closeQuietly(in);
        }
    }

    /** 把异常翻译成一句用户能照着排查的话 —— 原始异常名对现场排障没帮助。 */
    private static String describe(Exception e) {
        String msg = e.getMessage() == null ? "" : e.getMessage();
        if (e instanceof SocketTimeoutException) {
            return "超时：能连上但没有响应，检查电脑端程序是否在运行、端口是否被防火墙挡了";
        }
        if (e instanceof ConnectException) {
            return "连不上 " + msg + "：检查 IP/端口、手机与电脑是否同一网段、Windows 防火墙是否放行";
        }
        if (e instanceof java.net.UnknownHostException) {
            return "地址解析不了：" + msg;
        }
        return e.getClass().getSimpleName() + ": " + msg;
    }

    private static String trim(String s, int max) {
        if (s == null) {
            return "";
        }
        String one = s.replace('\n', ' ').replace('\r', ' ').trim();
        return one.length() <= max ? one : one.substring(0, max) + "…";
    }

    private static void closeQuietly(OutputStream os) {
        if (os != null) {
            try {
                os.close();
            } catch (Throwable ignored) {
                // 关闭失败不值得上报
            }
        }
    }

    private static void closeQuietly(InputStream in) {
        if (in != null) {
            try {
                in.close();
            } catch (Throwable ignored) {
            }
        }
    }

    private static void closeQuietly(Cursor c) {
        if (c != null) {
            try {
                c.close();
            } catch (Throwable ignored) {
            }
        }
    }
}
