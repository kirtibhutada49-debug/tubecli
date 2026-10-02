"""Máy tự gửi CPU / RAM / ổ đĩa lên cloud mỗi 15 phút — biểu đồ «Giám sát» + cảnh báo CPU qua email.

VÌ SAO (2/10/2026)
    Trước đây cloud tự đi hỏi: cron 15 phút SSH vào từng máy (qua provisioner) đọc /proc. Cách đó
      - cần cloud giữ mật khẩu SSH và một server provisioner chạy suốt,
      - bỏ sót máy chỉ nối tunnel (không IP, không SSH) — chúng không bao giờ có biểu đồ,
      - mỗi lượt chỉ đọc 20 máy đầu (`ORDER BY id LIMIT 20`): máy thứ 21 trở đi không có cảnh báo.
    Máy tự gửi thì không cần SSH, và cloud chỉ còn SSH máy CHƯA tự gửi (lõi cũ / tắt tính năng).

GỬI GÌ
    Đúng hình dạng của /api/v1/system/stats (read_stats) + phiên bản lõi. Không tên tiến trình, không
    đường dẫn, không gì về người dùng — chỉ con số tài nguyên.

KÝ
    Cùng khoá Town cloud cấp qua cloud_identity, miền chữ ký «metrics» (public_agents.sign) — chữ ký
    của đường này không dùng lại được cho đường agents/invoke. Chưa có khoá (cloud chưa đẩy danh tính)
    thì im, không gọi mạng.

TẮT
    TUBECLI_METRICS=off. Cloud sẽ quay về SSH (nếu là VPS có IP).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PUSH_PATH = "/api/servers/metrics"
PUSH_EVERY_SEC = 900          # cloud trả next_in; đây là mặc định khi chưa nghe được gì
MIN_EVERY_SEC = 300           # cloud có lỡ trả số bé cũng không biến máy thành máy spam
MAX_EVERY_SEC = 3600
FIRST_DELAY_SEC = 60          # để máy khởi động xong (và CPU khởi động không thành mẫu đầu)
GONE_BACKOFF_SEC = 6 * 3600   # 404: cloud không biết máy (đã xoá / mã đổi) — hỏi lại thưa


def _enabled() -> bool:
    return os.environ.get("TUBECLI_METRICS", "on").strip().lower() not in ("off", "0", "false", "no")


def read_stats(cpu: float) -> Dict[str, Any]:
    """CPU / RAM / ổ đĩa của CHÍNH máy này, hình dạng cloud đã dùng cho VPS (route /api/v1/system/stats
    dùng chung hàm này). `cpu` do người gọi đo, vì hai bên đo theo cửa sổ khác nhau."""
    import psutil

    mem = psutil.virtual_memory()
    # Ổ chứa TubeCLI, không phải "/" — trên Windows đó có thể là ổ D:, và cái người
    # dùng quan tâm là chỗ video/hồ sơ trình duyệt đang ăn đĩa.
    from tubecli.config import BASE_DIR
    try:
        disk = shutil.disk_usage(str(BASE_DIR))
    except OSError:
        disk = shutil.disk_usage(os.path.abspath(os.sep))
    return {
        "cpu": round(float(cpu), 1),
        "cores": psutil.cpu_count(logical=True) or 1,
        "mem_total_mb": round(mem.total / 1048576),
        "mem_used_mb": round((mem.total - mem.available) / 1048576),
        "mem_pct": round(mem.percent, 1),
        "disk_total_gb": round(disk.total / 1073741824, 1),
        "disk_used_gb": round((disk.total - disk.free) / 1073741824, 1),
        "disk_pct": round((disk.total - disk.free) / disk.total * 100, 1) if disk.total else 0,
        "platform": sys.platform,
    }


class _CpuMeter:
    """CPU TRUNG BÌNH giữa hai lần đọc, với mốc RIÊNG.

    Không dùng psutil.cpu_percent(None): bộ đếm đó dùng chung cả tiến trình, và route
    /api/v1/system/stats gọi nó mỗi 7 giây khi có người mở tab — cửa sổ 15 phút của mình sẽ bị
    cắt còn 7 giây. Trung bình 15 phút cũng đúng nghĩa hơn cho cảnh báo «CPU cao suốt 30 phút»
    so với một lát cắt 1 giây."""

    def __init__(self) -> None:
        self._last = None

    @staticmethod
    def _split(t) -> tuple:
        # Như psutil: guest đã nằm trong user (Linux) nên trừ ra khỏi tổng; iowait là chờ, không phải bận.
        total = sum(t) - getattr(t, "guest", 0) - getattr(t, "guest_nice", 0)
        busy = total - t.idle - getattr(t, "iowait", 0)
        return total, busy

    def read(self) -> float:
        import psutil

        now = psutil.cpu_times()
        prev, self._last = self._last, now
        if prev is None:
            return float(psutil.cpu_percent(interval=1.0))
        t1, b1 = self._split(prev)
        t2, b2 = self._split(now)
        if t2 - t1 <= 0:
            return 0.0
        return max(0.0, min(100.0, (b2 - b1) / (t2 - t1) * 100.0))


def _identity() -> Optional[Dict[str, str]]:
    try:
        from tubecli.core import cloud_identity

        ident = cloud_identity.load()
    except Exception:
        return None
    code, key = ident.get("server_code"), ident.get("town_key")
    return {"code": code, "key": key} if code and key else None


def _clamp(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return PUSH_EVERY_SEC
    return max(MIN_EVERY_SEC, min(MAX_EVERY_SEC, n))


class _Pusher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._meter = _CpuMeter()
        self.last: Dict[str, Any] = {}      # lần gửi gần nhất {at, status} — để soi khi cần

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="metrics-push", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            self._meter.read()              # mồi mốc CPU ⇒ mẫu đầu là trung bình FIRST_DELAY_SEC giây
        except Exception:
            pass
        delay = FIRST_DELAY_SEC
        while not self._stop.wait(delay):
            try:
                delay = self.push_once()
            except Exception as e:          # không bao giờ nổi lên trên
                logger.debug("[metrics] push failed: %s", e)
                delay = PUSH_EVERY_SEC

    def push_once(self) -> int:
        """Gửi một mẫu. Trả số giây tới lần gửi sau."""
        if not _enabled():
            return PUSH_EVERY_SEC
        ident = _identity()
        if not ident:
            return PUSH_EVERY_SEC
        from tubecli import __version__
        from tubecli.core.public_agents import sign
        from tubecli.core.town_telemetry import CLOUD_URL

        stats = read_stats(self._meter.read())
        stats["v"] = __version__
        body = json.dumps(stats, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        req = urllib.request.Request(
            CLOUD_URL + PUSH_PATH, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Town-Server": ident["code"],
                "X-Town-Ts": ts,
                "X-Town-Sig": sign(ident["key"], "metrics", ts, body),
                "User-Agent": "TubeCLI-Town/1.0",   # tunnel chặn UA mặc định của urllib
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                raw = res.read()
                self.last = {"at": int(time.time()), "status": res.status}
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = {}
                return _clamp(data.get("next_in") if isinstance(data, dict) else None)
        except urllib.error.HTTPError as e:
            self.last = {"at": int(time.time()), "status": e.code}
            if e.code == 404:
                return GONE_BACKOFF_SEC
            if e.code in (401, 503):
                # 401 = khoá lệch (cloud vừa cấp khoá mới, máy chưa nhận) · 503 = cloud chưa bật — đợi lâu hơn
                return MAX_EVERY_SEC
            return PUSH_EVERY_SEC
        except (urllib.error.URLError, OSError) as e:
            self.last = {"at": int(time.time()), "status": 0, "error": str(e)[:200]}
            return PUSH_EVERY_SEC


_pusher = _Pusher()


def start() -> None:
    """Gọi lúc khởi động server."""
    try:
        _pusher.start()
    except Exception:
        pass
