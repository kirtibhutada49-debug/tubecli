"""Máy tự gửi CPU/RAM/đĩa lên cloud (tubecli/core/metrics_push.py) — 2/10/2026.

Chạy:  python tests/metrics_push_test.py      (exit 0 = pass)

Mạng GIẢ hết (feedback-tests-mock-all-http); danh tính cloud nằm ở thư mục tạm — KHÔNG đụng
data/cloud_identity.json thật (ghi đè nó là máy mất khoá ký Town).
  1. read_stats đúng hình dạng cloud đọc; route /api/v1/system/stats dùng chung nó
  2. CPU = trung bình giữa hai lần đọc, mốc riêng (không phải lát cắt 1 giây)
  3. chưa có khoá / tắt bằng TUBECLI_METRICS=off → không gọi mạng
  4. chữ ký «metrics.<ts>.<thân>» khớp công thức cloud (lib/town.js verifySignature, domain 'metrics')
  5. nghe next_in của cloud nhưng kẹp 300–3600 s; 404/401/5xx/mất mạng → lùi đúng nhịp
"""
import collections
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ── Chặn mạng: mỗi bài tự đặt câu trả lời; không đặt thì là lỗi ─────────────────
import urllib.request  # noqa: E402

calls = []
reply = {"fn": None}


def _fake_urlopen(req, timeout=None):
    calls.append(req)
    if reply["fn"] is None:
        raise RuntimeError("network is disabled in tests")
    return reply["fn"](req)


urllib.request.urlopen = _fake_urlopen


class _Res:
    def __init__(self, body, status=200):
        self._b, self.status = body, status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def answer(obj, status=200):
    body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    reply["fn"] = lambda req: _Res(body, status)


def http_error(code):
    def fn(req):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))
    reply["fn"] = fn


from tubecli.core import cloud_identity as _ci  # noqa: E402

tmp = tempfile.mkdtemp(prefix="metrics-test-")
_ci._path = lambda: os.path.join(tmp, "cloud_identity.json")

from tubecli import __version__  # noqa: E402
from tubecli.core import metrics_push as mp  # noqa: E402
from tubecli.core.town_telemetry import CLOUD_URL  # noqa: E402

check("bài test KHÔNG đụng vào data/cloud_identity.json thật", _ci._path().startswith(tmp), _ci._path())

# ── 1. Hình dạng số liệu ───────────────────────────────────────────────────────
CLOUD_KEYS = {"cpu", "mem_pct", "disk_pct", "mem_used_mb", "mem_total_mb", "disk_used_gb", "disk_total_gb"}
s = mp.read_stats(12.345)
check("read_stats có đủ trường cloud ghi vào server_metrics", CLOUD_KEYS <= set(s), sorted(s))
check("cpu làm tròn 1 chữ số", s["cpu"] == 12.3, s["cpu"])
check("RAM/đĩa là số thật của máy (> 0)", s["mem_total_mb"] > 0 and s["disk_total_gb"] > 0, s)
src = (ROOT / "tubecli" / "api" / "server.py").read_text(encoding="utf-8")
check("route /api/v1/system/stats dùng chung read_stats",
      "return read_stats(psutil.cpu_percent(interval=None))" in src)
check("startup_event khởi động metrics_push", "metrics_push.start()" in src)

# ── 2. CPU trung bình giữa hai lần đọc ─────────────────────────────────────────
import psutil  # noqa: E402

T = collections.namedtuple("T", "user system idle iowait")
seq = [T(100, 50, 800, 50), T(400, 100, 1300, 200), T(400, 100, 1300, 200)]
orig_times, orig_pct = psutil.cpu_times, psutil.cpu_percent
psutil.cpu_times = lambda: seq.pop(0)
psutil.cpu_percent = lambda interval=None: 33.0
try:
    m = mp._CpuMeter()
    first = m.read()
    second = m.read()
    third = m.read()
