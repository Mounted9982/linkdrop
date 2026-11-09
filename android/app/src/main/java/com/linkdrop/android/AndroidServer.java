package com.linkdrop.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.wifi.WifiManager;
import android.os.Build;

import androidx.documentfile.provider.DocumentFile;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLServerSocket;

final class AndroidServer {
    static final int DISCOVERY_PORT = 48611;
    static final int BUFFER_SIZE = 64 * 1024;

    interface Listener {
        void onServerEvent(String type, JSONObject payload);
        void onIncomingNotification(JSONObject payload);
    }

    private static final class IncomingFile {
        String id;
        String relative;
        long size;
        String sha256;
        String token;
    }

    static final class IncomingSession {
        String id;
        String senderId;
        String senderName;
        String batch;
        final Map<String, IncomingFile> files = new LinkedHashMap<>();
        final Set<String> uploaded = ConcurrentHashMap.newKeySet();
        final CountDownLatch decision = new CountDownLatch(1);
        volatile boolean accepted;
        volatile DocumentFile directory;
        long createdAt = System.currentTimeMillis();

        JSONObject toJson() {
            JSONObject value = new JSONObject();
            JSONArray items = new JSONArray();
            long total = 0;
            for (IncomingFile item : files.values()) {
                JSONObject file = new JSONObject();
                try {
                    file.put("name", item.relative);
                    file.put("size", item.size);
                    items.put(file);
                } catch (JSONException ignored) {
                }
                total += item.size;
            }
            try {
                value.put("sessionId", id);
                value.put("sender", senderName);
                value.put("batch", batch);
                value.put("fileCount", files.size());
                value.put("totalSize", total);
                value.put("files", items);
            } catch (JSONException ignored) {
            }
            return value;
        }
    }

    private final Context context;
    private final AppState state;
    private final TlsIdentity identity;
    private final Listener listener;
    private final Set<String> sessionIds = ConcurrentHashMap.newKeySet();
    private final Map<String, IncomingSession> sessions = new ConcurrentHashMap<>();
    private final Map<String, JSONObject> peers = Collections.synchronizedMap(new LinkedHashMap<>());
    private final Map<String, List<Long>> failedAuth = new ConcurrentHashMap<>();
    private final AtomicBoolean running = new AtomicBoolean(false);
    private SSLServerSocket serverSocket;
    private DatagramSocket discoverySocket;
    private WifiManager.MulticastLock multicastLock;
    private Thread serverThread;
    private Thread discoveryThread;

    AndroidServer(Context context, AppState state, TlsIdentity identity, Listener listener) {
        this.context = context;
        this.state = state;
        this.identity = identity;
        this.listener = listener;
    }

