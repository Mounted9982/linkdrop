package com.linkdrop.android;

import android.content.Context;

import org.bouncycastle.asn1.x500.X500Name;
import org.bouncycastle.cert.X509CertificateHolder;
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter;
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder;
import org.bouncycastle.jce.provider.BouncyCastleProvider;
import org.bouncycastle.operator.ContentSigner;
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.math.BigInteger;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.Date;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;

final class TlsIdentity {
    private static final String ALIAS = "linkdrop";
    private final File keyStoreFile;
    private final char[] password = "linkdrop-local".toCharArray();
    private final BouncyCastleProvider bcProvider = new BouncyCastleProvider();
    private KeyStore keyStore;

    TlsIdentity(Context context) {
        keyStoreFile = new File(context.getFilesDir(), "identity.p12");
        ensure();
    }

    private void ensure() {
        try {
            keyStore = KeyStore.getInstance("PKCS12");
            if (keyStoreFile.exists()) {
                try (FileInputStream input = new FileInputStream(keyStoreFile)) {
                    keyStore.load(input, password);
                }
                if (keyStore.containsAlias(ALIAS)) {
                    return;
                }
            }
            keyStore.load(null, password);
            KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
            generator.initialize(2048, new SecureRandom());
            KeyPair pair = generator.generateKeyPair();
            Date now = new Date();
            X500Name subject = new X500Name("CN=LinkDrop Android, O=LinkDrop");
            JcaX509v3CertificateBuilder builder = new JcaX509v3CertificateBuilder(
                subject,
                new BigInteger(128, new SecureRandom()),
                new Date(now.getTime() - 86_400_000L),
                new Date(now.getTime() + 3_650L * 86_400_000L),
                subject,
                pair.getPublic()
            );
            ContentSigner signer = new JcaContentSignerBuilder("SHA256withRSA")
                .setProvider(bcProvider)
                .build(pair.getPrivate());
            X509CertificateHolder holder = builder.build(signer);
            X509Certificate certificate = new JcaX509CertificateConverter()
                .setProvider(bcProvider)
                .getCertificate(holder);
            keyStore.setKeyEntry(ALIAS, pair.getPrivate(), password, new Certificate[]{certificate});
            try (FileOutputStream output = new FileOutputStream(keyStoreFile)) {
                keyStore.store(output, password);
            }
        } catch (Exception exc) {
            throw new IllegalStateException("无法创建 TLS 身份", exc);
        }
    }

    String fingerprint() {
        try {
            Certificate certificate = keyStore.getCertificate(ALIAS);
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(certificate.getEncoded());
            StringBuilder value = new StringBuilder();
            for (byte item : digest) {
                if (value.length() > 0) {
                    value.append(':');
                }
                value.append(String.format(java.util.Locale.US, "%02X", item & 0xff));
            }
            return value.toString();
        } catch (Exception exc) {
            throw new IllegalStateException("无法读取 TLS 指纹", exc);
        }
    }

    SSLContext serverContext() {
        try {
            KeyManagerFactory managerFactory = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
            managerFactory.init(keyStore, password);
            SSLContext context = SSLContext.getInstance("TLS");
            context.init(managerFactory.getKeyManagers(), null, new SecureRandom());
            return context;
        } catch (Exception exc) {
            throw new IllegalStateException("无法启动 TLS 服务", exc);
        }
    }
}
