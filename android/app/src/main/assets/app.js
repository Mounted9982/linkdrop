(function () {
  "use strict";

  const state = {
    data: {},
    connected: false,
    remoteShares: [],
    remoteEntries: [],
    currentShareId: "",
    currentPath: "",
    pendingIncoming: null,
    transferActive: false
  };

  const $ = (id) => document.getElementById(id);
  const hasNative = () => typeof window.AndroidNative !== "undefined";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  function formatBytes(value) {
    let number = Number(value || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (number >= 1024 && index < units.length - 1) {
      number /= 1024;
      index++;
    }
    return `${index === 0 ? Math.round(number) : number.toFixed(1)} ${units[index]}`;
  }

  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove("show"), 2400);
  }

  function navigate(name) {
    document.querySelectorAll(".page").forEach((page) => {
      page.classList.toggle("active", page.dataset.page === name);
    });
    document.querySelectorAll(".bottom-nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.target === name);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function copyText(text) {
    if (hasNative()) {
      AndroidNative.copyText(String(text || ""));
      toast("已复制");
    }
  }

  function applyState(data) {
    if (!data || typeof data !== "object") return;
    state.data = data;
    $("deviceName").textContent = data.deviceName || "Android 设备";
    $("pairCode").textContent = data.pairCode || "------";
    $("fingerprint").textContent = data.fingerprint || "不可用";
    $("nameSetting").value = data.deviceName || "";
    $("codeSetting").value = data.pairCode || "";
    const addresses = data.addresses || [];
    $("addressButton").textContent = addresses[0] || `端口 ${data.port || 48610}`;
    $("addressButton").onclick = () => copyText(addresses[0] || "");
    $("receiveTitle").textContent = data.receiveUri ? "已设置接收目录" : "尚未选择";
    $("receiveSubtitle").textContent = data.receiveUri
      ? "Windows 发来的文件会保存到该目录"
      : "Windows 向你发送文件前必须设置";
    renderPeers(data.peers || []);
    renderShares(data.shares || []);
    renderInbox(data.recentIncoming || []);
    if (data.remoteInfo) {
      state.connected = true;
      $("connectStatus").textContent = `已连接 · TLS · ${data.remoteInfo.device_name || "Windows"}`;
      $("connectStatus").classList.add("ok");
      state.remoteShares = data.remoteShares || [];
      renderRemoteShares();
    }
  }

  function renderPeers(peers) {
    state.data.peers = peers;
    const box = $("peers");
    box.innerHTML = "";
    $("peersEmpty").classList.toggle("hidden", peers.length > 0);
    peers.forEach((peer) => {
      const row = document.createElement("div");
      row.className = "peer";
      row.innerHTML = `
        <div class="avatar">${escapeHtml(String(peer.name || "?").slice(0, 1).toUpperCase())}</div>
        <div class="grow"><strong>${escapeHtml(peer.name)}</strong><small>${escapeHtml(peer.address)}</small></div>
        <button class="small-button">连接</button>`;
      row.querySelector("button").onclick = () => {
        navigate("send");
        $("addressInput").value = peer.address || "";
        $("codeInput").value = "";
        $("codeInput").focus();
        toast("请输入对方的配对码");
      };
      box.appendChild(row);
    });
  }

  function renderShares(shares) {
    $("shareCount").textContent = `${shares.length} 个`;
    const box = $("shares");
    box.innerHTML = "";
    if (!shares.length) {
      box.innerHTML = `<div class="empty"><div class="empty-icon">▤</div><strong>还没有共享文件夹</strong><span>选择手机中的一个目录，Windows 就能浏览其中的文件。</span></div>`;
      return;
    }
    shares.forEach((share) => {
      const row = document.createElement("div");
      row.className = "share-row";
      row.innerHTML = `
        <div class="card-icon blue">▤</div>
        <div class="grow"><strong>${escapeHtml(share.name)}</strong><small>只读共享 · SAF 授权</small></div>
        <button class="small-button danger">停止</button>`;
      row.querySelector("button").onclick = () => {
        if (confirm(`停止共享“${share.name}”？手机文件不会被删除。`)) {
          AndroidNative.removeShare(share.id);
        }
      };
      box.appendChild(row);
    });
  }

  function renderInbox(items) {
    state.data.recentIncoming = items;
    const box = $("inbox");
    box.innerHTML = "";
    if (!items.length) {
      box.innerHTML = `<div class="empty"><div class="empty-icon">↓</div><strong>本次运行尚未收到文件</strong><span>完成接收后会显示在这里。</span></div>`;
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "inbox-row";
      row.innerHTML = `
        <div class="card-icon green">↓</div>
        <div class="grow"><strong>${escapeHtml(item.name || "文件")}</strong><small>${escapeHtml(item.sender || "未知设备")} · ${formatBytes(item.size)}</small></div>
        ${item.path ? `<button class="small-button light">打开</button>` : ""}`;
      const button = row.querySelector("button");
      if (button) button.onclick = () => AndroidNative.openUri(item.path);
      box.appendChild(row);
    });
  }

  function renderRemoteShares() {
    const select = $("remoteShareSelect");
    const shares = state.remoteShares || [];
    select.innerHTML = "";
    shares.forEach((share) => {
      const option = document.createElement("option");
      option.value = share.id;
      option.textContent = share.name;
      select.appendChild(option);
    });
    if (shares.length) {
      state.currentShareId = shares[0].id;
      state.currentPath = "";
      loadRemote(state.currentShareId, "");
    }
    $("remoteBrowser").classList.toggle("hidden", shares.length === 0);
  }

  function changeRemoteShare() {
    state.currentShareId = $("remoteShareSelect").value;
    state.currentPath = "";
    loadRemote(state.currentShareId, "");
  }

  function loadRemote(shareId, path) {
    state.currentShareId = shareId;
    state.currentPath = path || "";
    $("remotePath").textContent = "/" + state.currentPath;
    AndroidNative.listRemote(shareId, state.currentPath);
  }

  function goUp() {
    const parts = state.currentPath.split("/").filter(Boolean);
    parts.pop();
    loadRemote(state.currentShareId, parts.join("/"));
  }

  function renderRemoteEntries(entries) {
    state.remoteEntries = entries || [];
    const box = $("remoteFiles");
    box.innerHTML = "";
    if (!state.remoteEntries.length) {
      box.innerHTML = `<div class="empty"><div class="empty-icon">▤</div><strong>文件夹为空</strong></div>`;
      return;
    }
    state.remoteEntries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "file-row";
      row.innerHTML = `
        <div class="file-icon">${entry.is_dir ? "▸" : "▤"}</div>
        <div class="grow"><strong>${escapeHtml(entry.name)}</strong><small>${entry.is_dir ? "文件夹" : formatBytes(entry.size)}</small></div>
        ${entry.is_dir ? "" : `<button class="download">下载</button>`}`;
      if (entry.is_dir) {
        row.onclick = () => loadRemote(state.currentShareId, entry.path);
      } else {
        row.querySelector("button").onclick = () => {
          AndroidNative.downloadRemote(state.currentShareId, entry.path, entry.name);
        };
      }
      box.appendChild(row);
    });
  }

  function showIncoming(payload) {
    state.pendingIncoming = payload;
    $("incomingSummary").textContent = `${payload.sender || "另一台设备"} 请求发送 ${payload.fileCount || 0} 个文件，共 ${formatBytes(payload.totalSize)}。`;
    const box = $("incomingFiles");
    box.innerHTML = "";
    (payload.files || []).slice(0, 8).forEach((file) => {
      const row = document.createElement("div");
      row.className = "modal-file";
      row.innerHTML = `<span>${escapeHtml(file.name)}</span><span>${formatBytes(file.size)}</span>`;
      box.appendChild(row);
    });
    if ((payload.files || []).length > 8) {
      const row = document.createElement("div");
      row.className = "modal-file";
      row.textContent = `…另有 ${payload.files.length - 8} 个文件`;
      box.appendChild(row);
    }
    $("incomingModal").classList.remove("hidden");
  }

  function answerIncoming(accepted) {
    if (!state.pendingIncoming) return;
    AndroidNative.respondIncoming(state.pendingIncoming.sessionId, accepted);
    state.pendingIncoming = null;
    $("incomingModal").classList.add("hidden");
    toast(accepted ? "已确认接收" : "已拒绝");
  }

  function handleEvent(raw) {
    let event;
    try {
      event = typeof raw === "string" ? JSON.parse(raw) : raw;
    } catch (_) {
      return;
    }
    const payload = event.payload || {};
    switch (event.type) {
      case "state":
        applyState(payload);
        break;
      case "peers":
        renderPeers(payload.peers || []);
        break;
      case "incoming_request":
        showIncoming(payload);
        break;
      case "incoming":
      case "incoming_complete":
        AndroidNative && refreshState();
        if (event.type === "incoming_complete") toast("文件接收完成");
        break;
      case "incoming_answered":
        break;
      case "connected":
        state.connected = true;
        $("connectStatus").textContent = `已连接 · TLS · ${payload.device_name || "Windows"}`;
        $("connectStatus").classList.add("ok");
        toast("连接成功");
        refreshState();
        break;
      case "remote_entries":
        state.currentPath = payload.path || "";
        $("remotePath").textContent = "/" + state.currentPath;
        renderRemoteEntries(payload.entries || []);
        break;
      case "selection":
        const count = payload.count || 0;
        $("selectionTitle").textContent = count ? `已选择 ${count} 个文件` : "尚未选择";
        $("selectionSubtitle").textContent = count ? `批次：${payload.batch || "本次传输"}` : "选择文件或整个文件夹发给 Windows";
        break;
      case "transfer_start":
        state.transferActive = true;
        $("transferCard").classList.remove("hidden");
        $("transferTitle").textContent = payload.message || "传输中";
        $("transferPercent").textContent = "0%";
        $("progressBar").style.width = "0%";
        $("transferDetail").textContent = "准备中…";
        break;
      case "transfer_progress":
        updateProgress(payload.done, payload.total);
        break;
      case "transfer_done":
        state.transferActive = false;
        $("transferTitle").textContent = payload.message || "完成";
        $("transferPercent").textContent = "100%";
        $("progressBar").style.width = "100%";
        toast(payload.message || "传输完成");
        break;
      case "transfer_error":
        state.transferActive = false;
        $("transferTitle").textContent = "传输失败";
        $("progressBar").style.width = "0%";
        toast(payload.message || "传输失败");
        break;
      case "error":
      case "server_error":
        $("connectStatus").textContent = "连接失败";
        $("connectStatus").classList.remove("ok");
        state.connected = false;
        toast(payload.message || "操作失败");
        break;
    }
  }

  function updateProgress(done, total) {
    const safeTotal = Number(total || 0);
    const safeDone = Number(done || 0);
    const percent = safeTotal > 0 ? Math.min(100, Math.round(safeDone / safeTotal * 100)) : 0;
    $("transferCard").classList.remove("hidden");
    $("transferPercent").textContent = `${percent}%`;
    $("progressBar").style.width = `${percent}%`;
    $("transferDetail").textContent = `${formatBytes(safeDone)} / ${formatBytes(safeTotal)}`;
  }

  function refreshState() {
    if (!hasNative()) return;
    try {
      applyState(JSON.parse(AndroidNative.getState()));
    } catch (_) {}
  }

  function scanPeers() { hasNative() && AndroidNative.scanPeers(); }
  function addShare() { hasNative() && AndroidNative.addSharedFolder(); }
  function chooseReceiveFolder() { hasNative() && AndroidNative.chooseReceiveFolder(); }
  function chooseFiles() { hasNative() && AndroidNative.chooseSendFiles(); }
  function chooseFolder() { hasNative() && AndroidNative.chooseSendFolder(); }
  function sendSelected() {
    if (!state.connected) {
      toast(state.remoteInfoLoaded ? "请先连接 Windows" : "请先连接 Windows");
      return;
    }
    if (!state.data || !state.data.connected) {
      toast("请先连接 Windows 设备");
      return;
    }
    AndroidNative.sendSelected();
  }
  function connectRemote() {
    const address = $("addressInput").value.trim();
    const code = $("codeInput").value.trim();
    if (!address || !/^\d{6}$/.test(code)) {
      toast("请输入地址和 6 位配对码");
      return;
    }
    $("connectStatus").textContent = "正在连接…";
    $("connectStatus").classList.remove("ok");
    AndroidNative.connect(address, code);
  }
  function saveIdentity() {
    const name = $("nameSetting").value.trim();
    const code = $("codeSetting").value.trim();
    if (!name || !/^\d{6}$/.test(code)) {
      toast("设备名称不能为空，配对码必须为 6 位数字");
      return;
    }
    AndroidNative.updateIdentity(name, code);
    toast("设置已保存");
  }
  function randomCode() {
    $("codeSetting").value = String(Math.floor(Math.random() * 1000000)).padStart(6, "0");
  }
  function copyCode() { copyText(state.data.pairCode || ""); }
  function openCroc(code) {
    if (hasNative()) AndroidNative.openCroc(code || "");
  }

  window.LinkDropNative = { dispatch: handleEvent };
  Object.assign(window, {
    scanPeers,
    addShare,
    chooseReceiveFolder,
    connectRemote,
    changeRemoteShare,
    goUp,
    chooseFiles,
    chooseFolder,
    sendSelected,
    copyCode,
    openCroc,
    answerIncoming,
    saveIdentity,
    randomCode
  });
  window.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".bottom-nav button").forEach((button) => {
      button.onclick = () => navigate(button.dataset.target);
    });
    $("refreshButton").onclick = () => {
      scanPeers();
      refreshState();
      toast("正在刷新");
    };
    if (!hasNative()) {
      toast("请在 Android 应用中打开");
    } else {
      refreshState();
    }
  });
})();