    void start() {
        if (!running.compareAndSet(false, true)) {
            return;
        }
        try {
            SSLContext tls = identity.serverContext();
            serverSocket = (SSLServerSocket) tls.getServerSocketFactory().createServerSocket();
            serverSocket.setReuseAddress(true);
            serverSocket.bind(new InetSocketAddress(AppState.PORT));
            WifiManager wifi = (WifiManager) context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wifi != null) {
                multicastLock = wifi.createMulticastLock("LinkDropDiscovery");
                multicastLock.setReferenceCounted(false);
                multicastLock.acquire();
            }
            discoverySocket = new DatagramSocket(null);
            discoverySocket.setReuseAddress(true);
            discoverySocket.setBroadcast(true);
            discoverySocket.setSoTimeout(1000);
            discoverySocket.bind(new InetSocketAddress(DISCOVERY_PORT));
        } catch (Exception exc) {
            running.set(false);
            closeQuietly();
            throw new IllegalStateException("无法启动接收服务：" + exc.getMessage(), exc);
        }
        serverThread = new Thread(this::acceptLoop, "LinkDropHttps");
        serverThread.start();
        discoveryThread = new Thread(this::discoveryLoop, "LinkDropDiscovery");
        discoveryThread.start();
        scanPeers();
    }

    void stop() {
        running.set(false);
        closeQuietly();
        if (multicastLock != null && multicastLock.isHeld()) {
            multicastLock.release();
        }
    }

    private void closeQuietly() {
        try {
            if (serverSocket != null) {
                serverSocket.close();
            }
        } catch (IOException ignored) {
        }
        try {
            if (discoverySocket != null) {
                discoverySocket.close();
            }
        } catch (Exception ignored) {
        }
    }

    void respondIncoming(String sessionId, boolean accepted) {
        IncomingSession session = sessions.get(sessionId);
        if (session == null) {
            return;
        }
        session.accepted = accepted;
        session.decision.countDown();
    }

    void scanPeers() {
        if (!running.get()) {
            return;
        }
        JSONObject message = new JSONObject();
        try {
            message.put("app", "LinkDrop");
            message.put("version", 1);
            message.put("device_id", state.getDeviceId());
            message.put("device_name", state.getDeviceName());
            message.put("port", AppState.PORT);
            message.put("tls", true);
            message.put("cert_fingerprint", identity.fingerprint());
        } catch (JSONException ignored) {
        }
        byte[] bytes = message.toString().getBytes(StandardCharsets.UTF_8);
        try (DatagramSocket socket = new DatagramSocket()) {
            socket.setBroadcast(true);
            socket.send(new DatagramPacket(bytes, bytes.length, InetAddress.getByName("255.255.255.255"), DISCOVERY_PORT));
        } catch (Exception ignored) {
        }
    }

    private void emitPeers() {
        JSONArray array = new JSONArray();
        synchronized (peers) {
            for (JSONObject peer : peers.values()) {
                array.put(peer);
            }
        }
        JSONObject payload = new JSONObject();
        try {
            payload.put("peers", array);
        } catch (JSONException ignored) {
        }
        listener.onServerEvent("peers", payload);
    }

    private void discoveryLoop() {
        byte[] buffer = new byte[8192];
        while (running.get()) {
            try {
                DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                discoverySocket.receive(packet);
                String json = new String(packet.getData(), packet.getOffset(), packet.getLength(), StandardCharsets.UTF_8);
                JSONObject message = new JSONObject(json);
                if (!"LinkDrop".equals(message.optString("app"))) {
                    continue;
                }
                String deviceId = message.optString("device_id");
                if (deviceId.isEmpty() || deviceId.equals(state.getDeviceId())) {
                    continue;
                }
                int port = message.optInt("port", 0);
                if (port < 1 || port > 65535) {
                    continue;
                }
                JSONObject peer = new JSONObject();
                peer.put("id", deviceId);
                peer.put("name", message.optString("device_name", packet.getAddress().getHostAddress()));
                peer.put("address", packet.getAddress().getHostAddress() + ":" + port);
                peer.put("fingerprint", message.optString("cert_fingerprint"));
                peers.put(deviceId, peer);
                emitPeers();
                scanPeers();
            } catch (SocketTimeoutException ignored) {
            } catch (Exception ignored) {
                if (!running.get()) {
                    break;
                }
            }
        }
    }

    private void acceptLoop() {
        while (running.get()) {
            try {
                Socket socket = serverSocket.accept();
                socket.setSoTimeout(45_000);
                new Thread(() -> handle(socket), "LinkDropRequest").start();
            } catch (Exception ignored) {
                if (!running.get()) {
                    break;
                }
            }
        }
    }

    private static final class Request {
        String method;
        String path;
        Map<String, String> query = new HashMap<>();
        Map<String, String> headers = new HashMap<>();
        InputStream input;
    }

    private void handle(Socket socket) {
        boolean keepOpen = true;
        try {
            socket.setKeepAlive(true);
            Request request = parseRequest(socket.getInputStream());
            if (request == null) {
                sendJson(socket, 400, error("请求格式无效"));
                return;
            }
            if ("/api/health".equals(request.path)) {
                JSONObject value = baseInfo();
                value.put("ok", true);
                sendJson(socket, 200, value);
                keepOpen = false;
                return;
            }
            if (!authorized(socket, request)) {
                keepOpen = false;
                return;
            }
            if ("GET".equals(request.method)) {
                handleGet(socket, request);
            } else if ("POST".equals(request.method)) {
                handlePost(socket, request);
            } else {
                sendJson(socket, 405, error("不支持的请求方法"));
            }
            keepOpen = false;
        } catch (Exception exc) {
            try {
                sendJson(socket, 500, error(exc.getMessage() == null ? "服务器错误" : exc.getMessage()));
            } catch (Exception ignored) {
            }
        } finally {
            if (!keepOpen) {
                try {
                    socket.close();
                } catch (IOException ignored) {
                }
            }
        }
    }

    private JSONObject baseInfo() throws JSONException {
        JSONObject value = new JSONObject();
        value.put("ok", true);
        value.put("app", "LinkDrop");
        value.put("version", 1);
        value.put("device_id", state.getDeviceId());
        value.put("device_name", state.getDeviceName());
        value.put("allow_incoming", !state.getReceiveUri().isEmpty());
        value.put("cert_fingerprint", identity.fingerprint());
        value.put("tls", true);
        return value;
    }

    private boolean authorized(Socket socket, Request request) throws IOException {
        String address = socket.getInetAddress().getHostAddress();
        List<Long> failures = failedAuth.computeIfAbsent(address, key -> new ArrayList<>());
        long cutoff = System.currentTimeMillis() - 300_000L;
        synchronized (failures) {
            failures.removeIf(value -> value < cutoff);
            if (failures.size() >= 8) {
                sendJson(socket, 429, error("尝试次数过多，请 5 分钟后再试"));
                return false;
            }
        }
        String header = request.headers.getOrDefault("authorization", "");
        String supplied = header.toLowerCase(Locale.US).startsWith("bearer ")
            ? header.substring(7).trim() : "";
        byte[] expected = state.getPairCode().getBytes(StandardCharsets.UTF_8);
        if (!supplied.isEmpty() && MessageDigest.isEqual(expected, supplied.getBytes(StandardCharsets.UTF_8))) {
            synchronized (failures) {
                failures.clear();
            }
            return true;
        }
        synchronized (failures) {
            failures.add(System.currentTimeMillis());
        }
        sendJson(socket, 401, error("配对码错误或已变更"));
        return false;
    }

    private void handleGet(Socket socket, Request request) throws Exception {
        if ("/api/info".equals(request.path)) {
            sendJson(socket, 200, baseInfo());
        } else if ("/api/shares".equals(request.path)) {
            sendJson(socket, 200, sharesJson());
        } else if ("/api/list".equals(request.path)) {
            sendJson(socket, 200, listJson(request.query.getOrDefault("share_id", ""), request.query.getOrDefault("path", "")));
        } else if ("/api/download".equals(request.path)) {
            download(socket, request);
        } else if ("/api/zip".equals(request.path)) {
            downloadZip(socket, request);
        } else {
            sendJson(socket, 404, error("接口不存在"));
        }
    }

    private void handlePost(Socket socket, Request request) throws Exception {
        if ("/api/prepare".equals(request.path)) {
            prepare(socket, request);
        } else if ("/api/upload".equals(request.path)) {
            upload(socket, request);
        } else {
            sendJson(socket, 404, error("接口不存在"));
        }
    }

    private JSONObject sharesJson() {
        JSONArray array = new JSONArray();
        for (AppState.SharedFolder share : state.getShares()) {
            DocumentFile root = TreeAccess.tree(context, share.uri);
            JSONObject item = new JSONObject();
            try {
                item.put("id", share.id);
                item.put("name", share.name);
                item.put("available", root != null && root.exists());
                array.put(item);
            } catch (JSONException ignored) {
            }
        }
        JSONObject result = new JSONObject();
        try {
            result.put("ok", true);
            result.put("shares", array);
        } catch (JSONException ignored) {
        }
        return result;
    }

    private JSONObject listJson(String shareId, String relative) {
        JSONObject result = new JSONObject();
        try {
            AppState.SharedFolder share = state.findShare(shareId);
            if (share == null) {
                return error("共享文件夹不存在或共享已取消");
            }
            DocumentFile root = TreeAccess.tree(context, share.uri);
            if (root == null || !root.exists()) {
                return error("共享文件夹当前无法访问");
            }
            DocumentFile target = TreeAccess.resolve(root, relative, false);
            if (target == null || !target.exists()) {
                return error("文件或文件夹不存在");
            }
            JSONArray entries = new JSONArray();
            if (target.isFile()) {
                JSONObject entry = new JSONObject();
                entry.put("name", TreeAccess.displayName(target));
                entry.put("path", relative);
                entry.put("is_dir", false);
                entry.put("size", target.length());
                entry.put("modified", target.lastModified());
                entries.put(entry);
            } else {
                for (DocumentFile child : TreeAccess.children(target)) {
                    JSONObject entry = new JSONObject();
                    String childPath = relative.isEmpty()
                        ? TreeAccess.displayName(child)
                        : relative.replace('\\', '/') + "/" + TreeAccess.displayName(child);
                    entry.put("name", TreeAccess.displayName(child));
                    entry.put("path", childPath);
                    entry.put("is_dir", child.isDirectory());
                    entry.put("size", child.isDirectory() ? 0 : child.length());
                    entry.put("modified", child.lastModified());
                    entries.put(entry);
                }
            }
            result.put("ok", true);
            result.put("path", relative);
            result.put("entries", entries);
        } catch (Exception exc) {
            result = error(exc.getMessage() == null ? "读取目录失败" : exc.getMessage());
        }
        return result;
    }

    private void download(Socket socket, Request request) throws Exception {
        String shareId = request.query.getOrDefault("share_id", "");
        String relative = request.query.getOrDefault("path", "");
        DocumentFile file = resolveSharedFile(shareId, relative);
        if (file == null || !file.isFile()) {
            sendJson(socket, 404, error("文件不存在"));
            return;
        }
        sendHeaders(socket, 200, "application/octet-stream", file.length(), null);
        try (InputStream input = TreeAccess.openInput(context, file);
             OutputStream output = socket.getOutputStream()) {
            input.transferTo(output);
        }
    }

    private void downloadZip(Socket socket, Request request) throws Exception {
        String shareId = request.query.getOrDefault("share_id", "");
        String relative = request.query.getOrDefault("path", "");
        DocumentFile file = resolveSharedFile(shareId, relative);
        if (file == null || !file.isDirectory()) {
            sendJson(socket, 404, error("文件夹不存在"));
            return;
        }
        File temp = File.createTempFile("linkdrop-", ".zip", context.getCacheDir());
        try {
            try (ZipOutputStream zip = new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(temp)))) {
                addZipEntries(zip, file, TreeAccess.displayName(file));
            }
            sendHeaders(socket, 200, "application/zip", temp.length(), TreeAccess.displayName(file) + ".zip");
            try (InputStream input = new FileInputStream(temp);
                 OutputStream output = socket.getOutputStream()) {
                input.transferTo(output);
            }
        } finally {
            temp.delete();
        }
    }

    private void addZipEntries(ZipOutputStream zip, DocumentFile directory, String base) throws Exception {
        List<DocumentFile> children = TreeAccess.children(directory);
        if (children.isEmpty()) {
            zip.putNextEntry(new ZipEntry(base.replace('\\', '/') + "/"));
            zip.closeEntry();
            return;
        }
        for (DocumentFile child : children) {
            String path = base + "/" + TreeAccess.displayName(child);
            if (child.isDirectory()) {
                addZipEntries(zip, child, path);
            } else if (child.isFile()) {
                zip.putNextEntry(new ZipEntry(path.replace('\\', '/')));
                try (InputStream input = TreeAccess.openInput(context, child)) {
                    input.transferTo(zip);
                }
                zip.closeEntry();
            }
        }
    }

    private DocumentFile resolveSharedFile(String shareId, String relative) {
        try {
            AppState.SharedFolder share = state.findShare(shareId);
            if (share == null) {
                return null;
            }
            DocumentFile root = TreeAccess.tree(context, share.uri);
            return root == null ? null : TreeAccess.resolve(root, relative, false);
        } catch (Exception ignored) {
            return null;
        }
    }

    private void prepare(Socket socket, Request request) throws Exception {
        if (state.getReceiveUri().isEmpty()) {
            sendJson(socket, 403, error("请先在 Android 端选择接收文件夹"));
            return;
        }
        long lengthValue = contentLength(request);
        if (lengthValue < 0 || lengthValue > 8 * 1024 * 1024) {
            sendJson(socket, 400, error("请求内容过大"));
            return;
        }
        int length = (int) lengthValue;
        byte[] body = readExactly(request.input, length);
        JSONObject payload = new JSONObject(new String(body, StandardCharsets.UTF_8));
        JSONArray rawFiles = payload.optJSONArray("files");
        if (rawFiles == null || rawFiles.length() == 0 || rawFiles.length() > 10_000) {
            sendJson(socket, 400, error("传输内容为空或文件数量过多"));
            return;
        }
        IncomingSession session = new IncomingSession();
        session.id = UUID.randomUUID().toString().replace("-", "");
        session.senderId = payload.optString("device_id");
        session.senderName = payload.optString("device_name", "未知设备");
        session.batch = TreeAccess.sanitizeName(payload.optString("batch", "本次传输"));
        for (int index = 0; index < rawFiles.length(); index++) {
            JSONObject raw = rawFiles.getJSONObject(index);
            IncomingFile file = new IncomingFile();
            file.id = raw.optString("id");
            file.relative = raw.optString("relative_path").replace('\\', '/');
            file.size = raw.optLong("size", -1);
            file.sha256 = raw.optString("sha256").toLowerCase(Locale.US);
            file.token = UUID.randomUUID().toString().replace("-", "");
            if (file.id.isEmpty() || file.relative.isEmpty() || file.size < 0
                || !file.sha256.matches("[0-9a-f]{64}") || session.files.containsKey(file.id)) {
                sendJson(socket, 400, error("文件信息无效"));
                return;
            }
            session.files.put(file.id, file);
        }
        sessions.put(session.id, session);
        listener.onServerEvent("incoming_request", session.toJson());
        listener.onIncomingNotification(session.toJson());
        boolean accepted = session.decision.await(120, TimeUnit.SECONDS) && session.accepted;
        if (!accepted) {
            sessions.remove(session.id);
            sendJson(socket, 403, error("接收方拒绝或确认超时"));
            return;
        }
        DocumentFile receiveRoot = TreeAccess.tree(context, state.getReceiveUri());
        if (receiveRoot == null || !receiveRoot.canWrite()) {
            sessions.remove(session.id);
            sendJson(socket, 403, error("Android 接收目录不可写"));
            return;
        }
        session.directory = TreeAccess.uniqueDirectory(receiveRoot, session.batch);
        if (session.directory == null) {
            sessions.remove(session.id);
            sendJson(socket, 500, error("无法创建接收目录"));
            return;
        }
        JSONObject response = new JSONObject();
        response.put("ok", true);
        response.put("session_id", session.id);
        response.put("directory", session.directory.getUri().toString());
        JSONObject tokens = new JSONObject();
        for (IncomingFile file : session.files.values()) {
            tokens.put(file.id, file.token);
        }
        response.put("tokens", tokens);
        long total = 0;
        for (IncomingFile file : session.files.values()) {
            total += file.size;
        }
        response.put("total_size", total);
        sendJson(socket, 200, response);
    }

    private void upload(Socket socket, Request request) throws Exception {
        String sessionId = request.query.getOrDefault("session_id", "");
        String fileId = request.query.getOrDefault("file_id", "");
        String token = request.query.getOrDefault("token", "");
        IncomingSession session = sessions.get(sessionId);
        if (session == null || !session.accepted || session.directory == null) {
            sendJson(socket, 403, error("传输会话不存在或未获确认"));
            return;
        }
        IncomingFile file = session.files.get(fileId);
        if (file == null || !MessageDigest.isEqual(
            file.token.getBytes(StandardCharsets.UTF_8),
            token.getBytes(StandardCharsets.UTF_8)
        )) {
            sendJson(socket, 403, error("文件传输令牌无效"));
            return;
        }
        if (session.uploaded.contains(fileId)) {
            sendJson(socket, 409, error("文件已经接收完成"));
            return;
        }
        long length = contentLength(request);
        if (length != file.size) {
            sendJson(socket, 400, error("文件大小与会话信息不一致"));
            return;
        }
        String relative = file.relative.replace('\\', '/');
        int slash = relative.lastIndexOf('/');
        String parentPath = slash < 0 ? "" : relative.substring(0, slash);
        String name = slash < 0 ? relative : relative.substring(slash + 1);
        DocumentFile parent = TreeAccess.resolve(session.directory, parentPath, true);
        if (parent == null) {
            sendJson(socket, 500, error("无法创建接收子目录"));
            return;
        }
        DocumentFile target = TreeAccess.createFile(parent, name);
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long received = 0;
        try (OutputStream output = TreeAccess.openOutput(context, target)) {
            byte[] buffer = new byte[BUFFER_SIZE];
            while (received < file.size) {
                int count = request.input.read(buffer, 0, (int) Math.min(buffer.length, file.size - received));
                if (count < 0) {
                    throw new IOException("发送方连接中断");
                }
                output.write(buffer, 0, count);
                digest.update(buffer, 0, count);
                received += count;
            }
        } catch (Exception exc) {
            target.delete();
            throw exc;
        }
        String actual = hex(digest.digest());
        if (!MessageDigest.isEqual(
            actual.getBytes(StandardCharsets.UTF_8),
            file.sha256.getBytes(StandardCharsets.UTF_8)
        )) {
            target.delete();
            sendJson(socket, 422, error("SHA-256 校验失败，文件已丢弃"));
            return;
        }
        session.uploaded.add(fileId);
        JSONObject event = new JSONObject();
        event.put("path", target.getUri().toString());
        event.put("name", name);
        event.put("size", received);
        event.put("sender", session.senderName);
        listener.onServerEvent("incoming", event);
        if (session.uploaded.size() == session.files.size()) {
            JSONObject complete = new JSONObject();
            complete.put("directory", session.directory.getUri().toString());
            listener.onServerEvent("incoming_complete", complete);
        }
        JSONObject response = new JSONObject();
        response.put("ok", true);
        response.put("size", received);
        response.put("sha256", actual);
        sendJson(socket, 201, response);
    }

    private Request parseRequest(InputStream rawInput) throws IOException {
        BufferedInputStream input = new BufferedInputStream(rawInput);
        String requestLine = readLine(input);
        if (requestLine == null || requestLine.isEmpty()) {
            return null;
        }
        String[] parts = requestLine.split(" ");
        if (parts.length < 2) {
            return null;
        }
        Request request = new Request();
        request.method = parts[0];
        String target = parts[1];
        int queryIndex = target.indexOf('?');
        request.path = queryIndex >= 0 ? target.substring(0, queryIndex) : target;
        if (queryIndex >= 0) {
            for (String pair : target.substring(queryIndex + 1).split("&")) {
                String[] values = pair.split("=", 2);
                String key = URLDecoder.decode(values[0], StandardCharsets.UTF_8.name());
                String value = values.length > 1
                    ? URLDecoder.decode(values[1], StandardCharsets.UTF_8.name()) : "";
                request.query.put(key, value);
            }
        }
        String line;
        while ((line = readLine(input)) != null && !line.isEmpty()) {
            int colon = line.indexOf(':');
            if (colon > 0) {
                request.headers.put(
                    line.substring(0, colon).trim().toLowerCase(Locale.US),
                    line.substring(colon + 1).trim()
                );
            }
        }
        request.input = input;
        return request;
    }

    private static String readLine(InputStream input) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        int previous = -1;
        while (true) {
            int value = input.read();
            if (value < 0) {
                return buffer.size() == 0 ? null : buffer.toString(StandardCharsets.UTF_8.name());
            }
            if (previous == '\r' && value == '\n') {
                byte[] bytes = buffer.toByteArray();
                return new String(bytes, 0, Math.max(0, bytes.length - 1), StandardCharsets.UTF_8);
            }
            buffer.write(value);
            previous = value;
            if (buffer.size() > 32 * 1024) {
                throw new IOException("请求头过大");
            }
        }
    }

    private long contentLength(Request request) {
        try {
            return Long.parseLong(request.headers.getOrDefault("content-length", "-1"));
        } catch (NumberFormatException exc) {
            return -1;
        }
    }

    private void sendJson(Socket socket, int status, JSONObject value) throws IOException {
        byte[] body = value.toString().getBytes(StandardCharsets.UTF_8);
        OutputStream output = socket.getOutputStream();
        String header = "HTTP/1.1 " + status + " " + reason(status) + "\r\n"
            + "Content-Type: application/json; charset=utf-8\r\n"
            + "Content-Length: " + body.length + "\r\n"
            + "Connection: close\r\n\r\n";
        output.write(header.getBytes(StandardCharsets.UTF_8));
        output.write(body);
        output.flush();
    }

    private void sendHeaders(Socket socket, int status, String contentType, long size, String filename) throws IOException {
        OutputStream output = socket.getOutputStream();
        StringBuilder header = new StringBuilder()
            .append("HTTP/1.1 ").append(status).append(' ').append(reason(status)).append("\r\n")
            .append("Content-Type: ").append(contentType).append("\r\n")
            .append("Content-Length: ").append(size).append("\r\n");
        if (filename != null && !filename.isEmpty()) {
            String encoded = java.net.URLEncoder.encode(filename, StandardCharsets.UTF_8.name()).replace("+", "%20");
            header.append("Content-Disposition: attachment; filename*=UTF-8''").append(encoded).append("\r\n");
        }
        header.append("Connection: close\r\n\r\n");
        output.write(header.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static String reason(int status) {
        switch (status) {
            case 200:
                return "OK";
            case 201:
                return "Created";
            case 400:
                return "Bad Request";
            case 401:
                return "Unauthorized";
            case 403:
                return "Forbidden";
            case 404:
                return "Not Found";
            case 409:
                return "Conflict";
            case 422:
                return "Unprocessable Entity";
            case 429:
                return "Too Many Requests";
            case 500:
                return "Internal Server Error";
            default:
                return "Error";
        }
    }

    private static JSONObject error(String message) {
        JSONObject value = new JSONObject();
        try {
            value.put("ok", false);
            value.put("error", message);
        } catch (JSONException ignored) {
        }
        return value;
    }

    private static String hex(byte[] bytes) {
        StringBuilder value = new StringBuilder();
        for (byte item : bytes) {
            value.append(String.format(Locale.US, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private static byte[] readExactly(InputStream input, int length) throws IOException {
        byte[] value = new byte[length];
        int offset = 0;
        while (offset < length) {
            int count = input.read(value, offset, length - offset);
            if (count < 0) {
                throw new IOException("请求内容不完整");
            }
            offset += count;
        }
        return value;
    }
}
