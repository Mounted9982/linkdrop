package com.linkdrop.android;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

final class AppState {
    static final String PREFS = "linkdrop";
    static final int PORT = 48610;

    static final class SharedFolder {
        final String id;
        final String name;
        final String uri;

        SharedFolder(String id, String name, String uri) {
            this.id = id;
            this.name = name;
            this.uri = uri;
        }
    }

    private final SharedPreferences prefs;

    AppState(Context context) {
        prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (getDeviceId().isEmpty()) {
            prefs.edit()
                .putString("device_id", UUID.randomUUID().toString().replace("-", ""))
                .apply();
        }
        if (getDeviceName().isEmpty()) {
            String model = android.os.Build.MODEL == null ? "Android" : android.os.Build.MODEL.trim();
            prefs.edit().putString("device_name", model.isEmpty() ? "Android 设备" : model).apply();
        }
        if (getPairCode().isEmpty()) {
            prefs.edit().putString("pair_code", randomCode()).apply();
        }
    }

    String getDeviceId() {
        return prefs.getString("device_id", "");
    }

    String getDeviceName() {
        return prefs.getString("device_name", "");
    }

    void setDeviceName(String name) {
        String cleaned = name == null ? "" : name.trim();
        if (!cleaned.isEmpty()) {
            prefs.edit().putString("device_name", cleaned.substring(0, Math.min(48, cleaned.length()))).apply();
        }
    }

    String getPairCode() {
        return prefs.getString("pair_code", "");
    }

    void setPairCode(String code) {
        if (code != null && code.matches("\\d{6}")) {
            prefs.edit().putString("pair_code", code).apply();
        }
    }

    String randomCode() {
        return String.format(java.util.Locale.US, "%06d", new SecureRandom().nextInt(1_000_000));
    }

    String getReceiveUri() {
        return prefs.getString("receive_uri", "");
    }

    void setReceiveUri(String uri) {
        prefs.edit().putString("receive_uri", uri == null ? "" : uri).apply();
    }

    List<SharedFolder> getShares() {
        List<SharedFolder> result = new ArrayList<>();
        try {
            JSONArray array = new JSONArray(prefs.getString("shares", "[]"));
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.getJSONObject(i);
                result.add(new SharedFolder(
                    item.optString("id"),
                    item.optString("name"),
                    item.optString("uri")
                ));
            }
        } catch (JSONException ignored) {
        }
        return result;
    }

    SharedFolder findShare(String id) {
        for (SharedFolder share : getShares()) {
            if (share.id.equals(id)) {
                return share;
            }
        }
        return null;
    }

    SharedFolder addShare(String uri, String name) {
        List<SharedFolder> shares = getShares();
        for (SharedFolder existing : shares) {
            if (existing.uri.equals(uri)) {
                return existing;
            }
        }
        SharedFolder share = new SharedFolder(
            UUID.randomUUID().toString().replace("-", "").substring(0, 12),
            name == null || name.trim().isEmpty() ? "共享文件夹" : name.trim(),
            uri
        );
        shares.add(share);
        persistShares(shares);
        return share;
    }

    void removeShare(String id) {
        List<SharedFolder> shares = getShares();
        shares.removeIf(item -> item.id.equals(id));
        persistShares(shares);
    }

    private void persistShares(List<SharedFolder> shares) {
        JSONArray array = new JSONArray();
        for (SharedFolder share : shares) {
            JSONObject item = new JSONObject();
            try {
                item.put("id", share.id);
                item.put("name", share.name);
                item.put("uri", share.uri);
                array.put(item);
            } catch (JSONException ignored) {
            }
        }
        prefs.edit().putString("shares", array.toString()).apply();
    }

    boolean trustDevice(String deviceId, String fingerprint) {
        if (deviceId == null || deviceId.isEmpty() || fingerprint == null || fingerprint.isEmpty()) {
            return false;
        }
        String key = "trust_" + deviceId;
        String previous = prefs.getString(key, "");
        if (!previous.isEmpty() && !previous.equals(fingerprint)) {
            return false;
        }
        if (previous.isEmpty()) {
            prefs.edit().putString(key, fingerprint).apply();
        }
        return true;
    }
}