finally:
    psutil.cpu_times, psutil.cpu_percent = orig_times, orig_pct
check("lần đọc đầu (chưa có mốc) đo lát 1 giây", first == 33.0, first)
# bận: 150 → 500 (iowait KHÔNG tính là bận), tổng: 1000 → 2000 ⇒ 35 %
check("lần sau = trung bình cả khoảng giữa hai lần đọc (35 %)", abs(second - 35.0) < 1e-9, second)
check("không có thời gian trôi qua → 0, không chia cho 0", third == 0.0, third)

# ── 3. Chưa có khoá / tắt → im ─────────────────────────────────────────────────
p = mp._Pusher()
p._meter.read = lambda: 10.0
calls.clear()
check("chưa có danh tính cloud → không gọi mạng, hẹn 15 phút", p.push_once() == 900 and not calls, len(calls))
_ci.save("tuan89tk", "k7m2qx", "b" * 48)
os.environ["TUBECLI_METRICS"] = "off"
check("TUBECLI_METRICS=off → không gọi mạng", p.push_once() == 900 and not calls, len(calls))
os.environ.pop("TUBECLI_METRICS")

# ── 4. Gửi đúng chỗ, đúng chữ ký ───────────────────────────────────────────────
answer({"ok": True, "recorded": True, "next_in": 900})
nxt = p.push_once()
check("gửi 1 lần, nghe next_in 900", nxt == 900 and len(calls) == 1, (nxt, len(calls)))
req = calls[-1]
h = {k.lower(): v for k, v in req.header_items()}
check("đúng URL cloud", req.full_url == CLOUD_URL + "/api/servers/metrics", req.full_url)
check("đúng mã máy", h.get("x-town-server") == "k7m2qx", h)
check("UA riêng (tunnel chặn UA mặc định của urllib)", h.get("user-agent") == "TubeCLI-Town/1.0", h.get("user-agent"))
want = hmac.new(("b" * 48).encode(), f"metrics.{h.get('x-town-ts')}.".encode() + req.data, hashlib.sha256).hexdigest()
check("chữ ký = HMAC(khoá, «metrics.<ts>.<thân>»)", h.get("x-town-sig") == want)
wrong = hmac.new(("b" * 48).encode(), f"agents.{h.get('x-town-ts')}.".encode() + req.data, hashlib.sha256).hexdigest()
check("chữ ký KHÁC miền agents (không dùng chéo được)", h.get("x-town-sig") != wrong)
body = json.loads(req.data)
check("thân có số liệu + phiên bản lõi", body.get("cpu") == 10.0 and body.get("v") == __version__, body)
check("thân CHỈ có con số tài nguyên (danh sách trắng)",
      set(body) <= CLOUD_KEYS | {"cores", "platform", "v"}, sorted(set(body) - CLOUD_KEYS))
check("ghi lại lần gửi gần nhất", p.last.get("status") == 200, p.last)

# ── 5. Nhịp gửi ────────────────────────────────────────────────────────────────
answer({"next_in": 5})
check("next_in quá nhỏ → kẹp 300 s", p.push_once() == 300)
answer({"next_in": 999999})
check("next_in quá lớn → kẹp 3600 s", p.push_once() == 3600)
answer(b"<html>tunnel error</html>")
check("thân không phải JSON → mặc định 900 s", p.push_once() == 900)
http_error(404)
check("404 (cloud không biết máy) → 6 giờ mới hỏi lại", p.push_once() == 6 * 3600)
http_error(401)
check("401 (khoá lệch) → 1 giờ", p.push_once() == 3600)
http_error(500)
check("500 → thử lại nhịp thường", p.push_once() == 900)


def _down(req):
    raise urllib.error.URLError("connection refused")


reply["fn"] = _down
check("mất mạng → nhịp thường, không nổ", p.push_once() == 900 and p.last.get("status") == 0, p.last)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
