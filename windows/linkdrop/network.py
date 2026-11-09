from __future__ import annotations

import http.client
import hashlib
import json
import mimetypes
import os
import queue
import secrets
import socket
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .certificates import certificate_fingerprint, client_ssl_context, ensure_certificate, server_ssl_context
from .config import AppConfig, ConfigStore, DISCOVERY_PORT, app_data_dir


PROTOCOL_VERSION = 1
BUFFER_SIZE = 1024 * 1024
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_FAILURES = 8


class LinkDropError(RuntimeError):
    pass


class TransferCancelled(LinkDropError):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_relative(raw: str, *, allow_empty: bool = False) -> Path:
    value = raw.replace("\\", "/").strip("/")
    path = Path(value)
    if not value:
        if allow_empty:
            return Path(".")
        raise ValueError("无效的相对路径")
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise ValueError("无效的相对路径")
    return path


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(2, 10_000):
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    raise LinkDropError("无法生成不重复的文件名")


def _human_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def blocked(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(address, []) if now - stamp < LOGIN_WINDOW_SECONDS]
            self._attempts[address] = recent
            return len(recent) >= LOGIN_MAX_FAILURES

    def failed(self, address: str) -> None:
        with self._lock:
            self._attempts.setdefault(address, []).append(time.monotonic())

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


@dataclass
class IncomingEvent:
    path: str
    size: int
    sender: str


@dataclass
class IncomingFile:
    id: str
    relative_path: str
    size: int
    sha256: str


@dataclass
class IncomingSession:
    id: str
    sender_id: str
    sender_name: str
    batch_name: str
    directory: Path
    files: dict[str, IncomingFile]
    tokens: dict[str, str]
    decision_event: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False
    uploaded: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)

    def decide(self, accepted: bool) -> None:
        self.accepted = accepted
        self.decision_event.set()

    def wait(self, timeout: float = 120.0) -> bool:
        return self.decision_event.wait(timeout) and self.accepted


class TransferHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: ConfigStore,
        events: "queue.Queue[tuple[str, Any]]",
    ) -> None:
        self.store = store
        self.events = events
        self.rate_limiter = RateLimiter()
        self.sessions: dict[str, IncomingSession] = {}
        self.sessions_lock = threading.Lock()
        key_path, cert_path = ensure_certificate(store.data_dir())
        self.cert_fingerprint = certificate_fingerprint(cert_path)
        super().__init__(address, TransferRequestHandler)
        self.socket = server_ssl_context(store.data_dir()).wrap_socket(self.socket, server_side=True)

    def cleanup_sessions(self) -> None:
        cutoff = time.monotonic() - 900
        with self.sessions_lock:
            stale = [
                session_id
                for session_id, session in self.sessions.items()
                if session.created_at < cutoff
            ]
            for session_id in stale:
                self.sessions.pop(session_id, None)


