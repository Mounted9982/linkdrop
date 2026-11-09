package com.linkdrop.android;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ApplicationInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import androidx.documentfile.provider.DocumentFile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public final class MainActivity extends Activity implements LinkDropService.UiListener {
    private static final int PICK_SHARE_FOLDER = 1001;
    private static final int PICK_RECEIVE_FOLDER = 1002;
    private static final int PICK_SEND_FILES = 1003;
    private static final int PICK_SEND_FOLDER = 1004;
    private static final int PICK_DOWNLOAD_TARGET = 1005;

    private WebView webView;
    private boolean webReady;
    private List<RemoteApi.SelectedFile> selectedFiles = new ArrayList<>();
    private String selectedBatch = "";
    private String pendingShareId;
    private String pendingSharePath;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFFF4F6FA);
        webView = new WebView(this);
        webView.setBackgroundColor(0xFFF4F6FA);
        root.addView(webView, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));
        setContentView(root);
        configureWebView();
        requestNotificationPermission();
        startService(new Intent(this, LinkDropService.class));
        webView.loadUrl("file:///android_asset/index.html");
    }

    private void configureWebView() {
        WebView.setWebContentsDebuggingEnabled(
            (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0
        );
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        webView.addJavascriptInterface(new NativeBridge(), "AndroidNative");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                webReady = true;
                attachService();
            }
        });
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
            && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2001);
        }
    }

    private void attachService() {
        LinkDropService service = LinkDropService.get();
        if (service == null) {
            webView.postDelayed(this::attachService, 250);
            return;
        }
        service.registerListener(this);
    }

    @Override
    public void onEvent(JSONObject value) {
        if (!webReady || webView == null) {
            return;
        }
        String script = "window.LinkDropNative&&window.LinkDropNative.dispatch("
            + JSONObject.quote(value.toString()) + ")";
        webView.evaluateJavascript(script, null);
    }

    private void dispatchLocal(String type, JSONObject payload) {
        JSONObject wrapper = new JSONObject();
        try {
            wrapper.put("type", type);
            wrapper.put("payload", payload);
            onEvent(wrapper);
        } catch (Exception ignored) {
        }
    }

    private final class NativeBridge {
        @JavascriptInterface
        public String getState() {
            LinkDropService service = LinkDropService.get();
            return service == null ? "{}" : service.getState().toString();
        }

        @JavascriptInterface
        public void updateIdentity(String name, String code) {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.updateIdentity(name, code);
            }
        }

        @JavascriptInterface
        public void addSharedFolder() {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            startActivityForResult(intent, PICK_SHARE_FOLDER);
        }

        @JavascriptInterface
        public void chooseReceiveFolder() {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            startActivityForResult(intent, PICK_RECEIVE_FOLDER);
        }

        @JavascriptInterface
        public void removeShare(String id) {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.removeShare(id);
            }
        }

        @JavascriptInterface
        public void scanPeers() {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.scanPeers();
            }
        }

        @JavascriptInterface
        public void connect(String address, String code) {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.connect(address, code);
            }
        }

        @JavascriptInterface
        public void listRemote(String shareId, String path) {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.listRemote(shareId, path);
            }
        }

        @JavascriptInterface
        public void chooseSendFiles() {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("*/*")
                .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            startActivityForResult(intent, PICK_SEND_FILES);
        }

        @JavascriptInterface
        public void chooseSendFolder() {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
            startActivityForResult(intent, PICK_SEND_FOLDER);
        }

        @JavascriptInterface
        public void sendSelected() {
            LinkDropService service = LinkDropService.get();
            if (service == null) {
                return;
            }
            if (selectedFiles.isEmpty()) {
                dispatchLocal("selection", new JSONObject());
                return;
            }
            service.sendFiles(new ArrayList<>(selectedFiles), selectedBatch);
        }

        @JavascriptInterface
        public void downloadRemote(String shareId, String path, String name) {
            pendingShareId = shareId;
            pendingSharePath = path;
            Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("application/octet-stream")
                .putExtra(Intent.EXTRA_TITLE, name);
            startActivityForResult(intent, PICK_DOWNLOAD_TARGET);
        }

        @JavascriptInterface
        public void respondIncoming(String sessionId, boolean accepted) {
            LinkDropService service = LinkDropService.get();
            if (service != null) {
                service.respondIncoming(sessionId, accepted);
            }
        }

        @JavascriptInterface
        public void openCroc(String code) {
            String url = "https://getcroc.com/";
            if (code != null && !code.trim().isEmpty()) {
                try {
                    url += "?code=" + java.net.URLEncoder.encode(code.trim(), "UTF-8");
                } catch (Exception ignored) {
                }
            }
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        }

        @JavascriptInterface
        public void openUri(String uri) {
            if (uri == null || uri.isEmpty()) {
                return;
            }
            try {
                Intent intent = new Intent(Intent.ACTION_VIEW);
                intent.setDataAndType(Uri.parse(uri), DocumentsContract.Document.MIME_TYPE_DIR);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(intent);
            } catch (Exception exc) {
                dispatchLocal("error", message("无法打开目录：" + exc.getMessage()));
            }
        }

        @JavascriptInterface
        public void copyText(String text) {
            ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            clipboard.setPrimaryClip(ClipData.newPlainText("LinkDrop", text == null ? "" : text));
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        LinkDropService service = LinkDropService.get();
        if (service == null) {
            dispatchLocal("error", message("接收服务尚未启动"));
            return;
        }
        if (requestCode == PICK_SHARE_FOLDER) {
            DocumentFile root = DocumentFile.fromTreeUri(this, uri);
            String name = root == null ? "共享文件夹" : TreeAccess.displayName(root);
            service.addShare(uri, name);
        } else if (requestCode == PICK_RECEIVE_FOLDER) {
            service.setReceiveUri(uri);
        } else if (requestCode == PICK_SEND_FILES) {
            collectFiles(data);
        } else if (requestCode == PICK_SEND_FOLDER) {
            collectFolder(uri);
        } else if (requestCode == PICK_DOWNLOAD_TARGET) {
            if (pendingShareId != null) {
                service.downloadRemote(pendingShareId, pendingSharePath, uri);
            }
        }
    }

    private void collectFiles(Intent data) {
        selectedFiles = new ArrayList<>();
        selectedBatch = "Android_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        if (data.getClipData() != null) {
            ClipData clip = data.getClipData();
            for (int index = 0; index < clip.getItemCount(); index++) {
                addSelected(clip.getItemAt(index).getUri(), "");
            }
        } else if (data.getData() != null) {
            addSelected(data.getData(), "");
        }
        dispatchSelection();
    }

    private void collectFolder(Uri uri) {
        selectedFiles = new ArrayList<>();
        DocumentFile root = DocumentFile.fromTreeUri(this, uri);
        if (root == null) {
            dispatchLocal("error", message("无法读取所选文件夹"));
            return;
        }
        String rootName = TreeAccess.displayName(root);
        selectedBatch = rootName;
        collectTree(root, "");
        dispatchSelection();
    }

    private void collectTree(DocumentFile directory, String relativeBase) {
        for (DocumentFile child : directory.listFiles()) {
            String relative = relativeBase.isEmpty()
                ? TreeAccess.displayName(child)
                : relativeBase + "/" + TreeAccess.displayName(child);
            if (child.isDirectory()) {
                collectTree(child, relative);
            } else if (child.isFile()) {
                TreeAccess.persistReadPermission(this, child.getUri());
                selectedFiles.add(new RemoteApi.SelectedFile(
                    child.getUri(),
                    TreeAccess.displayName(child),
                    relative,
                    child.length()
                ));
            }
        }
    }

    private void addSelected(Uri uri, String relative) {
        DocumentFile file = DocumentFile.fromSingleUri(this, uri);
        String name = file == null ? "文件" : TreeAccess.displayName(file);
        long size = file == null ? 0 : file.length();
        TreeAccess.persistReadPermission(this, uri);
        selectedFiles.add(new RemoteApi.SelectedFile(
            uri,
            name,
            relative.isEmpty() ? name : relative,
            size
        ));
    }

    private void dispatchSelection() {
        JSONObject payload = new JSONObject();
        try {
            payload.put("count", selectedFiles.size());
            payload.put("batch", selectedBatch);
            JSONArray names = new JSONArray();
            for (int index = 0; index < Math.min(selectedFiles.size(), 20); index++) {
                names.put(selectedFiles.get(index).name);
            }
            payload.put("names", names);
        } catch (Exception ignored) {
        }
        dispatchLocal("selection", payload);
    }

    private static JSONObject message(String text) {
        JSONObject value = new JSONObject();
        try {
            value.put("message", text);
        } catch (Exception ignored) {
        }
        return value;
    }

    @Override
    protected void onDestroy() {
        LinkDropService service = LinkDropService.get();
        if (service != null) {
            service.unregisterListener(this);
        }
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
