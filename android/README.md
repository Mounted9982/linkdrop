# LinkDrop Android 1.0

LinkDrop Android 是与 Windows 版 LinkDrop 1.1+ 互通的局域网文件传输客户端。

## 已实现

- Android 8.0 及以上
- 局域网 UDP 自动发现
- 与 Windows 版一致的 HTTPS API
- 6 位配对码鉴权
- TLS 加密与首次连接证书指纹固定
- 选择系统文件夹作为只读共享目录
- Windows 浏览、下载和 ZIP 打包下载
- 选择多个文件或整个文件夹发送
- Windows 发来的文件可在手机上确认或拒绝
- SHA-256 完整性校验，校验失败自动删除半成品
- Android SAF 持久目录授权，不需要“所有文件访问”权限
- 前台服务保持局域网接收能力
- croc 官方网页公网传输入口

## 首次使用

1. 安装 APK 并打开 LinkDrop。
2. 进入“共享”，先选择接收文件夹。Android 11+ 的系统选择器通常不允许直接选择 Download 根目录，请先在 Download 下新建 `LinkDrop` 目录并选择它。
3. 如需让 Windows 浏览手机文件，再添加一个只读共享文件夹。
4. 在 Windows LinkDrop 的“发送与浏览”中输入手机显示的 `IP:端口` 和 6 位配对码。

如果自动发现不可用，可手动输入地址。部分路由器启用了 AP 隔离或 VPN，会阻止局域网访问。

## 文件发送

在“发送”页先连接 Windows，再选择文件或文件夹。发送前会计算 SHA-256，Windows 接收端确认后才会上传。单个文件夹的名称会成为接收批次目录，内部文件保持原有层级。

## 公网传输

Android 10+ 不允许应用可靠地直接执行下载到私有目录的二进制文件，因此 APK 不内置 croc 可执行核心。公网页会调用 croc 官方网页：

- 发送：打开 croc 网页后选择文件。
- 接收：输入对方口令，网页会建立 PAKE 端到端加密通道。

文件不会经过 LinkDrop 自有服务器。croc 网页属于第三方开源服务，使用时需能访问 `getcroc.com`。

## 安全说明

- 不申请全盘存储权限，只访问用户通过系统文件选择器授权的目录。
- 配对码和文件都经 HTTPS 传输。
- 首次连接会记录对方证书指纹，设备身份变化后会阻止连接。
- 接收端需要逐批确认。
- 分享目录为只读；接收目录可写。
- 当前 APK 使用本机 Android 调试密钥签名，适合直接安装测试和内部使用，不适合上架应用商店。若准备长期发布，应改用独立的正式签名密钥并妥善保管。

## 构建

项目需要 JDK 17、Android SDK 35 和 Gradle 9.5.1。

```powershell
.\build.ps1
```

输出：

```text
dist\LinkDrop-Android-1.0.0.apk
```

## 已验证

- Android 15 模拟器启动与页面渲染
- Android 连接 Windows 版并完成 TLS 配对
- Android 发送文件到 Windows
- Windows 发送文件到 Android，包括确认弹窗
- Android 共享目录列表、单文件下载和 ZIP 下载
- 双向文件 SHA-256 一致

