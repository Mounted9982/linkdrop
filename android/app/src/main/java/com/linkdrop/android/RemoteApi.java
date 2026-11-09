package com.linkdrop.android;

import android.content.Context;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.KeyManagementException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

final class RemoteApi {
    static final class SelectedFile {
        final Uri uri;
        final String name;
        final String relative;
        final long size;

        SelectedFile(Uri uri, String name, String relative, long size) {
            this.uri = uri;
            this.name = name;
            this.relative = relative.replace('\\', '/');
            this.size = size;
        }
    }

    static final class Connected {
        final String host;
        final int port;
        final String code;
        final String fingerprint;
        final JSONObject info;
        final JSONArray shares;

        Connected(String host, int port, String code, String fingerprint, JSONObject info, JSONArray shares) {
            this.host = host;
            this.port = port;
            this.code = code;
            this.fingerprint = fingerprint;
            this.info = info;
            this.shares = shares;
        }
    }

    private final Context context;
    private final SSLContext contextSsl;

    RemoteApi(Context context) {
        this.context = context;
        try {
            contextSsl = SSLContext.getInstance("TLS");
            contextSsl.init(null, new TrustManager[]{new X509TrustManager() {
                @Override
                public void checkClientTrusted(X509Certificate[] chain, String authType) {
                }

                @Override
                public void checkServerTrusted(X509Certificate[] chain, String authType) {
                }

                @Override
                public X509Certificate[] getAcceptedIssuers() {
                    return new X509Certificate[0];
                }
            }}, new SecureRandom());
        } catch (NoSuchAlgorithmException | KeyManagementException exc) {
            throw new IllegalStateException("无法初始化 TLS 客户端", exc);
        }
    }

    Connected connect(String host, int port, String code) throws Exception {
        JSONObject info = getJson(host, port, code, "/api/info");
        JSONObject shares = getJson(host, port, code, "/api/shares");
        String actual = info.optString("cert_fingerprint", "");
        if (!actual.isEmpty() && !actual.equals(lastFingerprint)) {
            throw new IllegalStateException("TLS 身份信息不一致，已停止连接");
        }
        return new Connected(host, port, code, lastFingerprint, info, shares.optJSONArray("shares"));
    }

    private String lastFingerprint;

    private HttpsURLConnection open(String host, int port, String code, String path) throws Exception {
        HttpsURLConnection connection = (HttpsURLConnection) new java.net.URL(
            "https://" + host + ":" + port + path
        ).openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(45_000);
        connection.setSSLSocketFactory(contextSsl.getSocketFactory());
        connection.setHostnameVerifier((hostname, session) -> true);
        connection.setRequestProperty("Authorization", "Bearer " + code);
        connection.setRequestProperty("Accept", "application/json");
        return connection;
    }

    private void captureFingerprint(HttpsURLConnection connection) throws Exception {
        Certificate[] certificates = connection.getServerCertificates();
        if (certificates.length == 0) {
            throw new IllegalStateException("对方未提供 TLS 证书");
        }
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(certificates[0].getEncoded());
        StringBuilder fingerprint = new StringBuilder();
        for (byte item : digest) {
            if (fingerprint.length() > 0) {
                fingerprint.append(':');
            }
            fingerprint.append(String.format(Locale.US, "%02X", item & 0xff));
        }
        lastFingerprint = fingerprint.toString();
    }

