"""Tìm / cài / cập nhật Codex CLI (gói npm @openai/codex).

Cài vào thư mục RIÊNG của TubeCLI (`<data>/codex_gpt/npm`) bằng `npm install --prefix`: không cần
sudo, không đụng bản `codex` người dùng tự cài. Máy đã có `codex` toàn cục thì dùng luôn bản đó cho
tới khi người dùng bấm cài bản riêng. Lệnh chạy = `node <.../@openai/codex/bin/codex.js>` khi tìm
được file JS (cùng một cách trên Windows lẫn Linux, khỏi vướng codex.cmd).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

PKG = "@openai/codex"


def data_root() -> Path:
    """Gốc dữ liệu của Codex GPT. Test trỏ sang thư mục tạm bằng TUBECLI_CODEX_GPT_DIR."""
    env = os.environ.get("TUBECLI_CODEX_GPT_DIR", "").strip()
    if env:
        return Path(env)
    from tubecli.config import DATA_DIR
    return Path(DATA_DIR) / "codex_gpt"


def npm_prefix() -> Path:
    return data_root() / "npm"


def _private_js() -> Path:
    return npm_prefix() / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


def _global_js(bin_path: str) -> Optional[Path]:
    """`codex` toàn cục của npm → file codex.js bên cạnh (Windows: %APPDATA%\\npm\\codex.cmd)."""
    p = Path(bin_path).resolve()
    for base in (p.parent, p.parent.parent / "lib"):
        cand = base / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if cand.is_file():
            return cand
    if p.suffix == ".js" and p.is_file():
        return p
    return None


def command() -> Optional[List[str]]:
    """Lệnh gọi Codex (chưa kèm đối số), None nếu máy chưa có.

    Thứ tự: TUBECLI_CODEX_CMD (test / người dùng tự trỏ) → bản riêng của TubeCLI → `codex` toàn cục.
    """
    env = os.environ.get("TUBECLI_CODEX_CMD", "").strip()
    if env:
        try:
            v = json.loads(env)
            if isinstance(v, list) and v:
                return [str(x) for x in v]
        except ValueError:
            pass
        return shlex.split(env, posix=os.name != "nt")
    node = shutil.which("node")
    js = _private_js()
    if node and js.is_file():
        return [node, str(js)]
    g = shutil.which("codex")
    if g:
        gj = _global_js(g)
        if node and gj:
            return [node, str(gj)]
        return [g]
    return None


def source() -> str:
    """«private» | «global» | «env» | «» — cho trang biết đang dùng bản nào."""
    if os.environ.get("TUBECLI_CODEX_CMD", "").strip():
        return "env"
    if shutil.which("node") and _private_js().is_file():
        return "private"
    return "global" if shutil.which("codex") else ""


_VER_CACHE = {"cmd": None, "at": 0.0, "ver": ""}


def version(force: bool = False) -> str:
    """«0.160.0» hoặc «» nếu không chạy được. Đệm 5 phút (gọi mỗi lần mở trang)."""
    cmd = command()
    if not cmd:
        return ""
    now = time.time()
    if not force and _VER_CACHE["cmd"] == cmd and now - _VER_CACHE["at"] < 300:
        return _VER_CACHE["ver"]
    ver = ""
    try:
        p = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        m = re.search(r"(\d+\.\d+\.\d+[\w.-]*)", (p.stdout or "") + (p.stderr or ""))
        ver = m.group(1) if m else ""
    except Exception:      # noqa: BLE001
        ver = ""
    _VER_CACHE.update(cmd=cmd, at=now, ver=ver)
    return ver


# ── cài / cập nhật (chạy nền, trang hỏi /status) ─────────────────────────────
_INSTALL = {"state": "idle", "log": "", "at": 0}      # idle | running | done | error
_LOCK = threading.Lock()


def install_state() -> dict:
    with _LOCK:
        return dict(_INSTALL)


def _log(line: str, state: Optional[str] = None) -> None:
    with _LOCK:
        if state:
            _INSTALL["state"] = state
        _INSTALL["at"] = int(time.time())
        if line:
            _INSTALL["log"] = (_INSTALL["log"] + line.rstrip() + "\n")[-4000:]


def _install_worker(spec: str) -> None:
    npm = shutil.which("npm")
    if not npm or not shutil.which("node"):
        _log("Node.js / npm not found on this machine — install Node.js 18+ first.", "error")
        return
    prefix = npm_prefix()
    prefix.mkdir(parents=True, exist_ok=True)
    _log(f"npm install --prefix {prefix} {spec}")
    try:
        p = subprocess.run([npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund", spec],
                           capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _log("npm install timed out (15 min).", "error")
        return
    except Exception as e:      # noqa: BLE001
        _log(f"npm install failed: {e}", "error")
        return
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0 or not _private_js().is_file():
        _log((out or "npm install failed")[-1500:], "error")
        return
    ver = version(force=True)
    _log(f"Codex CLI {ver or '?'} installed.", "done")


def start_install(spec: str = PKG + "@latest") -> dict:
    """Cài/cập nhật nền. Đang chạy thì trả trạng thái hiện có (không chạy chồng)."""
    with _LOCK:
        if _INSTALL["state"] == "running":
            return dict(_INSTALL)
        _INSTALL.update(state="running", log="", at=int(time.time()))
    threading.Thread(target=_install_worker, args=(spec,), daemon=True, name="codex-gpt-install").start()
    return install_state()
