package com.linkdrop.android;

import android.content.ContentResolver;
import android.content.Context;
import android.net.Uri;
import android.webkit.MimeTypeMap;

import androidx.documentfile.provider.DocumentFile;

import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

final class TreeAccess {
    private TreeAccess() {
    }

    static DocumentFile tree(Context context, String uri) {
        if (uri == null || uri.isEmpty()) {
            return null;
        }
        return DocumentFile.fromTreeUri(context, Uri.parse(uri));
    }

    static DocumentFile resolve(DocumentFile root, String relative, boolean createDirectories) {
        DocumentFile current = root;
        if (relative == null || relative.trim().isEmpty() || relative.equals(".")) {
            return root;
        }
        for (String segment : relative.replace('\\', '/').split("/")) {
            if (segment.isEmpty() || segment.equals(".") || segment.equals("..") || segment.contains(":")) {
                throw new IllegalArgumentException("路径无效");
            }
            DocumentFile child = current.findFile(segment);
            if (child == null && createDirectories) {
                child = current.createDirectory(segment);
            }
            if (child == null) {
                return null;
            }
            current = child;
        }
        return current;
    }

    static DocumentFile uniqueDirectory(DocumentFile root, String requested) {
        String safe = sanitizeName(requested);
        DocumentFile candidate = root.findFile(safe);
        if (candidate == null) {
            return root.createDirectory(safe);
        }
        for (int index = 2; index < 10_000; index++) {
            String name = safe + " (" + index + ")";
            candidate = root.findFile(name);
            if (candidate == null) {
                return root.createDirectory(name);
            }
        }
        throw new IllegalStateException("无法生成不重复的接收目录");
    }

    static String sanitizeName(String raw) {
        String value = raw == null ? "" : raw.trim();
        value = value.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
        while (value.endsWith(".") || value.endsWith(" ")) {
            value = value.substring(0, value.length() - 1);
        }
        if (value.isEmpty() || value.equals(".") || value.equals("..")) {
            value = "LinkDrop";
        }
        return value.substring(0, Math.min(128, value.length()));
    }

    static String mimeType(String name) {
        int dot = name.lastIndexOf('.');
        String extension = dot >= 0 ? name.substring(dot + 1) : "";
        String value = MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension.toLowerCase());
        return value == null ? "application/octet-stream" : value;
    }

    static DocumentFile createFile(DocumentFile directory, String name) {
        String safe = sanitizeName(name);
        DocumentFile existing = directory.findFile(safe);
        if (existing != null) {
            throw new IllegalStateException("同名文件已存在");
        }
        DocumentFile file = directory.createFile(mimeType(safe), safe);
        if (file == null) {
            throw new IllegalStateException("无法创建接收文件");
        }
        return file;
    }

    static List<DocumentFile> children(DocumentFile directory) {
        List<DocumentFile> result = new ArrayList<>();
        for (DocumentFile child : directory.listFiles()) {
            result.add(child);
        }
        result.sort((left, right) -> {
            if (left.isDirectory() != right.isDirectory()) {
                return left.isDirectory() ? -1 : 1;
            }
            return displayName(left).compareToIgnoreCase(displayName(right));
        });
        return result;
    }

    static String displayName(DocumentFile file) {
        String name = file.getName();
        if (name == null || name.isEmpty()) {
            String path = file.getUri().getLastPathSegment();
            return path == null ? "未命名" : path;
        }
        return name;
    }

    static InputStream openInput(Context context, DocumentFile file) throws Exception {
        InputStream input = context.getContentResolver().openInputStream(file.getUri());
        if (input == null) {
            throw new IllegalStateException("无法读取文件");
        }
        return input;
    }

    static OutputStream openOutput(Context context, DocumentFile file) throws Exception {
        OutputStream output = context.getContentResolver().openOutputStream(file.getUri(), "wt");
        if (output == null) {
            throw new IllegalStateException("无法写入文件");
        }
        return output;
    }

    static void persistReadPermission(Context context, Uri uri) {
        try {
            context.getContentResolver().takePersistableUriPermission(
                uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
            );
        } catch (SecurityException ignored) {
        }
    }

    static void persistReadWritePermission(Context context, Uri uri) {
        try {
            context.getContentResolver().takePersistableUriPermission(
                uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            );
        } catch (SecurityException exc) {
            persistReadPermission(context, uri);
        }
    }
}