    JSONObject getJson(String host, int port, String code, String path) throws Exception {
        HttpsURLConnection connection = open(host, port, code, path);
        try {
            int status = connection.getResponseCode();
            captureFingerprint(connection);
            String body = readBody(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
            JSONObject value = body.isEmpty() ? new JSONObject() : new JSONObject(body);
            if (status >= 400) {
                throw new IllegalStateException(value.optString("error", "HTTP " + status));
            }
            return value;
        } finally {
            connection.disconnect();
        }
    }

    JSONObject list(String host, int port, String code, String shareId, String path) throws Exception {
        String query = "?share_id=" + URLEncoder.encode(shareId, "UTF-8")
            + "&path=" + URLEncoder.encode(path == null ? "" : path, "UTF-8");
        return getJson(host, port, code, "/api/list" + query);
    }

    void download(
        Connected connection,
        String shareId,
        String path,
        Uri destination,
        Progress progress
    ) throws Exception {
        String query = "?share_id=" + URLEncoder.encode(shareId, "UTF-8")
            + "&path=" + URLEncoder.encode(path, "UTF-8");
        HttpsURLConnection url = open(connection.host, connection.port, connection.code, "/api/download" + query);
        try {
            int status = url.getResponseCode();
            captureFingerprint(url);
            if (status >= 400) {
                throw new IllegalStateException("下载失败：HTTP " + status);
            }
            long total = url.getContentLengthLong();
            try (InputStream input = new BufferedInputStream(url.getInputStream());
                 OutputStream output = context.getContentResolver().openOutputStream(destination, "wt")) {
                if (output == null) {
                    throw new IllegalStateException("无法写入目标文件");
                }
                byte[] buffer = new byte[64 * 1024];
                long done = 0;
                int count;
                while ((count = input.read(buffer)) >= 0) {
                    output.write(buffer, 0, count);
                    done += count;
                    progress.update(done, total);
                }
            }
        } finally {
            url.disconnect();
        }
    }

    void send(
        Connected connection,
        List<SelectedFile> files,
        String batch,
        Progress progress
    ) throws Exception {
        JSONArray metadata = new JSONArray();
        for (SelectedFile file : files) {
            JSONObject item = new JSONObject();
            item.put("id", java.util.UUID.randomUUID().toString().replace("-", ""));
            item.put("relative_path", file.relative);
            item.put("size", file.size);
            item.put("sha256", sha256(file.uri, null));
            metadata.put(item);
        }
        JSONObject body = new JSONObject();
        body.put("device_id", new AppState(context).getDeviceId());
        body.put("device_name", new AppState(context).getDeviceName());
        body.put("batch", batch);
        body.put("files", metadata);
        JSONObject prepared = postJson(
            connection.host,
            connection.port,
            connection.code,
            "/api/prepare",
            body.toString().getBytes(StandardCharsets.UTF_8)
        );
        String sessionId = prepared.getString("session_id");
        JSONObject tokens = prepared.getJSONObject("tokens");
        long total = 0;
        for (SelectedFile file : files) {
            total += file.size;
        }
        long sentTotal = 0;
        for (int index = 0; index < files.size(); index++) {
            SelectedFile file = files.get(index);
            String fileId = metadata.getJSONObject(index).getString("id");
            upload(connection, sessionId, fileId, tokens.getString(fileId), file, sentTotal, total, progress);
            sentTotal += file.size;
            progress.update(sentTotal, total);
        }
    }

    private void upload(
        Connected connection,
        String sessionId,
        String fileId,
        String token,
        SelectedFile file,
        long base,
        long total,
        Progress progress
    ) throws Exception {
        String query = "?session_id=" + URLEncoder.encode(sessionId, "UTF-8")
            + "&file_id=" + URLEncoder.encode(fileId, "UTF-8")
            + "&token=" + URLEncoder.encode(token, "UTF-8");
        HttpsURLConnection url = open(connection.host, connection.port, connection.code, "/api/upload" + query);
        url.setRequestMethod("POST");
        url.setDoOutput(true);
        url.setRequestProperty("Content-Type", "application/octet-stream");
        url.setFixedLengthStreamingMode(file.size);
        try (InputStream input = new BufferedInputStream(context.getContentResolver().openInputStream(file.uri));
             OutputStream output = new BufferedOutputStream(url.getOutputStream())) {
            if (input == null) {
                throw new IllegalStateException("无法读取 " + file.name);
            }
            byte[] buffer = new byte[64 * 1024];
            long done = 0;
            int count;
            while ((count = input.read(buffer)) >= 0) {
                output.write(buffer, 0, count);
                done += count;
                progress.update(base + done, total);
            }
        }
        int status = url.getResponseCode();
        captureFingerprint(url);
        String response = readBody(status >= 400 ? url.getErrorStream() : url.getInputStream());
        url.disconnect();
        if (status >= 400) {
            JSONObject error = response.isEmpty() ? new JSONObject() : new JSONObject(response);
            throw new IllegalStateException(error.optString("error", "发送失败：HTTP " + status));
        }
    }

    private JSONObject postJson(String host, int port, String code, String path, byte[] body) throws Exception {
        HttpsURLConnection connection = open(host, port, code, path);
        connection.setRequestMethod("POST");
        connection.setDoOutput(true);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setFixedLengthStreamingMode(body.length);
        try (OutputStream output = connection.getOutputStream()) {
            output.write(body);
        }
        int status = connection.getResponseCode();
        captureFingerprint(connection);
        String response = readBody(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
        connection.disconnect();
        JSONObject value = response.isEmpty() ? new JSONObject() : new JSONObject(response);
        if (status >= 400) {
            throw new IllegalStateException(value.optString("error", "HTTP " + status));
        }
        return value;
    }

    private String sha256(Uri uri, Progress progress) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new BufferedInputStream(context.getContentResolver().openInputStream(uri))) {
            if (input == null) {
                throw new IllegalStateException("无法读取文件");
            }
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                digest.update(buffer, 0, count);
            }
        }
        StringBuilder value = new StringBuilder();
        for (byte item : digest.digest()) {
            value.append(String.format(Locale.US, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private static String readBody(InputStream input) throws Exception {
        if (input == null) {
            return "";
        }
        try (InputStream source = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            source.transferTo(output);
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    interface Progress {
        void update(long done, long total);
    }
}
