package com.linkdrop.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class LinkDropService extends Service implements AndroidServer.Listener {
    interface UiListener {
        void onEvent(JSONObject value);
    }

    private static final String CHANNEL_ID = "linkdrop_service";
    private static final int NOTIFICATION_ID = 48610;
    private static final int INCOMING_NOTIFICATION_ID = 48611;
    static volatile LinkDropService instance;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final Map<String, JSONObject> pendingIncoming = Collections.synchronizedMap(new LinkedHashMap<>());
    private final List<JSONObject> recentIncoming = Collections.synchronizedList(new ArrayList<>());
    private AppState state;
    private TlsIdentity identity;
    private AndroidServer server;
    private RemoteApi remoteApi;
    private volatile UiListener uiListener;
    private RemoteApi.Connected connection;
    private JSONArray lastPeers = new JSONArray();

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        state = new AppState(this);
        identity = new TlsIdentity(this);
        remoteApi = new RemoteApi(this);
        createNotificationChannel();
        startForeground(NOTIFICATION_ID, serviceNotification("正在启动接收服务"));
        server = new AndroidServer(this, state, identity, this);
        try {
            server.start();
            updateServiceNotification("局域网接收服务运行中");
        } catch (Exception exc) {
            emit("server_error", json("message", exc.getMessage()));
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        if (server != null) {
            server.stop();
        }
        instance = null;
        super.onDestroy();
    }

    static LinkDropService get() {
        return instance;
    }

    void registerListener(UiListener listener) {
        uiListener = listener;
        listener.onEvent(event("state", getState()));
        synchronized (pendingIncoming) {
            for (JSONObject item : pendingIncoming.values()) {
                listener.onEvent(event("incoming_request", item));
            }
        }
    }

    void unregisterListener(UiListener listener) {
        if (uiListener == listener) {
            uiListener = null;
        }
    }

    JSONObject getState() {
        JSONObject value = new JSONObject();
        try {
            value.put("deviceName", state.getDeviceName());
            value.put("pairCode", state.getPairCode());
            value.put("port", AppState.PORT);
            value.put("addresses", new JSONArray(localAddresses()));
            value.put("receiveUri", state.getReceiveUri());
            value.put("fingerprint", identity.fingerprint());
            JSONArray shares = new JSONArray();
            for (AppState.SharedFolder share : state.getShares()) {
                JSONObject item = new JSONObject();
                item.put("id", share.id);
                item.put("name", share.name);
                item.put("uri", share.uri);
                shares.put(item);
            }
            value.put("shares", shares);
            value.put("peers", lastPeers);
            JSONArray history = new JSONArray();
            synchronized (recentIncoming) {
                for (JSONObject item : recentIncoming) {
                    history.put(item);
                }
            }
            value.put("recentIncoming", history);
            value.put("connected", connection != null);
            if (connection != null) {
                value.put("remoteInfo", connection.info);
                value.put("remoteShares", connection.shares);
            }
        } catch (Exception ignored) {
        }
        return value;
    }

    private List<String> localAddresses() {
        List<String> result = new ArrayList<>();
        try {
            for (NetworkInterface network : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!network.isUp() || network.isLoopback()) {
                    continue;
                }
                for (InetAddress address : Collections.list(network.getInetAddresses())) {
                    if (address instanceof Inet4Address && !address.isLoopbackAddress()) {
                        result.add(address.getHostAddress() + ":" + AppState.PORT);
                    }
                }
            }
        } catch (Exception ignored) {
        }
        Collections.sort(result);
        return result;
    }

    void updateIdentity(String name, String code) {
        state.setDeviceName(name);
        state.setPairCode(code);
        emit("state", getState());
    }

    void setReceiveUri(Uri uri) {
        TreeAccess.persistReadWritePermission(this, uri);
        state.setReceiveUri(uri.toString());
        emit("state", getState());
    }

    void addShare(Uri uri, String name) {
        TreeAccess.persistReadPermission(this, uri);
        state.addShare(uri.toString(), name);
        emit("state", getState());
    }

    void removeShare(String id) {
        state.removeShare(id);
        emit("state", getState());
    }

    void scanPeers() {
        server.scanPeers();
    }

    void respondIncoming(String sessionId, boolean accepted) {
        pendingIncoming.remove(sessionId);
        server.respondIncoming(sessionId, accepted);
        emit("incoming_answered", json("sessionId", sessionId, "accepted", accepted));
    }

    void connect(String address, String code) {
        runBackground(() -> {
            try {
                String[] hostPort = parseAddress(address);
                RemoteApi.Connected connected = remoteApi.connect(hostPort[0], Integer.parseInt(hostPort[1]), code);
                String deviceId = connected.info.optString("device_id");
                if (!state.trustDevice(deviceId, connected.fingerprint)) {
                    throw new IllegalStateException("设备 TLS 身份与上次连接不一致");
                }
                connection = connected;
                emit("connected", connected.info);
                emit("state", getState());
            } catch (Exception exc) {
                emit("error", json("message", exc.getMessage() == null ? "连接失败" : exc.getMessage()));
            }
        });
    }

    void listRemote(String shareId, String path) {
        RemoteApi.Connected selected = connection;
        if (selected == null) {
            emit("error", json("message", "请先连接 Windows 设备"));
            return;
        }
        runBackground(() -> {
            try {
                JSONObject result = remoteApi.list(selected.host, selected.port, selected.code, shareId, path);
                JSONObject payload = new JSONObject();
                payload.put("shareId", shareId);
                payload.put("path", path);
                payload.put("entries", result.optJSONArray("entries"));
                emit("remote_entries", payload);
            } catch (Exception exc) {
                emit("error", json("message", exc.getMessage() == null ? "读取目录失败" : exc.getMessage()));
            }
        });
    }

    void downloadRemote(String shareId, String path, Uri destination) {
        RemoteApi.Connected selected = connection;
        if (selected == null) {
            emit("error", json("message", "请先连接 Windows 设备"));
            return;
        }
        emit("transfer_start", json("message", "正在下载", "mode", "download"));
        runBackground(() -> {
            try {
                remoteApi.download(selected, shareId, path, destination, (done, total) ->
                    emit("transfer_progress", json("done", done, "total", total))
                );
                emit("transfer_done", json("message", "下载完成", "uri", destination.toString()));
            } catch (Exception exc) {
                emit("transfer_error", json("message", exc.getMessage() == null ? "下载失败" : exc.getMessage()));
            }
        });
    }

    void sendFiles(List<RemoteApi.SelectedFile> files, String batch) {
        RemoteApi.Connected selected = connection;
        if (selected == null) {
            emit("error", json("message", "请先连接 Windows 设备"));
            return;
        }
        emit("transfer_start", json("message", "正在计算校验值并准备发送", "mode", "send"));
        runBackground(() -> {
            try {
                remoteApi.send(selected, files, batch, (done, total) ->
                    emit("transfer_progress", json("done", done, "total", total))
                );
                emit("transfer_done", json("message", "发送完成"));
            } catch (Exception exc) {
                emit("transfer_error", json("message", exc.getMessage() == null ? "发送失败" : exc.getMessage()));
            }
        });
    }

    private void runBackground(Runnable action) {
        new Thread(action, "LinkDropClient").start();
    }

    private String[] parseAddress(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.startsWith("[")) {
            int end = value.indexOf("]:");
            if (end > 0) {
                return new String[]{value.substring(1, end), value.substring(end + 2)};
            }
        }
        int colon = value.lastIndexOf(':');
        if (colon > 0 && value.indexOf(':') == colon) {
            return new String[]{value.substring(0, colon), value.substring(colon + 1)};
        }
        return new String[]{value, String.valueOf(AppState.PORT)};
    }

    @Override
    public void onServerEvent(String type, JSONObject payload) {
        if ("peers".equals(type)) {
            lastPeers = payload.optJSONArray("peers");
        } else if ("incoming".equals(type)) {
            try {
                payload.put("time", System.currentTimeMillis());
            } catch (Exception ignored) {
            }
            recentIncoming.add(0, payload);
            while (recentIncoming.size() > 50) {
                recentIncoming.remove(recentIncoming.size() - 1);
            }
        } else if ("incoming_request".equals(type)) {
            pendingIncoming.put(payload.optString("sessionId"), payload);
        }
        emit(type, payload);
    }

    @Override
    public void onIncomingNotification(JSONObject payload) {
        Intent intent = new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pending = PendingIntent.getActivity(
            this,
            1,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("收到文件请求")
            .setContentText(payload.optString("sender", "另一台设备") + " 正在等待你确认")
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.notify(INCOMING_NOTIFICATION_ID, notification);
    }

    private void emit(String type, JSONObject payload) {
        UiListener listener = uiListener;
        if (listener != null) {
            JSONObject value = event(type, payload);
            main.post(() -> {
                UiListener current = uiListener;
                if (current != null) {
                    current.onEvent(value);
                }
            });
        }
    }

    private static JSONObject event(String type, JSONObject payload) {
        JSONObject value = new JSONObject();
        try {
            value.put("type", type);
            value.put("payload", payload);
        } catch (Exception ignored) {
        }
        return value;
    }

    private static JSONObject json(Object... values) {
        JSONObject result = new JSONObject();
        for (int index = 0; index + 1 < values.length; index += 2) {
            try {
                result.put(String.valueOf(values[index]), values[index + 1]);
            } catch (Exception ignored) {
            }
        }
        return result;
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "LinkDrop 文件接收",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("保持局域网文件接收服务运行");
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    private Notification serviceNotification(String text) {
        Intent intent = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("LinkDrop")
            .setContentText(text)
            .setContentIntent(pending)
            .setOngoing(true)
            .build();
    }

    private void updateServiceNotification(String text) {
        getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, serviceNotification(text));
    }
}
