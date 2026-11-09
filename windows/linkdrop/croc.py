from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable


CROC_RELAY = "1.getcroc.com:9009"
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CODE_RE = re.compile(r"^[A-Za-z0-9-]{6,128}$")


def croc_executable() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        path = Path(frozen_root) / "croc" / "croc.exe"
        if path.exists():
            return path
    project_root = Path(__file__).resolve().parent.parent
    path = project_root / "vendor" / "croc" / "croc.exe"
    if path.exists():
        return path
    raise FileNotFoundError("未找到随应用附带的 croc 公网传输组件。")


def generate_code() -> str:
    # Roughly 48 bits of entropy. The phrase is still single-use and can be
    # rotated by generating a new transfer.
    return f"linkdrop-{secrets.token_hex(6)}"


def clean_output(value: str) -> str:
    value = ANSI_RE.sub("", value)
    value = value.replace("\r", "\n")
    lines = []
    for line in value.splitlines():
        stripped = " ".join(line.split())
        if stripped and (not lines or lines[-1] != stripped):
            lines.append(stripped)
    return "\n".join(lines)


class CrocSession:
    def __init__(
        self,
        on_line: Callable[[str], None],
        on_done: Callable[[int, bool], None],
    ) -> None:
        self.on_line = on_line
        self.on_done = on_done
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.cancelled = False
        self.mode = ""
        self._output_buffer = ""

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start_send(self, paths: list[str], code: str) -> None:
        if not CODE_RE.fullmatch(code):
            raise ValueError("公网口令格式无效。")
        sources = [Path(item).expanduser().resolve() for item in paths]
        if not sources or any(not item.exists() for item in sources):
            raise ValueError("请选择存在且可读取的文件或文件夹。")
        command = [
            str(croc_executable()),
            "--yes",
            "--no-update-check",
            "--disable-clipboard",
            "--relay",
            CROC_RELAY,
            "send",
            "--code",
            code,
            "--transport",
            "auto",
            *[str(item) for item in sources],
        ]
        self.mode = "send"
        self._start(command)

    def start_receive(self, code: str, inbox: Path) -> None:
        code = code.strip()
        if not CODE_RE.fullmatch(code):
            raise ValueError("请输入有效的公网传输口令。")
        inbox.mkdir(parents=True, exist_ok=True)
        command = [
            str(croc_executable()),
            "--yes",
            "--rename",
            "--no-update-check",
            "--disable-clipboard",
            "--relay",
            CROC_RELAY,
            "--out",
            str(inbox),
            code,
        ]
        self.mode = "receive"
        self._start(command)

    def _start(self, command: list[str]) -> None:
        if self.running:
            raise RuntimeError("已有公网传输正在进行。")
        environment = os.environ.copy()
        environment["CROC_NO_UPDATE_CHECK"] = "1"
        environment["CROC_NO_CLIPBOARD"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            creationflags=creationflags,
        )
        self.cancelled = False
        self.thread = threading.Thread(target=self._read_output, name="LinkDropCroc", daemon=True)
        self.thread.start()

    def _read_output(self) -> None:
        assert self.process is not None
        if self.process.stdout is not None:
            for chunk in iter(lambda: self.process.stdout.readline(), ""):
                cleaned = clean_output(chunk)
                if cleaned:
                    self.on_line(cleaned)
        return_code = self.process.wait()
        self.on_done(return_code, self.cancelled)

    def cancel(self) -> None:
        if not self.running or self.process is None:
            return
        self.cancelled = True
        try:
            self.process.terminate()
        except OSError:
            return

        def force_kill() -> None:
            process = self.process
            if process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

        threading.Timer(2.0, force_kill).start()

