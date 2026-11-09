from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_NAME = "LinkDrop"
DEFAULT_PORT = 48610
DISCOVERY_PORT = 48611


def app_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def default_inbox() -> Path:
    documents = Path.home() / "Documents"
    if not documents.exists():
        documents = Path.home()
    return documents / APP_NAME / "Inbox"


def friendly_device_name() -> str:
    raw = socket.gethostname().strip() or "Windows 设备"
    return raw[:48]


@dataclass
class Share:
    id: str
    name: str
    path: str

    @classmethod
    def create(cls, path: str, name: str | None = None) -> "Share":
        resolved = Path(path).expanduser().resolve()
        return cls(
            id=uuid.uuid4().hex[:12],
            name=(name or resolved.name or str(resolved))[:64],
            path=str(resolved),
        )


@dataclass
class AppConfig:
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    device_name: str = field(default_factory=friendly_device_name)
    port: int = DEFAULT_PORT
    pair_code: str = field(default_factory=lambda: f"{secrets.randbelow(1_000_000):06d}")
    inbox_path: str = field(default_factory=lambda: str(default_inbox()))
    shares: list[Share] = field(default_factory=list)
    allow_incoming: bool = True
    trusted_devices: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        shares = [
            Share(
                id=str(item.get("id", "")),
                name=str(item.get("name", "共享文件夹")),
                path=str(item.get("path", "")),
            )
            for item in raw.get("shares", [])
            if item.get("id") and item.get("path")
        ]
        port = int(raw.get("port", DEFAULT_PORT))
        if not 1024 <= port <= 65535:
            port = DEFAULT_PORT
        pair_code = str(raw.get("pair_code", "")).strip()
        if len(pair_code) != 6 or not pair_code.isdigit():
            pair_code = f"{secrets.randbelow(1_000_000):06d}"
        return cls(
            device_id=str(raw.get("device_id") or uuid.uuid4().hex),
            device_name=str(raw.get("device_name") or friendly_device_name())[:48],
            port=port,
            pair_code=pair_code,
            inbox_path=str(raw.get("inbox_path") or default_inbox()),
            shares=shares,
            allow_incoming=bool(raw.get("allow_incoming", True)),
            trusted_devices={
                str(device_id): str(fingerprint)
                for device_id, fingerprint in dict(raw.get("trusted_devices", {})).items()
                if device_id and fingerprint
            },
        )


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "config.json")
        self._lock = threading.RLock()
        self.config = self._load()

    def _load(self) -> AppConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return AppConfig.from_dict(raw)
        except (OSError, ValueError, TypeError):
            config = AppConfig()
            self._write(config)
            return config

    def _write(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def save(self) -> None:
        with self._lock:
            self._write(self.config)

    def snapshot(self) -> AppConfig:
        with self._lock:
            raw = asdict(self.config)
            raw["shares"] = [asdict(item) for item in self.config.shares]
            return AppConfig.from_dict(raw)

    def update(self, **changes: Any) -> AppConfig:
        with self._lock:
            for key, value in changes.items():
                if hasattr(self.config, key):
                    setattr(self.config, key, value)
            self._write(self.config)
            return self.snapshot()

    def add_share(self, path: str, name: str | None = None) -> Share:
        with self._lock:
            share = Share.create(path, name)
            if any(str(Path(item.path)).casefold() == share.path.casefold() for item in self.config.shares):
                raise ValueError("这个文件夹已经添加为共享。")
            self.config.shares.append(share)
            self._write(self.config)
            return share

    def remove_share(self, share_id: str) -> None:
        with self._lock:
            self.config.shares = [item for item in self.config.shares if item.id != share_id]
            self._write(self.config)

    def ensure_inbox(self) -> Path:
        path = Path(self.config.inbox_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def data_dir(self) -> Path:
        return self.path.parent

    def trust_device(self, device_id: str, fingerprint: str) -> bool:
        """Remember a first-seen TLS identity. Return False on identity change."""
        with self._lock:
            current = self.config.trusted_devices.get(device_id)
            if current and not secrets.compare_digest(current, fingerprint):
                return False
            if current != fingerprint:
                self.config.trusted_devices[device_id] = fingerprint
                self._write(self.config)
            return True

