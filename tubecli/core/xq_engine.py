"""Engine cờ tướng THẬT cho skill xiangqi.move — mức Vừa/Khó (user 28/9/2026: đổi Vừa→Khó
«đánh giống y chan»; chọn phương án «b» = engine mạnh thật).

Vì sao hai mức từng giống hệt: engine alpha-beta thuần Python của AI Arena xong lớp 5 sau
~1.5 s, lớp 6 thì 6 s chưa xong — Vừa (2.2 s) và Khó (2.8 s) cùng dừng ở lớp 5, cùng nước.

Chọn Fairy-Stockfish 14.0.1 XQ, KHÔNG phải Pikafish: file mạng pikafish.nnue ghi «No
commercial use without permission», còn mạng cờ tướng của Fairy-Stockfish là CC0 (chính
giấy phép Pikafish chỉ sang). Engine là GPL-3, chạy như TIẾN TRÌNH RIÊNG qua UCI — không
link vào TubeCLI. Đo trên máy chủ local: độ sâu 19 trong 1 s, mạng NNUE nhúng sẵn.

Binary (~24 MB) tải lần đầu cần từ release cố định trên GitHub, kiểm SHA-256 ghim sẵn,
thử bản bmi2 → modern → x86-64 (CPU cũ chạy bmi2 là chết «illegal instruction»). HĐH/CPU
không có bản (ARM, macOS) → None: skill lùi về engine Python của Arena như trước.

Toạ độ: TubeCLI/cloud đánh số hàng 0–9 (h2e2), Fairy-Stockfish 1–10 (h3e3). FEN giống nhau.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("xq_engine")

RELEASE = "https://github.com/fairy-stockfish/Fairy-Stockfish/releases/download/fairy_sf_14_0_1_xq/"
_ASSETS = {
    "windows": [
        ("fairy-stockfish-largeboard_x86-64-bmi2.exe", "3e95f29fc5ce4d8f7fb3df433bc8323f4ff0386cfa1bd8195316e7db35d3d503"),
        ("fairy-stockfish-largeboard_x86-64-modern.exe", "f894e6db3e5f2842da57dbeab33505aabf976f55afccd30bb87c78cb8bcf2bb3"),
        ("fairy-stockfish-largeboard_x86-64.exe", "38c173576c13407b610a03da8c31144bbdee9f49d9d789c4e7b681cc18d36221"),
    ],
    "linux": [
        ("fairy-stockfish-largeboard_x86-64-bmi2", "432ce4325533fa40bb4dfeb03ce7338bd8c994c602cad65b5452b1e8a6dfbaf3"),
        ("fairy-stockfish-largeboard_x86-64-modern", "6009e907051b95a39cedfd7ba2e08ee3d6d8ba58a3fd3817763704830f91e1cc"),
        ("fairy-stockfish-largeboard_x86-64", "7584bbf366f4be1c88ed37ded80c496dd561ddf0c5b4f5a710702500aa49dc56"),
    ],
}
RETRY_AFTER_FAIL_SEC = 3600       # cài hỏng (mạng, CPU lạ) → không thử lại mỗi nước cờ
HASH_MB = 32                      # máy chủ VPS 8 GB từng sập RAM — engine giữ phần nhỏ
MATE = 100000

_install_lock = threading.Lock()
_state = {"path": None, "failed_at": 0.0, "installing": False}
_engine_lock = threading.Lock()
_engine: Optional["_Uci"] = None
_FSF_MOVE_RE = re.compile(r"^([a-i])(10|[1-9])([a-i])(10|[1-9])$")
_OUR_MOVE_RE = re.compile(r"^([a-i])([0-9])([a-i])([0-9])$")


def _platform_key() -> str:
    mach = platform.machine().lower()
    if mach not in ("x86_64", "amd64"):
        return ""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return ""


def _dir() -> Path:
    from tubecli.config import DATA_DIR

    d = Path(str(DATA_DIR)) / "engines" / "fairy_stockfish"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_fsf(move: str) -> str:
    m = _OUR_MOVE_RE.match(move or "")
    return f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}{int(m.group(4)) + 1}" if m else ""


def from_fsf(move: str) -> str:
    m = _FSF_MOVE_RE.match(move or "")
    return f"{m.group(1)}{int(m.group(2)) - 1}{m.group(3)}{int(m.group(4)) - 1}" if m else ""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _handshake(path: Path) -> bool:
    """Chạy thử: phải trả «uciok» và biết biến thể xiangqi — CPU không hợp là chết ngay."""
    try:
        r = subprocess.run([str(path)], input="uci\nquit\n", capture_output=True, text=True,
                           timeout=20, cwd=str(path.parent), **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "uciok" in r.stdout and "xiangqi" in r.stdout


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}


def _install_blocking() -> Optional[Path]:
    import requests

    key = _platform_key()
    if not key:
        return None
    d = _dir()
    marker = d / "engine.txt"
    if marker.exists():
        p = d / marker.read_text(encoding="utf-8").strip()
        if p.is_file():
            return p
    for name, sha in _ASSETS[key]:
        final = d / name
        tmp = d / (name + ".part")
        try:
            if not (final.is_file() and _sha256(final) == sha):
                with requests.get(RELEASE + name, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if _sha256(tmp) != sha:
                    logger.warning("xq engine %s: SHA-256 lệch — bỏ", name)
                    tmp.unlink(missing_ok=True)
                    continue
                os.replace(tmp, final)
            if not sys.platform.startswith("win"):
                final.chmod(0o755)
            if _handshake(final):
                marker.write_text(name, encoding="utf-8")
                logger.info("xq engine sẵn sàng: %s", name)
                return final
            logger.info("xq engine %s không chạy được trên CPU này — thử bản tiếp", name)
            final.unlink(missing_ok=True)
        except Exception as e:      # noqa: BLE001
            logger.warning("xq engine tải %s hỏng: %s", name, e)
            tmp.unlink(missing_ok=True)
    return None


def _install_worker() -> None:
    try:
        p = _install_blocking()
    finally:
        with _install_lock:
            _state["installing"] = False
    with _install_lock:
        if p:
            _state["path"] = p
        else:
            _state["failed_at"] = time.time()


def ensure(wait: float = 0.0) -> Optional[Path]:
    """Đường dẫn engine nếu dùng được. Chưa có thì tải NỀN (một luồng cho cả máy) và chờ tối
    đa `wait` giây; quá giờ → None, lượt này lùi về engine Python, lượt sau đã có."""
    with _install_lock:
        if _state["path"]:
            return _state["path"]
        if time.time() - _state["failed_at"] < RETRY_AFTER_FAIL_SEC:
            return None
        if not _state["installing"]:
            _state["installing"] = True
            threading.Thread(target=_install_worker, name="xq-engine-install", daemon=True).start()
    deadline = time.monotonic() + max(0.0, wait)
    while time.monotonic() < deadline:
        with _install_lock:
            if _state["path"] or not _state["installing"]:
                return _state["path"]
        time.sleep(0.25)
    return None


class _Uci:
    """Một tiến trình Fairy-Stockfish sống lâu; mọi lệnh đi qua _engine_lock."""

    def __init__(self, path: Path):
        self.p = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  cwd=str(path.parent), **_no_window())
        self.q: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.multipv = 1
        self._send("uci")
        self._until(lambda ln: ln == "uciok", 15)
        self._send("setoption name UCI_Variant value xiangqi")
        self._send("setoption name Threads value 1")
        self._send(f"setoption name Hash value {HASH_MB}")
        self._send("isready")
        self._until(lambda ln: ln == "readyok", 15)

    def _pump(self) -> None:
        for ln in self.p.stdout:
            self.q.put(ln.strip())
        self.q.put("\0eof")

    def _send(self, s: str) -> None:
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def _until(self, pred, timeout: float, lines: Optional[List[str]] = None) -> List[str]:
        lines = [] if lines is None else lines
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("uci timeout")
            ln = self.q.get(timeout=left)
            if ln == "\0eof":
                raise RuntimeError("engine exited")
            lines.append(ln)
            if pred(ln):
                return lines

    def alive(self) -> bool:
        return self.p.poll() is None

    def kill(self) -> None:
        try:
            self.p.kill()
        except OSError:
            pass

    def analyse(self, fen: str, multipv: int, movetime_ms: int = 0, depth: int = 0,
                timeout: float = 10.0) -> List[Tuple[str, int]]:
        """[(nước KIỂU TUBECLI, điểm cp theo bên đang đi)] theo thứ tự multipv."""
        if multipv != self.multipv:
            self._send(f"setoption name MultiPV value {multipv}")
            self.multipv = multipv
        self._send("position fen " + fen)
        self._send(f"go depth {depth}" if depth else f"go movetime {movetime_ms}")
        lines: List[str] = []
        try:
            self._until(lambda ln: ln.startswith("bestmove"), timeout, lines)
        except TimeoutError:
            # quá giờ: bảo engine dừng, giữ phân tích đã có
            self._send("stop")
            self._until(lambda ln: ln.startswith("bestmove"), 3, lines)
        best: dict = {}
        for ln in lines:
            if not ln.startswith("info ") or " pv " not in ln:
                continue
            m = re.search(r" multipv (\d+)", ln)
            k = int(m.group(1)) if m else 1
            sc = re.search(r" score (cp|mate) (-?\d+)", ln)
            if not sc:
                continue
            v = int(sc.group(2))
            if sc.group(1) == "mate":
                v = (MATE - abs(v)) * (1 if v > 0 else -1)
            mv = from_fsf(ln.split(" pv ", 1)[1].split()[0])
            if mv:
                best[k] = (mv, v)          # dòng sau (sâu hơn) đè dòng trước
        out = [best[k] for k in sorted(best)]
        if not out:
            bm = from_fsf(lines[-1].split()[1]) if len(lines[-1].split()) > 1 else ""
            out = [(bm, 0)] if bm else []
        return out


def analyse(fen: str, multipv: int = 1, movetime_ms: int = 0, depth: int = 0,
            timeout: float = 10.0) -> List[Tuple[str, int]]:
    """Gọi đồng bộ (chạy trong to_thread). Engine chết/treo → dựng lại một lần."""
    global _engine
    path = _state["path"]
    if not path:
        return []
    with _engine_lock:
        for attempt in range(2):
            try:
                if _engine is None or not _engine.alive():
                    _engine = _Uci(path)
                return _engine.analyse(fen, max(1, min(multipv, 10)), movetime_ms, depth, timeout)
            except Exception as e:      # noqa: BLE001
                logger.warning("xq engine lỗi (lần %d): %s", attempt + 1, e)
                if _engine is not None:
                    _engine.kill()
                _engine = None
    return []
