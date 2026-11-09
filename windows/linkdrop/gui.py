from __future__ import annotations

import os
import queue
import secrets
import subprocess
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__
from .config import AppConfig, ConfigStore, Share, app_data_dir
from .croc import CrocSession, generate_code
from .network import (
    DiscoveryService,
    IncomingEvent,
    IncomingSession,
    LinkDropError,
    Peer,
    RemoteClient,
    TransferCancelled,
    TransferHTTPServer,
    collect_files,
    format_bytes,
    local_ipv4_addresses,
    parse_address,
)


BG = "#F4F6FA"
SURFACE = "#FFFFFF"
SIDEBAR = "#111827"
SIDEBAR_HOVER = "#1F2937"
TEXT = "#111827"
MUTED = "#6B7280"
BLUE = "#2563EB"
BLUE_DARK = "#1D4ED8"
GREEN = "#059669"
GREEN_LIGHT = "#ECFDF5"
RED = "#DC2626"
RED_LIGHT = "#FEF2F2"
BORDER = "#E5E7EB"
FONT = "Microsoft YaHei UI"


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int = 128,
        height: int = 38,
        background: str = BLUE,
        foreground: str = "#FFFFFF",
        hover: str = BLUE_DARK,
        radius: int = 9,
        font: tuple[Any, ...] = (FONT, 10, "bold"),
    ) -> None:
        super().__init__(master, width=width, height=height, bg=master.cget("bg"), highlightthickness=0)
        self._command = command
        self._bg = background
        self._hover = hover
        self._radius = radius
        self._width = width
        self._height = height
        self._enabled = True
        self._shape = self.create_round_rect(1, 1, width - 1, height - 1, radius, fill=background, outline="")
        self._label = self.create_text(width / 2, height / 2, text=text, fill=foreground, font=font)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.configure(cursor="hand2")

    def create_round_rect(self, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs: Any) -> int:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _on_enter(self, _event: tk.Event) -> None:
        if self._enabled:
            self.itemconfigure(self._shape, fill=self._hover)

    def _on_leave(self, _event: tk.Event) -> None:
        if self._enabled:
            self.itemconfigure(self._shape, fill=self._bg)

    def _on_click(self, _event: tk.Event) -> None:
        if self._enabled:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.itemconfigure(self._shape, fill=self._bg if enabled else "#CBD5E1")


class SidebarButton(tk.Frame):
    def __init__(self, master: tk.Misc, text: str, icon: str, command: Callable[[], None]) -> None:
        super().__init__(master, bg=SIDEBAR, cursor="hand2")
        self.command = command
        self.active = False
        self.icon = icon
        self.label = tk.Label(
            self,
            text=f"  {icon}   {text}",
            anchor="w",
            bg=SIDEBAR,
            fg="#CBD5E1",
            font=(FONT, 10, "bold"),
            padx=14,
            pady=11,
        )
        self.label.pack(fill="x")
        for widget in (self, self.label):
            widget.bind("<Button-1>", lambda _event: self.command())
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _enter(self, _event: tk.Event) -> None:
        if not self.active:
            self.configure(bg=SIDEBAR_HOVER)
            self.label.configure(bg=SIDEBAR_HOVER)

    def _leave(self, _event: tk.Event) -> None:
        color = "#1D4ED8" if self.active else SIDEBAR
        self.configure(bg=color)
        self.label.configure(bg=color)

    def set_active(self, active: bool) -> None:
        self.active = active
        color = "#1D4ED8" if active else SIDEBAR
        foreground = "#FFFFFF" if active else "#CBD5E1"
        self.configure(bg=color)
        self.label.configure(bg=color, fg=foreground)


class Card(tk.Frame):
    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        super().__init__(master, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1, **kwargs)


class LinkDropApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.store = ConfigStore()
        self.config_snapshot = self.store.snapshot()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.server: TransferHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.discovery: DiscoveryService | None = None
        self.peers: dict[str, Peer] = {}
        self.remote: RemoteClient | None = None
        self.remote_share_id: str = ""
        self.remote_path: str = ""
        self.remote_share_map: dict[str, str] = {}
        self.current_transfer_cancel: threading.Event | None = None
        self.transfer_running = False
        self.croc_session: CrocSession | None = None
        self._last_speed_time = 0.0
        self._last_speed_bytes = 0

        self.title("LinkDrop - Windows 文件直传")
        self.geometry("1120x760")
        self.minsize(980, 650)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_style()
        self._build_shell()
        self._build_pages()
        self._show_page("home")
        self._wire_events()
        self.after(100, self._drain_events)
        self.after(150, self._start_services)
        self.after(500, lambda: self._scan_peers(automatic=True))
        self.after(15000, self._auto_scan)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=SURFACE, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED)
        style.configure(
            "Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT,
            rowheight=31,
            borderwidth=0,
            font=(FONT, 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#F8FAFC",
            foreground=MUTED,
            relief="flat",
            font=(FONT, 9, "bold"),
            padding=(8, 8),
        )
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", BLUE_DARK)])
        style.configure(
            "TEntry",
            padding=8,
            fieldbackground="#FFFFFF",
            borderwidth=1,
            relief="flat",
        )
        style.configure(
            "TCombobox",
            padding=7,
            fieldbackground="#FFFFFF",
            background="#FFFFFF",
            arrowcolor=MUTED,
        )
        style.configure("Horizontal.TProgressbar", troughcolor="#E2E8F0", background=BLUE, borderwidth=0)

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self, bg=SIDEBAR, width=232)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=22, pady=(26, 30))
        logo = tk.Canvas(brand, width=42, height=42, bg=SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(2, 2, 40, 40, fill=BLUE, outline="")
        logo.create_text(21, 20, text="⇄", fill="#FFFFFF", font=(FONT, 21, "bold"))
        brand_text = tk.Frame(brand, bg=SIDEBAR)
        brand_text.pack(side="left", padx=(11, 0))
        tk.Label(brand_text, text="LinkDrop", bg=SIDEBAR, fg="#FFFFFF", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="邻距直传", bg=SIDEBAR, fg="#94A3B8", font=(FONT, 8)).pack(anchor="w")

        self.sidebar_buttons: dict[str, SidebarButton] = {}
        definitions = [
            ("home", "概览", "⌂"),
            ("shares", "共享文件夹", "▤"),
            ("send", "发送与浏览", "↗"),
            ("remote", "公网直连", "◎"),
            ("inbox", "接收记录", "↓"),
            ("settings", "设置", "⚙"),
        ]
        for key, text, icon in definitions:
            button = SidebarButton(self.sidebar, text, icon, lambda name=key: self._show_page(name))
            button.pack(fill="x", padx=12, pady=2)
            self.sidebar_buttons[key] = button

        footer = tk.Frame(self.sidebar, bg=SIDEBAR)
        footer.pack(side="bottom", fill="x", padx=20, pady=20)
        self.sidebar_status = tk.Label(
            footer,
            text="●  正在启动服务",
            bg=SIDEBAR,
            fg="#FBBF24",
            font=(FONT, 9),
            anchor="w",
        )
        self.sidebar_status.pack(fill="x")
        tk.Label(
            footer,
            text=f"v{__version__}  ·  仅共享你选择的目录",
            bg=SIDEBAR,
            fg="#64748B",
            font=(FONT, 8),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

        self.main = tk.Frame(self, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}

    def _build_pages(self) -> None:
        self.pages["home"] = self._build_home_page()
        self.pages["shares"] = self._build_shares_page()
        self.pages["send"] = self._build_send_page()
        self.pages["remote"] = self._build_remote_page()
        self.pages["inbox"] = self._build_inbox_page()
        self.pages["settings"] = self._build_settings_page()

    def _page_header(self, parent: tk.Misc, title: str, subtitle: str) -> tk.Frame:
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", padx=30, pady=(25, 18))
        tk.Label(header, text=title, bg=BG, fg=TEXT, font=(FONT, 21, "bold"), anchor="w").pack(anchor="w")
        tk.Label(header, text=subtitle, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w").pack(anchor="w", pady=(5, 0))
        return header

    def _build_home_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._page_header(page, "概览", "把文件直接送到另一台设备，不经过第三方云盘。")
        content = tk.Frame(page, bg=BG)
        content.pack(fill="both", expand=True, padx=30, pady=(0, 26))

        hero = Card(content)
        hero.pack(fill="x")
        hero_left = tk.Frame(hero, bg=SURFACE)
        hero_left.pack(side="left", fill="both", expand=True, padx=24, pady=22)
        tk.Label(hero_left, text="这台设备", bg=SURFACE, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        self.home_device_name = tk.Label(
            hero_left,
            text=self.config_snapshot.device_name,
            bg=SURFACE,
            fg=TEXT,
            font=(FONT, 18, "bold"),
        )
        self.home_device_name.pack(anchor="w", pady=(3, 10))
        self.home_address = tk.Label(
            hero_left,
            text="正在获取地址…",
            bg=SURFACE,
            fg=BLUE,
            cursor="hand2",
            font=("Consolas", 11, "bold"),
        )
        self.home_address.pack(anchor="w")
        self.home_address.bind("<Button-1>", lambda _event: self._copy(self.home_address.cget("text")))
        tk.Label(
            hero_left,
            text="局域网设备可自动发现；传输使用 TLS 加密，远程也可使用一次性口令。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
        ).pack(anchor="w", pady=(8, 0))

        code_box = tk.Frame(hero, bg="#EFF6FF", width=210)
        code_box.pack(side="right", fill="y", padx=22, pady=20)
        code_box.pack_propagate(False)
        tk.Label(code_box, text="6 位配对码", bg="#EFF6FF", fg=BLUE_DARK, font=(FONT, 9)).pack(pady=(15, 3))
        self.home_code = tk.Label(
            code_box,
            text=self.config_snapshot.pair_code,
            bg="#EFF6FF",
            fg=BLUE_DARK,
            font=("Consolas", 23, "bold"),
        )
        self.home_code.pack()
        tk.Label(code_box, text="点击配对码可复制", bg="#EFF6FF", fg="#64748B", font=(FONT, 8)).pack(pady=(3, 0))
        self.home_code.bind("<Button-1>", lambda _event: self._copy(self.config_snapshot.pair_code))

        lower = tk.Frame(content, bg=BG)
        lower.pack(fill="both", expand=True, pady=(16, 0))
        lower.columnconfigure(0, weight=3)
        lower.columnconfigure(1, weight=2)
        lower.rowconfigure(0, weight=1)

        peers_card = Card(lower)
        peers_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        top = tk.Frame(peers_card, bg=SURFACE)
        top.pack(fill="x", padx=20, pady=(18, 10))
        tk.Label(top, text="发现附近的设备", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        RoundedButton(top, "重新扫描", self._scan_peers, width=92, height=32, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="right")
        self.peer_frame = tk.Frame(peers_card, bg=SURFACE)
        self.peer_frame.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self.peer_empty = tk.Label(
            self.peer_frame,
            text="正在局域网内寻找运行 LinkDrop 的设备…\n找不到时仍可通过地址连接。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 9),
            justify="left",
        )
        self.peer_empty.pack(anchor="w", pady=15)

        quick_card = Card(lower)
        quick_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(quick_card, text="快速操作", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(18, 12))
        self._quick_action(quick_card, "＋  共享一个文件夹", "选择后，对方可以浏览和下载。", lambda: self._add_share(open_page=True))
        self._quick_action(quick_card, "↗  直接发送文件", "选择文件或整个文件夹发给对方。", lambda: self._show_page("send"))
        self._quick_action(quick_card, "↓  打开接收目录", self._inbox_path_text(), self._open_inbox)
        return page

    def _quick_action(self, parent: tk.Misc, title: str, subtitle: str, command: Callable[[], None]) -> None:
        row = tk.Frame(parent, bg=SURFACE, cursor="hand2")
        row.pack(fill="x", padx=18, pady=4)
        dot = tk.Label(row, text="›", bg="#F8FAFC", fg=BLUE, font=(FONT, 18, "bold"), width=3, pady=7)
        dot.pack(side="left")
        labels = tk.Frame(row, bg=SURFACE)
        labels.pack(side="left", fill="x", expand=True, padx=8)
        first = tk.Label(labels, text=title, bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold"), anchor="w")
        first.pack(fill="x")
        self._inbox_path_label = tk.Label(labels, text=subtitle, bg=SURFACE, fg=MUTED, font=(FONT, 8), anchor="w")
        self._inbox_path_label.pack(fill="x", pady=(2, 0))
        for widget in (row, dot, labels, first, self._inbox_path_label):
            widget.bind("<Button-1>", lambda _event: command())

    def _inbox_path_text(self) -> str:
        return str(Path(self.config_snapshot.inbox_path))

    def _build_shares_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        header = self._page_header(page, "共享文件夹", "只开放你明确添加的目录；对方需要知道配对码。")
        RoundedButton(header, "＋  添加文件夹", self._add_share, width=132, height=36).pack(side="right", pady=5)
        card = Card(page)
        card.pack(fill="both", expand=True, padx=30, pady=(0, 26))
        info = tk.Frame(card, bg=SURFACE)
        info.pack(fill="x", padx=20, pady=(17, 8))
        tk.Label(info, text="已共享", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        self.share_count_label = tk.Label(info, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.share_count_label.pack(side="right")
        columns = ("name", "path", "status")
        self.share_tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="browse")
        for column, label, width in (("name", "名称", 150), ("path", "本机路径", 470), ("status", "状态", 90)):
            self.share_tree.heading(column, text=label)
            self.share_tree.column(column, width=width, anchor="w", stretch=column == "path")
        self.share_tree.pack(fill="both", expand=True, padx=20, pady=(4, 10))
        actions = tk.Frame(card, bg=SURFACE)
        actions.pack(fill="x", padx=20, pady=(0, 16))
        RoundedButton(actions, "打开文件夹", self._open_selected_share, width=110, height=34, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="left")
        RoundedButton(actions, "停止共享", self._remove_selected_share, width=100, height=34, background=RED_LIGHT, foreground=RED, hover="#FEE2E2").pack(side="left", padx=8)
        self._refresh_shares()
        return page

    def _build_send_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._page_header(page, "发送与浏览", "输入对方地址和配对码；局域网设备也可以从概览页直接选择。")
        content = tk.Frame(page, bg=BG)
        content.pack(fill="both", expand=True, padx=30, pady=(0, 26))

        connection = Card(content)
        connection.pack(fill="x")
        conn_inner = tk.Frame(connection, bg=SURFACE)
        conn_inner.pack(fill="x", padx=18, pady=14)
        tk.Label(conn_inner, text="目标地址", bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left")
        self.address_var = tk.StringVar()
        self.address_entry = ttk.Entry(conn_inner, textvariable=self.address_var, width=23)
        self.address_entry.pack(side="left", padx=(8, 14), ipady=2)
        tk.Label(conn_inner, text="配对码", bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold")).pack(side="left")
        self.remote_code_var = tk.StringVar()
        self.code_entry = ttk.Entry(conn_inner, textvariable=self.remote_code_var, width=12)
        self.code_entry.pack(side="left", padx=(8, 14), ipady=2)
        RoundedButton(conn_inner, "连接", self._connect_remote, width=82, height=34).pack(side="left")
        self.connection_status = tk.Label(conn_inner, text="未连接", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.connection_status.pack(side="right", padx=(12, 0))

        split = tk.Frame(content, bg=BG)
        split.pack(fill="both", expand=True, pady=(14, 0))
        split.columnconfigure(0, weight=3)
        split.columnconfigure(1, weight=2)
        split.rowconfigure(0, weight=1)
        browser = Card(split)
        browser.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        browser_top = tk.Frame(browser, bg=SURFACE)
        browser_top.pack(fill="x", padx=18, pady=(15, 8))
        tk.Label(browser_top, text="对方的共享文件", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        self.remote_share_combo = ttk.Combobox(browser_top, state="readonly", width=23)
        self.remote_share_combo.pack(side="right")
        self.remote_share_combo.bind("<<ComboboxSelected>>", self._remote_share_changed)
        nav = tk.Frame(browser, bg=SURFACE)
        nav.pack(fill="x", padx=18, pady=(0, 6))
        self.back_button = RoundedButton(nav, "← 返回", self._remote_go_up, width=76, height=29, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0", font=(FONT, 8, "bold"))
        self.back_button.pack(side="left")
        self.remote_path_label = tk.Label(nav, text="/", bg=SURFACE, fg=MUTED, font=(FONT, 8))
        self.remote_path_label.pack(side="left", padx=10)
        RoundedButton(nav, "刷新", self._refresh_remote_folder, width=62, height=29, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE", font=(FONT, 8, "bold")).pack(side="right")
        columns = ("name", "type", "size")
        self.remote_tree = ttk.Treeview(browser, columns=columns, show="headings", selectmode="browse")
        for column, label, width in (("name", "名称", 330), ("type", "类型", 80), ("size", "大小", 100)):
            self.remote_tree.heading(column, text=label)
            self.remote_tree.column(column, width=width, anchor="w", stretch=column == "name")
        self.remote_tree.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.remote_tree.bind("<Double-1>", self._remote_double_click)
        browser_actions = tk.Frame(browser, bg=SURFACE)
        browser_actions.pack(fill="x", padx=18, pady=(0, 15))
        RoundedButton(browser_actions, "下载所选", self._download_selected, width=96, height=32, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="left")
        RoundedButton(browser_actions, "文件夹打包下载", self._download_zip_folder, width=130, height=32, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0").pack(side="left", padx=8)

        send_panel = Card(split)
        send_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(send_panel, text="直接发送", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(17, 5))
        tk.Label(
            send_panel,
            text="文件会直接写入对方接收目录，文件夹层级会保留。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            wraplength=300,
            justify="left",
        ).pack(anchor="w", padx=20)
        buttons = tk.Frame(send_panel, bg=SURFACE)
        buttons.pack(fill="x", padx=20, pady=(14, 8))
        RoundedButton(buttons, "选择文件", self._send_choose_files, width=92, height=34, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="left")
        RoundedButton(buttons, "选择文件夹", self._send_choose_folder, width=100, height=34, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0").pack(side="left", padx=8)
        self.send_selection_label = tk.Label(send_panel, text="尚未选择", bg=SURFACE, fg=MUTED, font=(FONT, 8))
        self.send_selection_label.pack(anchor="w", padx=20)
        RoundedButton(send_panel, "开始发送", self._start_send_dialog, width=122, height=39).pack(anchor="w", padx=20, pady=(12, 14))

        tk.Label(send_panel, text="传输状态", bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold")).pack(anchor="w", padx=20, pady=(10, 6))
        self.transfer_name = tk.Label(send_panel, text="空闲", bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w")
        self.transfer_name.pack(fill="x", padx=20)
        self.progress = ttk.Progressbar(send_panel, maximum=100)
        self.progress.pack(fill="x", padx=20, pady=(7, 5))
        self.transfer_detail = tk.Label(send_panel, text="0 B / 0 B", bg=SURFACE, fg=MUTED, font=(FONT, 8), anchor="w")
        self.transfer_detail.pack(fill="x", padx=20)
        self.cancel_button = RoundedButton(
            send_panel,
            "取消传输",
            self._cancel_transfer,
            width=96,
            height=31,
            background=RED_LIGHT,
            foreground=RED,
            hover="#FEE2E2",
        )
        self.cancel_button.pack(anchor="w", padx=20, pady=(10, 16))
        self.cancel_button.set_enabled(False)
        return page

    def _build_remote_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._page_header(
            page,
            "公网直连",
            "无需公网 IP。使用一次性口令建立端到端加密通道，局域网外也能传输。",
        )
        content = tk.Frame(page, bg=BG)
        content.pack(fill="both", expand=True, padx=30, pady=(0, 26))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(1, weight=1)

        send_card = Card(content)
        send_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 7))
        tk.Label(send_card, text="生成发送口令", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(17, 4))
        tk.Label(
            send_card,
            text="文件经 PAKE 派生的密钥端到端加密；优先直连，必要时使用中继。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=20)
        remote_choose = tk.Frame(send_card, bg=SURFACE)
        remote_choose.pack(fill="x", padx=20, pady=(13, 7))
        RoundedButton(remote_choose, "选择文件", self._croc_choose_files, width=88, height=32, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="left")
        RoundedButton(remote_choose, "选择文件夹", self._croc_choose_folder, width=98, height=32, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0").pack(side="left", padx=7)
        self.croc_selection_label = tk.Label(send_card, text="尚未选择", bg=SURFACE, fg=MUTED, font=(FONT, 8))
        self.croc_selection_label.pack(anchor="w", padx=20)
        send_actions = tk.Frame(send_card, bg=SURFACE)
        send_actions.pack(fill="x", padx=20, pady=(12, 7))
        RoundedButton(send_actions, "生成口令并等待", self._start_croc_send, width=132, height=37).pack(side="left")
        RoundedButton(send_actions, "取消", self._cancel_croc, width=64, height=37, background=RED_LIGHT, foreground=RED, hover="#FEE2E2").pack(side="left", padx=7)
        tk.Label(send_card, text="发送口令", bg=SURFACE, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=20, pady=(7, 2))
        self.croc_code_label = tk.Label(
            send_card,
            text="尚未生成",
            bg="#F8FAFC",
            fg=BLUE_DARK,
            font=("Consolas", 13, "bold"),
            anchor="w",
            padx=12,
            pady=9,
            cursor="hand2",
        )
        self.croc_code_label.pack(fill="x", padx=20, pady=(0, 15))
        self.croc_code_label.bind("<Button-1>", lambda _event: self._copy_croc_code())

        receive_card = Card(content)
        receive_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 7))
        tk.Label(receive_card, text="凭口令接收", bg=SURFACE, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=20, pady=(17, 4))
        tk.Label(
            receive_card,
            text="输入发送方给出的口令。文件会保存到 LinkDrop 接收目录。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=20)
        tk.Label(receive_card, text="公网传输口令", bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold")).pack(anchor="w", padx=20, pady=(21, 5))
        self.croc_receive_code = tk.StringVar()
        ttk.Entry(receive_card, textvariable=self.croc_receive_code, font=("Consolas", 10)).pack(fill="x", padx=20)
        receive_actions = tk.Frame(receive_card, bg=SURFACE)
        receive_actions.pack(fill="x", padx=20, pady=(12, 7))
        RoundedButton(receive_actions, "开始接收", self._start_croc_receive, width=102, height=37).pack(side="left")
        RoundedButton(receive_actions, "取消", self._cancel_croc, width=64, height=37, background=RED_LIGHT, foreground=RED, hover="#FEE2E2").pack(side="left", padx=7)
        self.croc_state = tk.Label(receive_card, text="空闲", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.croc_state.pack(anchor="w", padx=20, pady=(8, 4))
        tk.Label(
            receive_card,
            text="安全提示：口令就是本次传输凭据，只发给接收方。每次传输请生成新口令。",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(5, 15))

        log_card = Card(content)
        log_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(7, 0))
        log_header = tk.Frame(log_card, bg=SURFACE)
        log_header.pack(fill="x", padx=18, pady=(12, 5))
        tk.Label(log_header, text="传输日志", bg=SURFACE, fg=TEXT, font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(
            log_header,
            text="仅显示 croc 的运行状态，不上传本地路径以外的内容",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
        ).pack(side="right")
        self.croc_log = tk.Text(
            log_card,
            height=7,
            bg="#0F172A",
            fg="#CBD5E1",
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 8),
            padx=12,
            pady=9,
            wrap="word",
            state="disabled",
        )
        self.croc_log.pack(fill="both", expand=True, padx=18, pady=(0, 14))
        return page

    def _build_inbox_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        header = self._page_header(page, "接收记录", "收到的文件保存在本机接收目录，不会上传到云端。")
        RoundedButton(header, "打开接收目录", self._open_inbox, width=126, height=36).pack(side="right", pady=5)
        card = Card(page)
        card.pack(fill="both", expand=True, padx=30, pady=(0, 26))
        self.inbox_path_label = tk.Label(card, text=str(self.config_snapshot.inbox_path), bg=SURFACE, fg=MUTED, font=("Consolas", 9), anchor="w")
        self.inbox_path_label.pack(fill="x", padx=20, pady=(16, 8))
        columns = ("time", "sender", "file", "size")
        self.inbox_tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="browse")
        for column, label, width in (("time", "时间", 135), ("sender", "发送方", 135), ("file", "文件", 500), ("size", "大小", 100)):
            self.inbox_tree.heading(column, text=label)
            self.inbox_tree.column(column, width=width, anchor="w", stretch=column == "file")
        self.inbox_tree.pack(fill="both", expand=True, padx=20, pady=(4, 10))
        self.inbox_tree.bind("<Double-1>", self._open_inbox_item)
        footer = tk.Frame(card, bg=SURFACE)
        footer.pack(fill="x", padx=20, pady=(0, 16))
        tk.Label(footer, text="双击记录可定位到文件", bg=SURFACE, fg=MUTED, font=(FONT, 8)).pack(side="left")
        RoundedButton(footer, "清空显示", self._clear_inbox_history, width=82, height=30, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0").pack(side="right")
        return page

    def _build_settings_page(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._page_header(page, "设置", "修改设备名称、端口、配对码和接收目录。")
        card = Card(page)
        card.pack(fill="both", expand=True, padx=30, pady=(0, 26))
        form = tk.Frame(card, bg=SURFACE)
        form.pack(fill="x", padx=28, pady=24)
        form.columnconfigure(1, weight=1)
        self.device_name_var = tk.StringVar(value=self.config_snapshot.device_name)
        self.port_var = tk.StringVar(value=str(self.config_snapshot.port))
        self.pair_code_var = tk.StringVar(value=self.config_snapshot.pair_code)
        self.inbox_var = tk.StringVar(value=self.config_snapshot.inbox_path)
        self.allow_incoming_var = tk.BooleanVar(value=self.config_snapshot.allow_incoming)
        self._form_label(form, 0, "设备名称")
        ttk.Entry(form, textvariable=self.device_name_var).grid(row=0, column=1, sticky="ew", pady=7)
        self._form_label(form, 1, "监听端口")
        port_row = tk.Frame(form, bg=SURFACE)
        port_row.grid(row=1, column=1, sticky="ew", pady=7)
        ttk.Entry(port_row, textvariable=self.port_var, width=12).pack(side="left")
        tk.Label(port_row, text="默认 48610，远程需要在路由器放行", bg=SURFACE, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=12)
        self._form_label(form, 2, "配对码")
        code_row = tk.Frame(form, bg=SURFACE)
        code_row.grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Entry(code_row, textvariable=self.pair_code_var, width=14).pack(side="left")
        RoundedButton(code_row, "随机生成", self._randomize_code, width=84, height=31, background="#EFF6FF", foreground=BLUE, hover="#DBEAFE").pack(side="left", padx=8)
        self._form_label(form, 3, "接收目录")
        inbox_row = tk.Frame(form, bg=SURFACE)
        inbox_row.grid(row=3, column=1, sticky="ew", pady=7)
        ttk.Entry(inbox_row, textvariable=self.inbox_var).pack(side="left", fill="x", expand=True)
        RoundedButton(inbox_row, "更改", self._choose_inbox, width=68, height=31, background="#F1F5F9", foreground=MUTED, hover="#E2E8F0").pack(side="left", padx=8)
        self._form_label(form, 4, "文件接收")
        ttk.Checkbutton(
            form,
            text="允许他人凭配对码向我发送文件",
            variable=self.allow_incoming_var,
        ).grid(row=4, column=1, sticky="w", pady=7)

        notice = tk.Frame(card, bg="#F8FAFC")
        notice.pack(fill="x", padx=28, pady=(0, 18))
        tk.Label(notice, text="网络提示", bg="#F8FAFC", fg=TEXT, font=(FONT, 9, "bold")).pack(anchor="w", padx=15, pady=(12, 3))
        tk.Label(
            notice,
            text=(
                "局域网：两台电脑连同一 Wi-Fi/路由器即可。首次启动若 Windows 防火墙询问，请允许专用网络。\n"
                "远程直连：填写公网 IP/端口或 Tailscale/ZeroTier 地址；严格 NAT 环境请使用“公网直连”。"
            ),
            bg="#F8FAFC",
            fg=MUTED,
            font=(FONT, 8),
            justify="left",
        ).pack(anchor="w", padx=15, pady=(0, 12))
        RoundedButton(card, "保存并应用", self._save_settings, width=126, height=39).pack(anchor="w", padx=28, pady=(0, 24))
        return page

    def _form_label(self, parent: tk.Misc, row: int, text: str) -> None:
        tk.Label(parent, text=text, bg=SURFACE, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 18),
            pady=7,
        )

    def _wire_events(self) -> None:
        self.bind("<Control-f>", lambda _event: self._show_page("send"))

    def _show_page(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for key, button in self.sidebar_buttons.items():
            button.set_active(key == name)
        if name == "send":
            self._refresh_peers_ui()
        if name == "home":
            self._refresh_peers_ui()

    def _start_services(self) -> None:
        self._start_server()
        self.discovery = DiscoveryService(
            self.store.config,
            self._on_peer_found,
            self.server.cert_fingerprint if self.server else "",
        )
        self.discovery.start()
        self._scan_peers(automatic=True)
        self._refresh_me()

    def _start_server(self) -> None:
        if self.server:
            return
        config = self.store.config
        try:
            server = TransferHTTPServer(("0.0.0.0", config.port), self.store, self.events)
        except OSError as exc:
            self.sidebar_status.configure(text="●  端口被占用", fg=RED)
            self.after(400, lambda: messagebox.showerror("无法启动", f"端口 {config.port} 无法监听：{exc}\n\n请在设置中更换端口。", parent=self))
            return
        self.server = server
        self.server_thread = threading.Thread(target=server.serve_forever, name="LinkDropHTTP", daemon=True)
        self.server_thread.start()
        self.sidebar_status.configure(text="●  服务运行中", fg="#34D399")

    def _stop_server(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except OSError:
                pass
            self.server = None

    def _refresh_me(self) -> None:
        config = self.store.snapshot()
        self.config_snapshot = config
        addresses = local_ipv4_addresses()
        address = f"{addresses[0]}:{config.port}" if addresses else f"127.0.0.1:{config.port}"
        self.home_device_name.configure(text=config.device_name)
        self.home_address.configure(text=address)
        self.home_code.configure(text=config.pair_code)
        self.address_var.set(address)
        self.remote_code_var.set(config.pair_code)

    def _on_peer_found(self, peer: Peer) -> None:
        self.events.put(("peer", peer))

    def _scan_peers(self, automatic: bool = False) -> None:
        if self.discovery:
            self.discovery.scan()
        if not automatic:
            self.peer_empty.configure(text="正在扫描局域网…")

    def _auto_scan(self) -> None:
        if self.winfo_exists():
            self._scan_peers(automatic=True)
            self.after(15000, self._auto_scan)

    def _refresh_peers_ui(self) -> None:
        if not hasattr(self, "peer_frame"):
            return
        for child in self.peer_frame.winfo_children():
            child.destroy()
        if not self.peers:
            tk.Label(
                self.peer_frame,
                text="暂未发现设备\n确认两台电脑在同一局域网，并允许 LinkDrop 通过防火墙。",
                bg=SURFACE,
                fg=MUTED,
                font=(FONT, 9),
                justify="left",
            ).pack(anchor="w", pady=16)
            return
        for peer in sorted(self.peers.values(), key=lambda item: item.name.casefold()):
            row = tk.Frame(self.peer_frame, bg="#F8FAFC", cursor="hand2")
            row.pack(fill="x", pady=4)
            avatar = tk.Label(row, text=peer.name[:1].upper(), bg="#DBEAFE", fg=BLUE_DARK, width=4, pady=8, font=(FONT, 11, "bold"))
            avatar.pack(side="left", padx=(8, 10), pady=8)
            labels = tk.Frame(row, bg="#F8FAFC")
            labels.pack(side="left", fill="x", expand=True)
            name = tk.Label(labels, text=peer.name, bg="#F8FAFC", fg=TEXT, font=(FONT, 9, "bold"), anchor="w")
            name.pack(fill="x", pady=(7, 0))
            address = tk.Label(labels, text=peer.address, bg="#F8FAFC", fg=MUTED, font=("Consolas", 8), anchor="w")
            address.pack(fill="x")
            button = RoundedButton(
                row,
                "连接",
                lambda selected=peer: self._connect_peer(selected),
                width=64,
                height=29,
                background="#DBEAFE",
                foreground=BLUE_DARK,
                hover="#BFDBFE",
                font=(FONT, 8, "bold"),
            )
            button.pack(side="right", padx=8)
            for widget in (row, avatar, labels, name, address):
                widget.bind("<Double-1>", lambda _event, selected=peer: self._connect_peer(selected))

    def _connect_peer(self, peer: Peer) -> None:
        self.address_var.set(peer.address)
        self.remote_code_var.set("")
        self._show_page("send")
        self.code_entry.focus_set()
        self.connection_status.configure(text=f"请输入 {peer.name} 的配对码", fg=MUTED)

    def _connect_remote(self) -> None:
        address = self.address_var.get()
        code = self.remote_code_var.get().strip()
        if len(code) != 6 or not code.isdigit():
            messagebox.showwarning("配对码", "配对码应为对方概览页显示的 6 位数字。", parent=self)
            return
        try:
            host, port = parse_address(address)
        except ValueError as exc:
            messagebox.showwarning("地址", str(exc), parent=self)
            return
        self.connection_status.configure(text="正在连接…", fg=MUTED)
        client = RemoteClient(host, port, code)

        def work() -> None:
            try:
                info = client.info()
                shares = client.shares()
                self.events.put(("connected", (client, info, shares)))
            except Exception as exc:
                self.events.put(("error", f"连接失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _connected(self, payload: tuple[RemoteClient, dict[str, Any], list[dict[str, Any]]]) -> None:
        client, info, shares = payload
        device_id = str(info.get("device_id", ""))
        if not self.store.trust_device(device_id, client.peer_fingerprint):
            self.remote = None
            self.connection_status.configure(text="设备身份发生变化，连接已拦截", fg=RED)
            messagebox.showerror(
                "设备身份变化",
                "对方的 TLS 证书与上次连接时不同。\n"
                "如果你没有更换设备或重装系统，请不要继续发送文件。",
                parent=self,
            )
            return
        self.remote = client
        short_fingerprint = client.peer_fingerprint.replace(":", "")[:12]
        self.connection_status.configure(
            text=f"已连接 · TLS · {info.get('device_name', '设备')} · {short_fingerprint}",
            fg=GREEN,
        )
        self.remote_share_map = {f"{item['name']}  ({item['id'][:5]})": item["id"] for item in shares if item.get("available")}
        values = list(self.remote_share_map)
        self.remote_share_combo.configure(values=values)
        if values:
            self.remote_share_combo.current(0)
            self._remote_share_changed()
        else:
            self.remote_share_combo.set("")
            self.remote_path = ""
            self.remote_path_label.configure(text="/")
            self._clear_tree(self.remote_tree)
            messagebox.showinfo("已连接", "连接成功，但对方还没有共享文件夹。", parent=self)

    def _remote_share_changed(self, _event: tk.Event | None = None) -> None:
        label = self.remote_share_combo.get()
        self.remote_share_id = self.remote_share_map.get(label, "")
        self.remote_path = ""
        self._refresh_remote_folder()

    def _refresh_remote_folder(self) -> None:
        if not self.remote or not self.remote_share_id:
            return
        client = self.remote
        share_id = self.remote_share_id
        path = self.remote_path

        def work() -> None:
            try:
                entries = client.list_folder(share_id, path)
                self.events.put(("remote_entries", (path, entries)))
            except Exception as exc:
                self.events.put(("error", f"读取共享目录失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _remote_share_for_entry(self, item_id: str) -> dict[str, Any] | None:
        try:
            values = self.remote_tree.item(item_id, "values")
        except tk.TclError:
            return None
        if not values:
            return None
        name = str(values[0])
        for entry in getattr(self, "_remote_entries", []):
            if entry.get("name") == name:
                return entry
        return None

    def _remote_go_up(self) -> None:
        if not self.remote_path:
            return
        parts = [part for part in self.remote_path.replace("\\", "/").split("/") if part]
        self.remote_path = "/".join(parts[:-1])
        self._refresh_remote_folder()

    def _remote_double_click(self, _event: tk.Event) -> None:
        selected = self.remote_tree.selection()
        if not selected:
            return
        entry = self._remote_share_for_entry(selected[0])
        if entry and entry.get("is_dir"):
            self.remote_path = str(entry.get("path", ""))
            self._refresh_remote_folder()

    def _download_selected(self) -> None:
        if not self.remote:
            messagebox.showinfo("下载", "请先连接对方设备。", parent=self)
            return
        selected = self.remote_tree.selection()
        if not selected:
            messagebox.showinfo("下载", "请先在列表中选择一个文件。", parent=self)
            return
        entry = self._remote_share_for_entry(selected[0])
        if not entry or entry.get("is_dir"):
            messagebox.showinfo("下载", "双击文件夹进入后，选择文件下载。", parent=self)
            return
        name = str(entry.get("name", "file"))
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="保存文件",
            initialfile=name,
            initialdir=str(Path.home() / "Downloads"),
        )
        if not destination:
            return
        if not self._begin_transfer(f"下载 {name}"):
            return

        def work() -> None:
            try:
                result = self.remote.download(
                    self.remote_share_id,
                    str(entry.get("path", "")),
                    Path(destination),
                    progress=self._transfer_progress,
                    cancel=self.current_transfer_cancel,
                )
                self.events.put(("transfer_done", f"下载完成：{result}"))
            except Exception as exc:
                self.events.put(("transfer_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _download_zip_folder(self) -> None:
        if not self.remote:
            messagebox.showinfo("下载", "请先连接对方设备。", parent=self)
            return
        selected = self.remote_tree.selection()
        folder = ""
        folder_name = ""
        if selected:
            entry = self._remote_share_for_entry(selected[0])
            if entry and entry.get("is_dir"):
                folder = str(entry.get("path", ""))
                folder_name = str(entry.get("name", "folder"))
        if not folder:
            folder = self.remote_path
            folder_name = Path(self.remote_path).name if self.remote_path else "共享文件夹"
        if not folder:
            messagebox.showinfo("打包下载", "请进入一个文件夹后再打包下载。", parent=self)
            return
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="保存压缩包",
            initialfile=f"{folder_name}.zip",
            defaultextension=".zip",
            initialdir=str(Path.home() / "Downloads"),
            filetypes=[("ZIP 压缩包", "*.zip")],
        )
        if not destination:
            return
        if not self._begin_transfer(f"打包下载 {folder_name}"):
            return

        def work() -> None:
            try:
                result = self.remote.download_zip(
                    self.remote_share_id,
                    folder,
                    Path(destination),
                    cancel=self.current_transfer_cancel,
                )
                self.events.put(("transfer_done", f"打包下载完成：{result}"))
            except Exception as exc:
                self.events.put(("transfer_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _add_share(self, open_page: bool = False) -> None:
        folder = filedialog.askdirectory(parent=self, title="选择要共享的文件夹")
        if not folder:
            return
        try:
            self.store.add_share(folder)
            self.config_snapshot = self.store.snapshot()
            self._refresh_shares()
            if open_page:
                self._show_page("shares")
        except Exception as exc:
            messagebox.showerror("添加共享", str(exc), parent=self)

    def _refresh_shares(self) -> None:
        if not hasattr(self, "share_tree"):
            return
        self._clear_tree(self.share_tree)
        shares = self.store.config.shares
        for share in shares:
            available = Path(share.path).exists()
            self.share_tree.insert(
                "",
                "end",
                iid=share.id,
                values=(share.name, share.path, "可用" if available else "路径失效"),
            )
        self.share_count_label.configure(text=f"{len(shares)} 个")

    def _selected_share(self) -> Share | None:
        selected = self.share_tree.selection()
        if not selected:
            return None
        return next((item for item in self.store.config.shares if item.id == selected[0]), None)

    def _open_selected_share(self) -> None:
        share = self._selected_share()
        if share:
            self._open_path(share.path)

    def _remove_selected_share(self) -> None:
        share = self._selected_share()
        if not share:
            return
        if messagebox.askyesno("停止共享", f"停止共享“{share.name}”？\n本机文件不会被删除。", parent=self):
            self.store.remove_share(share.id)
            self._refresh_shares()

    def _send_choose_files(self) -> None:
        files = filedialog.askopenfilenames(parent=self, title="选择要发送的文件")
        if files:
            self._selected_send_paths = list(files)
            self.send_selection_label.configure(text=f"已选择 {len(files)} 个文件")
            if hasattr(self, "croc_selection_label"):
                self.croc_selection_label.configure(text=f"已选择 {len(files)} 个文件")

    def _send_choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="选择要发送的文件夹")
        if folder:
            self._selected_send_paths = [folder]
            self.send_selection_label.configure(text=f"已选择文件夹：{Path(folder).name}")
            if hasattr(self, "croc_selection_label"):
                self.croc_selection_label.configure(text=f"已选择文件夹：{Path(folder).name}")

    def _croc_choose_files(self) -> None:
        self._send_choose_files()

    def _croc_choose_folder(self) -> None:
        self._send_choose_folder()

    def _start_croc_send(self) -> None:
        if not hasattr(self, "_selected_send_paths") or not self._selected_send_paths:
            messagebox.showinfo("公网发送", "请先选择文件或文件夹。", parent=self)
            return
        if self.transfer_running or (self.croc_session and self.croc_session.running):
            messagebox.showinfo("传输", "已有传输任务正在进行。", parent=self)
            return
        code = generate_code()
        self.croc_code_label.configure(text=code, fg=BLUE_DARK)
        self.croc_state.configure(text="等待接收方输入口令…", fg="#B45309")
        self._clear_croc_log()
        self._croc_log(f"发送口令：{code}")
        self._croc_log("正在连接加密传输网络，接收方输入口令后开始发送。")
        self._copy(code)
        try:
            self.croc_session = CrocSession(
                on_line=lambda line: self.events.put(("croc_line", line)),
                on_done=lambda return_code, cancelled: self.events.put(
                    ("croc_done", (return_code, cancelled))
                ),
            )
            self.croc_session.start_send(list(self._selected_send_paths), code)
        except Exception as exc:
            self.croc_state.configure(text="启动失败", fg=RED)
            self._croc_log(f"错误：{exc}")
            messagebox.showerror("公网发送", str(exc), parent=self)

    def _start_croc_receive(self) -> None:
        if self.transfer_running or (self.croc_session and self.croc_session.running):
            messagebox.showinfo("传输", "已有传输任务正在进行。", parent=self)
            return
        code = self.croc_receive_code.get().strip()
        if not code:
            messagebox.showinfo("公网接收", "请输入发送方给出的口令。", parent=self)
            return
        self.croc_state.configure(text="正在建立加密连接…", fg="#B45309")
        self._clear_croc_log()
        self._croc_log("正在连接加密传输网络…")
        try:
            self.croc_session = CrocSession(
                on_line=lambda line: self.events.put(("croc_line", line)),
                on_done=lambda return_code, cancelled: self.events.put(
                    ("croc_done", (return_code, cancelled))
                ),
            )
            self.croc_session.start_receive(code, self.store.ensure_inbox())
        except Exception as exc:
            self.croc_state.configure(text="启动失败", fg=RED)
            self._croc_log(f"错误：{exc}")
            messagebox.showerror("公网接收", str(exc), parent=self)

    def _cancel_croc(self) -> None:
        if self.croc_session and self.croc_session.running:
            self.croc_state.configure(text="正在取消…", fg=MUTED)
            self.croc_session.cancel()

    def _copy_croc_code(self) -> None:
        code = self.croc_code_label.cget("text")
        if code and code != "尚未生成":
            self._copy(code)
            self.croc_state.configure(text="口令已复制", fg=GREEN)

    def _clear_croc_log(self) -> None:
        self.croc_log.configure(state="normal")
        self.croc_log.delete("1.0", "end")
        self.croc_log.configure(state="disabled")

    def _croc_log(self, line: str) -> None:
        self.croc_log.configure(state="normal")
        self.croc_log.insert("end", f"{line}\n")
        self.croc_log.see("end")
        self.croc_log.configure(state="disabled")

    def _start_send_dialog(self) -> None:
        if not hasattr(self, "_selected_send_paths") or not self._selected_send_paths:
            messagebox.showinfo("发送", "请先选择文件或文件夹。", parent=self)
            return
        if not self.remote:
            messagebox.showinfo("发送", "请先连接对方设备，连接成功后即可发送。", parent=self)
            return
        try:
            items, batch = collect_files(self._selected_send_paths)
        except Exception as exc:
            messagebox.showerror("发送", str(exc), parent=self)
            return
        total = sum(item.stat().st_size for item, _relative in items)
        if total > 0 and not messagebox.askyesno(
            "确认发送",
            f"将向 {self.connection_status.cget('text').replace('已连接  ·  ', '')} 发送 {len(items)} 个文件，共 {format_bytes(total)}。\n\n继续？",
            parent=self,
        ):
            return
        if not self._begin_transfer(f"发送 {batch}", total):
            return
        sent_total = 0

        def file_progress(done: int, total_one: int) -> None:
            nonlocal sent_total
            self._transfer_progress(sent_total + done, total)

        def work() -> None:
            nonlocal sent_total
            try:
                cancel = self.current_transfer_cancel
                self.events.put(("transfer_name", "正在计算 SHA-256 校验值…"))
                prepared = self.remote.prepare_transfer(
                    items,
                    batch,
                    self.store.config.device_id,
                    self.store.config.device_name,
                    hash_progress=self._transfer_progress,
                    cancel=cancel,
                    waiting=lambda: self.events.put(
                        ("transfer_name", "校验完成，等待接收方确认…")
                    ),
                )
                session_id = str(prepared.get("session_id", ""))
                tokens = dict(prepared.get("tokens", {}))
                prepared_files = list(prepared.get("file_items", []))
                for (source, relative), file_meta in zip(items, prepared_files):
                    self.events.put(("transfer_name", f"{source.name}  ·  {relative}"))
                    file_id = str(file_meta.get("id", ""))
                    self.remote.upload_file(
                        source,
                        session_id,
                        file_id,
                        str(tokens.get(file_id, "")),
                        progress=file_progress,
                        cancel=cancel,
                    )
                    sent_total += source.stat().st_size
                    self._transfer_progress(sent_total, total)
                self.events.put(("transfer_done", f"发送完成：{len(items)} 个文件"))
            except Exception as exc:
                self.events.put(("transfer_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _begin_transfer(self, name: str, total: int = 0) -> bool:
        if self.croc_session and self.croc_session.running:
            messagebox.showinfo("传输", "公网传输正在进行，请先等待或取消。", parent=self)
            return False
        if self.transfer_running:
            messagebox.showinfo("传输", "已有传输任务正在进行。", parent=self)
            return False
        self.transfer_running = True
        self.current_transfer_cancel = threading.Event()
        self._last_speed_time = time.monotonic()
        self._last_speed_bytes = 0
        self.transfer_name.configure(text=name, fg=TEXT)
        self.transfer_detail.configure(text=f"0 B / {format_bytes(total)}")
        self.progress.configure(value=0)
        self.cancel_button.set_enabled(True)
        return True

    def _transfer_progress(self, done: int, total: int) -> None:
        self.events.put(("transfer_progress", (done, total)))

    def _update_transfer_progress(self, done: int, total: int) -> None:
        percent = (done / total * 100) if total else 0
        self.progress.configure(value=max(0, min(100, percent)))
        self.transfer_detail.configure(text=f"{format_bytes(done)} / {format_bytes(total)}")

    def _finish_transfer(self, message: str, success: bool) -> None:
        self.transfer_running = False
        self.current_transfer_cancel = None
        self.cancel_button.set_enabled(False)
        self.transfer_name.configure(text=message, fg=GREEN if success else RED)
        if success:
            self.progress.configure(value=100)
        else:
            self.progress.configure(value=0)

    def _cancel_transfer(self) -> None:
        if self.current_transfer_cancel:
            self.current_transfer_cancel.set()
            self.transfer_name.configure(text="正在取消…", fg=MUTED)

    def _choose_inbox(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="选择接收目录")
        if folder:
            self.inbox_var.set(folder)

    def _randomize_code(self) -> None:
        self.pair_code_var.set(f"{secrets.randbelow(1_000_000):06d}")

    def _save_settings(self) -> None:
        name = self.device_name_var.get().strip()
        code = self.pair_code_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("设置", "端口必须是数字。", parent=self)
            return
        if not name:
            messagebox.showerror("设置", "设备名称不能为空。", parent=self)
            return
        if len(code) != 6 or not code.isdigit():
            messagebox.showerror("设置", "配对码必须为 6 位数字。", parent=self)
            return
        if not 1024 <= port <= 65535:
            messagebox.showerror("设置", "端口应在 1024 到 65535 之间。", parent=self)
            return
        inbox = Path(self.inbox_var.get()).expanduser().resolve()
        try:
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("设置", f"无法使用接收目录：{exc}", parent=self)
            return
        old_port = self.store.config.port
        try:
            new_config = self.store.update(
                device_name=name,
                pair_code=code,
                port=port,
                inbox_path=str(inbox),
                allow_incoming=self.allow_incoming_var.get(),
            )
        except OSError as exc:
            messagebox.showerror("设置", f"保存配置失败：{exc}", parent=self)
            return
        self.config_snapshot = new_config
        if old_port != port:
            self._stop_server()
            self._start_server()
        if self.discovery:
            self.discovery.stop()
        self.discovery = DiscoveryService(
            self.store.config,
            self._on_peer_found,
            self.server.cert_fingerprint if self.server else "",
        )
        self.discovery.start()
        self._refresh_me()
        self.inbox_path_label.configure(text=new_config.inbox_path)
        messagebox.showinfo("设置", "设置已保存并生效。", parent=self)

    def _open_inbox(self) -> None:
        path = self.store.ensure_inbox()
        self._open_path(str(path))

    def _open_path(self, path: str) -> None:
        target = Path(path)
        if not target.exists():
            messagebox.showerror("打开", f"路径不存在：\n{path}", parent=self)
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开", str(exc), parent=self)

    def _open_inbox_item(self, _event: tk.Event | None = None) -> None:
        selected = self.inbox_tree.selection()
        if not selected:
            return
        values = self.inbox_tree.item(selected[0], "values")
        if len(values) >= 3:
            self._open_path(str(values[2]))

    def _clear_inbox_history(self) -> None:
        self._clear_tree(self.inbox_tree)

    def _copy(self, value: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        children = tree.get_children()
        if children:
            tree.delete(*children)

    def _handle_event(self, name: str, payload: Any) -> None:
        if name == "peer":
            peer = payload
            self.peers[peer.device_id] = peer
            self._refresh_peers_ui()
        elif name == "connected":
            self._connected(payload)
        elif name == "remote_entries":
            path, entries = payload
            self._remote_entries = entries
            self.remote_path_label.configure(text="/" + path if path else "/")
            self._clear_tree(self.remote_tree)
            for entry in entries:
                self.remote_tree.insert(
                    "",
                    "end",
                    values=(
                        ("▸  " if entry.get("is_dir") else "    ") + str(entry.get("name", "")),
                        "文件夹" if entry.get("is_dir") else "文件",
                        "" if entry.get("is_dir") else format_bytes(int(entry.get("size", 0))),
                    ),
                )
        elif name == "incoming":
            self._record_incoming(payload)
        elif name == "incoming_request":
            self._confirm_incoming(payload)
        elif name == "incoming_complete":
            self._record_incoming_complete(str(payload))
        elif name == "transfer_name":
            self.transfer_name.configure(text=str(payload), fg=TEXT)
        elif name == "transfer_progress":
            done, total = payload
            self._update_transfer_progress(done, total)
        elif name == "transfer_done":
            self._finish_transfer(str(payload), True)
        elif name == "transfer_failed":
            text = str(payload)
            self._finish_transfer("已取消" if "取消" in text else f"失败：{text}", False)
            if "取消" not in text:
                messagebox.showerror("传输失败", text, parent=self)
        elif name == "croc_line":
            if hasattr(self, "croc_log"):
                self._croc_log(str(payload))
        elif name == "croc_done":
            return_code, cancelled = payload
            if cancelled:
                self.croc_state.configure(text="公网传输已取消", fg=MUTED)
                self._croc_log("传输已取消。")
            elif return_code == 0:
                self.croc_state.configure(text="公网传输完成", fg=GREEN)
                self._croc_log("传输完成。")
            else:
                self.croc_state.configure(text=f"公网传输结束（代码 {return_code}）", fg=RED)
                self._croc_log(f"croc 已退出，返回代码 {return_code}。")
        elif name == "error":
            messagebox.showerror("操作失败", str(payload), parent=self)
            self.connection_status.configure(text="连接失败", fg=RED)

    def _record_incoming(self, event: IncomingEvent) -> None:
        if not hasattr(self, "inbox_tree"):
            return
        iid = self.inbox_tree.insert(
            "",
            0,
            values=(time.strftime("%m-%d %H:%M:%S"), event.sender, event.path, format_bytes(event.size)),
        )
        self.inbox_tree.selection_set(iid)
        self.transfer_name.configure(text=f"收到文件：{Path(event.path).name}", fg=GREEN)

    def _confirm_incoming(self, session: IncomingSession) -> None:
        total = sum(item.size for item in session.files.values())
        preview = "\n".join(
            f"  {item.relative_path}  ({format_bytes(item.size)})"
            for item in list(session.files.values())[:6]
        )
        if len(session.files) > 6:
            preview += f"\n  …另有 {len(session.files) - 6} 个文件"
        accepted = messagebox.askyesno(
            "接收文件",
            f"{session.sender_name} 请求发送文件\n\n"
            f"批次：{session.batch_name}\n"
            f"文件：{len(session.files)} 个，共 {format_bytes(total)}\n\n"
            f"{preview}\n\n确认接收？",
            parent=self,
            icon="question",
        )
        session.decide(accepted)
        if accepted:
            self.transfer_name.configure(text=f"正在接收 {session.batch_name}…", fg=TEXT)

    def _record_incoming_complete(self, directory: str) -> None:
        self.transfer_name.configure(text=f"接收完成：{Path(directory).name}", fg=GREEN)

    def _drain_events(self) -> None:
        try:
            while True:
                name, payload = self.events.get_nowait()
                self._handle_event(name, payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._drain_events)

    def _on_close(self) -> None:
        if self.transfer_running and not messagebox.askyesno("退出", "当前仍有传输任务，确定退出？", parent=self):
            return
        if self.current_transfer_cancel:
            self.current_transfer_cancel.set()
        if self.croc_session and self.croc_session.running:
            self.croc_session.cancel()
        if self.discovery:
            self.discovery.stop()
        self._stop_server()
        self.destroy()


def run() -> None:
    app = LinkDropApp()
    app.mainloop()