class TransferRequestHandler(BaseHTTPRequestHandler):
    server: TransferHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: Any) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _client_address(self) -> str:
        return self.client_address[0]

    def _auth(self) -> bool:
        address = self._client_address()
        if self.server.rate_limiter.blocked(address):
            self._error(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数过多，请 5 分钟后再试。")
            return False
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = self.server.store.config.pair_code
        if supplied and secrets.compare_digest(supplied, expected):
            self.server.rate_limiter.succeeded(address)
            return True
        self.server.rate_limiter.failed(address)
        self._error(HTTPStatus.UNAUTHORIZED, "配对码错误或已变更。")
        return False

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(self.path)
        return dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))

    def _route_path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self) -> None:
        try:
            route = self._route_path()
            query = self._query()
            config = self.server.store.config
            if route == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "app": "LinkDrop",
                        "version": PROTOCOL_VERSION,
                        "device_id": config.device_id,
                        "device_name": config.device_name,
                        "tls": True,
                    },
                )
                return
            if not self._auth():
                return
            if route == "/api/info":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "app": "LinkDrop",
                        "version": PROTOCOL_VERSION,
                        "device_id": config.device_id,
                        "device_name": config.device_name,
                        "allow_incoming": config.allow_incoming,
                        "cert_fingerprint": self.server.cert_fingerprint,
                        "tls": True,
                    },
                )
            elif route == "/api/shares":
                self._shares()
            elif route == "/api/list":
                self._list(query)
            elif route == "/api/download":
                self._download(query)
            elif route == "/api/zip":
                self._zip(query)
            else:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在。")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, _human_error(exc))
            except OSError:
                pass

    def do_POST(self) -> None:
        try:
            route = self._route_path()
            if not self._auth():
                return
            if route == "/api/prepare":
                self._prepare()
            elif route == "/api/upload":
                self._upload(self._query())
            else:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在。")
                return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, _human_error(exc))
            except OSError:
                pass

    def _read_json(self, limit: int = 8 * 1024 * 1024) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise LinkDropError("请求缺少长度。")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise LinkDropError("请求长度无效。") from exc
        if length < 0 or length > limit:
            raise LinkDropError("请求内容过大。")
        data = self.rfile.read(length)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LinkDropError("请求格式无效。") from exc
        if not isinstance(value, dict):
            raise LinkDropError("请求格式无效。")
        return value

    def _prepare(self) -> None:
        config = self.server.store.config
        if not config.allow_incoming:
            self._error(HTTPStatus.FORBIDDEN, "对方已关闭文件接收。")
            return
        body = self._read_json()
        sender_id = str(body.get("device_id", "")).strip()[:64]
        sender_name = str(body.get("device_name", "")).strip()[:48] or "未知设备"
        batch_raw = str(body.get("batch", "本次传输"))
        raw_items = body.get("files")
        if not raw_items or not isinstance(raw_items, list):
            self._error(HTTPStatus.BAD_REQUEST, "传输内容为空。")
            return
        if len(raw_items) > 10_000:
            self._error(HTTPStatus.BAD_REQUEST, "单次文件数量过多。")
            return
        try:
            batch = _safe_relative(batch_raw)
            if len(batch.parts) != 1:
                raise ValueError("批次名称不能包含目录")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        files: dict[str, IncomingFile] = {}
        seen_paths: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                self._error(HTTPStatus.BAD_REQUEST, "文件信息无效。")
                return
            file_id = str(raw.get("id", "")).strip()
            relative_raw = str(raw.get("relative_path", ""))
            digest = str(raw.get("sha256", "")).strip().lower()
            try:
                size = int(raw.get("size", -1))
                relative = _safe_relative(relative_raw)
            except (TypeError, ValueError):
                self._error(HTTPStatus.BAD_REQUEST, "文件路径或大小无效。")
                return
            normalized = str(relative).replace("\\", "/")
            if (
                not file_id
                or file_id in files
                or normalized in seen_paths
                or size < 0
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                self._error(HTTPStatus.BAD_REQUEST, "文件信息无效。")
                return
            seen_paths.add(normalized)
            files[file_id] = IncomingFile(file_id, normalized, size, digest)

        inbox = self.server.store.ensure_inbox()
        session_dir = _unique_path(inbox / batch)
        session_id = secrets.token_urlsafe(18)
        tokens = {file_id: secrets.token_urlsafe(32) for file_id in files}
        session = IncomingSession(
            id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            batch_name=batch.name,
            directory=session_dir,
            files=files,
            tokens=tokens,
        )
        self.server.cleanup_sessions()
        with self.server.sessions_lock:
            self.server.sessions[session_id] = session
        self.server.events.put(("incoming_request", session))
        if not session.wait(120.0):
            with self.server.sessions_lock:
                self.server.sessions.pop(session_id, None)
            self._error(HTTPStatus.FORBIDDEN, "接收方拒绝或确认超时。")
            return
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            with self.server.sessions_lock:
                self.server.sessions.pop(session_id, None)
            raise LinkDropError(f"无法创建接收目录：{exc}") from exc
        total_size = sum(item.size for item in files.values())
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "session_id": session_id,
                "directory": str(session_dir),
                "tokens": tokens,
                "total_size": total_size,
            },
        )

    def _find_share(self, share_id: str):
        for share in self.server.store.config.shares:
            if share.id == share_id:
                return share
        return None

    def _resolve_shared(self, share_id: str, relative: str) -> Path:
        share = self._find_share(share_id)
        if share is None:
            raise LinkDropError("共享文件夹不存在或共享已取消。")
        base = Path(share.path).expanduser().resolve()
        if not base.exists() or not base.is_dir():
            raise LinkDropError("共享文件夹当前无法访问。")
        child = _safe_relative(relative, allow_empty=True)
        candidate = (base / child).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise LinkDropError("路径超出共享范围。") from exc
        return candidate

    def _shares(self) -> None:
        result = []
        for share in self.server.store.config.shares:
            path = Path(share.path)
            result.append(
                {
                    "id": share.id,
                    "name": share.name,
                    "available": path.exists() and path.is_dir(),
                }
            )
        self._send_json(HTTPStatus.OK, {"ok": True, "shares": result})

    def _list(self, query: Mapping[str, str]) -> None:
        share_id = query.get("share_id", "")
        relative = query.get("path", "") or "."
        if relative == ".":
            relative = ""
        target = self._resolve_shared(share_id, relative)
        if not target.exists():
            self._error(HTTPStatus.NOT_FOUND, "文件或文件夹不存在。")
            return
        if target.is_file():
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "path": relative,
                    "entries": [
                        {
                            "name": target.name,
                            "path": relative,
                            "is_dir": False,
                            "size": target.stat().st_size,
                            "modified": target.stat().st_mtime,
                        }
                    ],
                },
            )
            return
        entries = []
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except PermissionError as exc:
            raise LinkDropError("没有权限读取这个文件夹。") from exc
        for child in children:
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except OSError:
                continue
            rel = child.relative_to(target)
            entries.append(
                {
                    "name": child.name,
                    "path": str(Path(relative) / rel).replace("\\", "/"),
                    "is_dir": is_dir,
                    "size": 0 if is_dir else stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        self._send_json(HTTPStatus.OK, {"ok": True, "path": relative, "entries": entries})

    def _download(self, query: Mapping[str, str]) -> None:
        target = self._resolve_shared(query.get("share_id", ""), query.get("path", ""))
        if not target.exists() or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "文件不存在。")
            return
        size = target.stat().st_size
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name))
        self.send_header("Connection", "close")
        self.end_headers()
        with target.open("rb") as stream:
            while True:
                block = stream.read(BUFFER_SIZE)
                if not block:
                    break
                self.wfile.write(block)

    def _zip(self, query: Mapping[str, str]) -> None:
        target = self._resolve_shared(query.get("share_id", ""), query.get("path", ""))
        if not target.exists() or not target.is_dir():
            self._error(HTTPStatus.NOT_FOUND, "文件夹不存在。")
            return
        temp_dir = app_data_dir() / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as output:
            archive_path = Path(output.name)
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for root, dirs, files in os.walk(target):
                    dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                    relative_root = Path(root).relative_to(target)
                    if not files and not dirs:
                        archive.writestr(str(Path(target.name) / relative_root) + "/", "")
                    for name in files:
                        source = Path(root) / name
                        if source.is_symlink():
                            continue
                        arcname = Path(target.name) / relative_root / name
                        archive.write(source, arcname=str(arcname))
            size = output.tell()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(f"{target.name}.zip"))
            self.send_header("Connection", "close")
            self.end_headers()
            with archive_path.open("rb") as archive_stream:
                while True:
                    block = archive_stream.read(BUFFER_SIZE)
                    if not block:
                        break
                    self.wfile.write(block)
        finally:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _upload(self, query: Mapping[str, str]) -> None:
        session_id = query.get("session_id", "")
        file_id = query.get("file_id", "")
        supplied_token = query.get("token", "")
        with self.server.sessions_lock:
            session = self.server.sessions.get(session_id)
            if session is None or not session.accepted:
                self._error(HTTPStatus.FORBIDDEN, "传输会话不存在或未获确认。")
                return
            item = session.files.get(file_id)
            expected_token = session.tokens.get(file_id, "")
            if item is None or not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
                self._error(HTTPStatus.FORBIDDEN, "文件传输令牌无效。")
                return
            if file_id in session.uploaded:
                self._error(HTTPStatus.CONFLICT, "这个文件已经接收完成。")
                return
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "请求缺少文件长度。")
            return
        try:
            length = int(length_raw)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "文件长度无效。")
            return
        if length != item.size:
            self._error(HTTPStatus.BAD_REQUEST, "文件大小与会话信息不一致。")
            return
        relative = _safe_relative(item.relative_path)
        destination = (session.directory / relative).resolve()
        try:
            destination.relative_to(session.directory)
        except ValueError as exc:
            raise LinkDropError("接收路径无效。") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._error(HTTPStatus.CONFLICT, "接收目录中已存在同名文件。")
            return
        partial = destination.with_name(destination.name + ".linkdrop-part")
        received = 0
        hasher = hashlib.sha256()
        try:
            with partial.open("wb") as stream:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(BUFFER_SIZE, remaining))
                    if not block:
                        raise LinkDropError("发送方连接中断，文件未接收完整。")
                    stream.write(block)
                    hasher.update(block)
                    remaining -= len(block)
                    received += len(block)
            digest = hasher.hexdigest()
            if not secrets.compare_digest(digest, item.sha256):
                partial.unlink(missing_ok=True)
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "SHA-256 校验失败，文件已丢弃。")
                return
            os.replace(partial, destination)
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        with self.server.sessions_lock:
            session.uploaded.add(file_id)
            complete = len(session.uploaded) == len(session.files)
        self.server.events.put(("incoming", IncomingEvent(str(destination), received, session.sender_name)))
        if complete:
            self.server.events.put(("incoming_complete", str(session.directory)))
        self._send_json(
            HTTPStatus.CREATED,
            {"ok": True, "path": str(destination), "size": received, "sha256": digest},
        )


