# LinkDrop

LinkDrop 是一个跨平台文件直传项目，包含 Windows 桌面端和 Android 客户端。两端可以互相发现、浏览共享目录、直接发送文件，并通过一次性口令完成公网加密传输。

## 功能

- Windows 与 Android 局域网 UDP 自动发现
- 可手动输入 `IP:端口` 连接
- 只读共享任意用户选择的目录
- 单文件、多文件和整个文件夹发送
- 文件夹层级保留
- 文件夹打包 ZIP 下载
- TLS 加密和首次连接证书固定
- 6 位配对码鉴权与错误次数限制
- 接收方逐批确认或拒绝
- SHA-256 完整性校验
- Windows 端内置 croc 公网传输
- Android 端通过 croc 官方网页进行公网传输
- Android 使用系统 SAF 目录授权，不申请全盘存储权限

## 目录

```text
windows/   Python + Tkinter Windows 桌面端
android/   Java + WebView Android 客户端
```

## Windows

要求 Python 3.11+、Tkinter 和 PowerShell。

```powershell
cd windows
.\build.ps1
```

构建脚本会在首次运行时下载经过 SHA-256 校验的 croc v11.6.5 Windows 组件，然后生成：

```text
windows\dist\LinkDrop.exe
```

开发运行：

```powershell
python -m pip install -r windows\requirements.txt
python windows\run.pyw
```

## Android

要求 JDK 17+、Android SDK 35 和 Android 8.0+ 设备。

```powershell
cd android
.\build.ps1
```

输出：

```text
android\dist\LinkDrop-Android-1.0.0.apk
```

首次使用时，需要在“共享”页选择一个接收文件夹。Android 11+ 通常不允许直接选择 Download 根目录，请先在 Download 下建立 `LinkDrop` 子目录。

公网传输在 Android 上跳转到 croc 官方网页，因为 Android 10+ 不允许可靠地直接执行应用私有目录中的二进制文件。

## 互通方式

1. 两台设备运行 LinkDrop 并连接同一局域网。
2. 在发送端输入接收端显示的局域网地址和 6 位配对码。
3. 接收端确认后开始传输。
4. 传输完成后两端会校验 SHA-256。

如果没有可访问的局域网或公网地址，可使用“公网传输”页的一次性口令。

## 安全模型

- 共享文件夹默认只读，只有用户明确授权的目录才可访问。
- Windows 使用应用自动生成的自签名 TLS 证书。
- Android 使用首次启动时生成的独立 TLS 身份。
- 首次成功连接后会固定对方证书指纹，证书变化会阻止后续连接。
- 配对码不会通过局域网发现广播。
- 接收文件先写入临时文件，SHA-256 不一致时自动删除。
- 收到文件不会被执行。

## 已知边界

- 局域网发现可能受到 AP 隔离、VPN 或 Windows 防火墙影响。
- 直接输入地址连接要求接收端网络可达。
- 公网模式依赖 croc 服务和对应中继端口。
- 当前 Android APK 使用调试密钥签名，适合侧载和内部使用，不适合应用商店。

## 状态

- Windows 1.1.0
- Android 1.0.0
- Android 构建已在 Android 15 上验证
- Windows ↔ Android 双向发送、接收确认、共享浏览、下载和 SHA-256 已通过测试