@dataclass
class Peer:
    device_id: str
    name: str
    host: str
    port: int
    cert_fingerprint: str = ""

    @property
    def address(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"


class DiscoveryService:
    def __init__(
        self,
        config: AppConfig,
        found: Callable[[Peer], None],
        cert_fingerprint: str = "",
    ) -> None:
        self.config = config
        self.found = found
        self.cert_fingerprint = cert_fingerprint
        self.stop_event = threading.Event()
        self._receiver: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._receiver = self._create_socket()
        try:
            self._receiver.bind(("", DISCOVERY_PORT))
        except OSError:
            self._receiver.close()
            self._receiver = None
        if self._receiver:
            thread = threading.Thread(target=self._listen, name="LinkDropDiscovery", daemon=True)
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _create_socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        return sock

    def scan(self) -> None:
        payload = _json_bytes(
            {
                "app": "LinkDrop",
                "version": PROTOCOL_VERSION,
                "device_id": self.config.device_id,
                "device_name": self.config.device_name,
                "port": self.config.port,
                "tls": True,
                "cert_fingerprint": self.cert_fingerprint,
            }
        )
        sock = self._create_socket()
        try:
            sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        finally:
            sock.close()

    def _listen(self) -> None:
        assert self._receiver is not None
        while not self.stop_event.is_set():
            try:
                body, address = self._receiver.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if message.get("app") != "LinkDrop":
                continue
            device_id = str(message.get("device_id", ""))
            if not device_id or device_id == self.config.device_id:
                continue
            try:
                port = int(message.get("port", 0))
            except (TypeError, ValueError):
                continue
            if not 1 <= port <= 65535:
                continue
            peer = Peer(
                device_id=device_id,
                name=str(message.get("device_name") or address[0])[:48],
                host=address[0],
                port=port,
                cert_fingerprint=str(message.get("cert_fingerprint", "")),
            )
            self.found(peer)
            self.scan()

    def stop(self) -> None:
        self.stop_event.set()
        if self._receiver:
            try:
                self._receiver.close()
            except OSError:
                pass


def parse_address(value: str) -> tuple[str, int]:
    raw = value.strip()
    if not raw:
        raise ValueError("请输入对方地址。")
    if raw.lower().startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(raw)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            raise ValueError("地址格式应为 IP:端口。")
        return host, port
    if raw.startswith("["):
        host, separator, port_raw = raw[1:].partition("]:")
    elif raw.count(":") == 1:
        host, port_raw = raw.rsplit(":", 1)
        separator = ":"
    else:
        host, port_raw, separator = raw, "", ""
    try:
        port = int(port_raw) if separator else 48610
    except ValueError as exc:
        raise ValueError("端口必须是数字。") from exc
    if not host or not 1 <= port <= 65535:
        raise ValueError("地址格式应为 IP:端口。")
    return host.strip(), port


class RemoteClient:
    def __init__(self, host: str, port: int, pair_code: str) -> None:
        self.host = host
        self.port = port
        self.pair_code = pair_code.strip()
        self.peer_fingerprint = ""

    def _connection(self, timeout: float = 12.0) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=timeout,
            context=client_ssl_context(),
        )

    def _capture_fingerprint(self, connection: http.client.HTTPSConnection) -> str:
        sock = connection.sock
        if sock is None:
            return self.peer_fingerprint
        certificate = sock.getpeercert(binary_form=True)
        if certificate:
            digest = hashlib.sha256(certificate).hexdigest().upper()
            self.peer_fingerprint = ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))
        return self.peer_fingerprint

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, dict[str, str], Any]:
        request_headers = {
            "Authorization": f"Bearer {self.pair_code}",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        connection = self._connection(timeout)
        try:
            connection.connect()
            self._capture_fingerprint(connection)
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            try:
                value = json.loads(data.decode("utf-8")) if data else {}
            except (UnicodeDecodeError, ValueError):
                value = {}
            if response.status >= 400:
                raise LinkDropError(str(value.get("error") or f"HTTP {response.status}"))
            return response.status, response_headers, value
        except (OSError, http.client.HTTPException) as exc:
            raise LinkDropError(f"无法连接对方：{_human_error(exc)}") from exc
        finally:
            connection.close()

    def info(self) -> dict[str, Any]:
        info = self._request("GET", "/api/info")[2]
        declared = str(info.get("cert_fingerprint", ""))
        if declared and self.peer_fingerprint and not secrets.compare_digest(declared, self.peer_fingerprint):
            raise LinkDropError("TLS 身份信息与设备声明不一致，已停止连接。")
        return info

    def shares(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/shares")[2].get("shares", []))

    def list_folder(self, share_id: str, path: str = "") -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"share_id": share_id, "path": path})
        return list(self._request("GET", f"/api/list?{query}")[2].get("entries", []))

    def download(
        self,
        share_id: str,
        relative: str,
        destination: Path,
        progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        query = urllib.parse.urlencode({"share_id": share_id, "path": relative})
        connection = self._connection(30.0)
        partial = destination.with_name(destination.name + ".linkdrop-part")
        try:
            connection.connect()
            self._capture_fingerprint(connection)
            connection.request(
                "GET",
                f"/api/download?{query}",
                headers={"Authorization": f"Bearer {self.pair_code}"},
            )
            response = connection.getresponse()
            if response.status != 200:
                body = response.read()
                try:
                    message = json.loads(body.decode("utf-8")).get("error")
                except (UnicodeDecodeError, ValueError):
                    message = None
                raise LinkDropError(message or f"下载失败（HTTP {response.status}）")
            total = int(response.getheader("Content-Length", "0") or 0)
            destination.parent.mkdir(parents=True, exist_ok=True)
            received = 0
            with partial.open("wb") as stream:
                while True:
                    if cancel and cancel.is_set():
                        raise TransferCancelled("传输已取消。")
                    block = response.read(BUFFER_SIZE)
                    if not block:
                        break
                    stream.write(block)
                    received += len(block)
                    if progress:
                        progress(received, total)
            final = _unique_path(destination) if destination.exists() else destination
            os.replace(partial, final)
            return final
        finally:
            connection.close()
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    def download_zip(
        self,
        share_id: str,
        relative: str,
        destination: Path,
        cancel: threading.Event | None = None,
    ) -> Path:
        query = urllib.parse.urlencode({"share_id": share_id, "path": relative})
        connection = self._connection(60.0)
        partial = destination.with_name(destination.name + ".linkdrop-part")
        try:
            connection.connect()
            self._capture_fingerprint(connection)
            connection.request(
                "GET",
                f"/api/zip?{query}",
                headers={"Authorization": f"Bearer {self.pair_code}"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise LinkDropError(f"打包下载失败（HTTP {response.status}）")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as stream:
                while True:
                    if cancel and cancel.is_set():
                        raise TransferCancelled("传输已取消。")
                    block = response.read(BUFFER_SIZE)
                    if not block:
                        break
                    stream.write(block)
            final = _unique_path(destination) if destination.exists() else destination
            os.replace(partial, final)
            return final
        finally:
            connection.close()
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    def prepare_transfer(
        self,
        items: list[tuple[Path, str]],
        batch: str,
        device_id: str,
        device_name: str,
        hash_progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
        waiting: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        total = sum(source.stat().st_size for source, _relative in items)
        hashed_total = 0
        files = []
        for source, relative in items:
            if cancel and cancel.is_set():
                raise TransferCancelled("传输已取消。")
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                while True:
                    if cancel and cancel.is_set():
                        raise TransferCancelled("传输已取消。")
                    block = stream.read(BUFFER_SIZE)
                    if not block:
                        break
                    digest.update(block)
                    hashed_total += len(block)
                    if hash_progress:
                        hash_progress(hashed_total, total)
            files.append(
                {
                    "id": uuid.uuid4().hex,
                    "relative_path": relative,
                    "size": source.stat().st_size,
                    "sha256": digest.hexdigest(),
                }
            )
        payload = _json_bytes(
            {
                "device_id": device_id,
                "device_name": device_name,
                "batch": batch,
                "files": files,
            }
        )
        if waiting:
            waiting()
        result = self._request(
            "POST",
            "/api/prepare",
            body=payload,
            headers={"Content-Type": "application/json"},
            timeout=130.0,
        )[2]
        result["file_items"] = files
        return result

    def upload_file(
        self,
        source: Path,
        session_id: str,
        file_id: str,
        token: str,
        progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        size = source.stat().st_size
        query = urllib.parse.urlencode(
            {"session_id": session_id, "file_id": file_id, "token": token}
        )
        connection = self._connection(60.0)
        try:
            connection.connect()
            self._capture_fingerprint(connection)
            connection.putrequest("POST", f"/api/upload?{query}")
            connection.putheader("Authorization", f"Bearer {self.pair_code}")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            sent = 0
            with source.open("rb") as stream:
                while True:
                    if cancel and cancel.is_set():
                        raise TransferCancelled("传输已取消。")
                    block = stream.read(BUFFER_SIZE)
                    if not block:
                        break
                    connection.send(block)
                    sent += len(block)
                    if progress:
                        progress(sent, size)
            response = connection.getresponse()
            data = response.read()
            try:
                value = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                value = {}
            if response.status >= 400:
                raise LinkDropError(str(value.get("error") or f"发送失败（HTTP {response.status}）"))
            return value
        except (OSError, http.client.HTTPException) as exc:
            raise LinkDropError(f"发送失败：{_human_error(exc)}") from exc
        finally:
            connection.close()


def collect_files(paths: Iterable[str]) -> tuple[list[tuple[Path, str]], str]:
    collected: list[tuple[Path, Path, Path]] = []
    roots: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            continue
        roots.append(path)
        if path.is_file():
            collected.append((path, path, Path(path.name)))
        elif path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                for name in files:
                    source = Path(root) / name
                    if source.is_symlink():
                        continue
                    rel = source.relative_to(path)
                    collected.append((source, path, rel))
    if not collected:
        raise LinkDropError("所选位置没有可发送的文件。")
    if len(roots) == 1 and roots[0].is_dir():
        batch = roots[0].name
        items = [(source, str(relative)) for source, _root, relative in collected]
    else:
        batch = f"文件_{time.strftime('%Y-%m-%d_%H%M%S')}"
        items = [
            (source, str(Path(root.name) / relative) if root.is_dir() else str(relative))
            for source, root, relative in collected
        ]
    return items, batch


def format_bytes(value: int | float) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(number)} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if not address.startswith("127."):
                addresses.add(address)
        finally:
            probe.close()
    except OSError:
        pass
    return sorted(addresses, key=lambda item: (item.startswith("100."), item))

