"""/preview/stop phải dừng ĐÚNG phiên kể cả khi Flow gửi mã theo cổng.

Run:  python tests/preview_stop_port_test.py     (exit 0 = pass)

Bối cảnh (6/10/2026): mã phiên thật là preview_<thời điểm> (từ bản 6/2026), còn Flow gửi preview_<cổng> ở
cả nút ■, nút ✕, lúc đổi hồ sơ → route luôn trả not_found, phiên sống tiếp; vòng dò 4 s của Flow thấy
hồ sơ còn chạy thì tự «xem nhờ» lại — người dùng thấy ■ «chuyển sang watch», ✕ «chỉ đóng khung».
Flow mới gửi mã thật; route này thêm đường lui theo cổng cho Flow cũ.
"""
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tubecli.extensions.browser import routes as R  # noqa: E402

failures = []
checks = 0


def check(label, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        failures.append(f"{label}: {detail}")


class FakeProc:
    def __init__(self):
        self.pid = 999999
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0 if self.terminated else None


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def run(body):
    return asyncio.run(R.stop_preview(FakeRequest(body)))


def seed():
    R._preview_processes.clear()
    a, b = FakeProc(), FakeProc()
    R._preview_processes["preview_1791258935"] = {"proc": a, "port": 55641, "profile": "browsercn1"}
    R._preview_processes["preview_1791259000"] = {"proc": b, "port": 40123, "profile": "browsercn2"}
    return a, b


# Chặn taskkill thật trên Windows: route gọi subprocess.run cho Windows — thay bằng hàm giả.
_calls = []
R.subprocess.run = lambda args, **kw: _calls.append(args)

# 1. mã thật → dừng đúng phiên đó
a, b = seed()
r = run({"session_id": "preview_1791258935"})
check("1 mã thật", r.get("status") == "stopped" and "preview_1791258935" not in R._preview_processes
      and "preview_1791259000" in R._preview_processes, str(r))

# 2. Flow cũ: preview_<cổng> → tìm theo cổng
a, b = seed()
r = run({"session_id": "preview_40123"})
check("2 preview_<cổng>", r.get("status") == "stopped" and "preview_1791259000" not in R._preview_processes
      and "preview_1791258935" in R._preview_processes, str(r))

# 3. Flow mới: mã lạ + trường port → tìm theo port
a, b = seed()
r = run({"session_id": "preview_gone", "port": 55641})
check("3 trường port", r.get("status") == "stopped" and "preview_1791258935" not in R._preview_processes, str(r))

# 4. không khớp gì → not_found, không đụng phiên nào
a, b = seed()
r = run({"session_id": "preview_12345"})
check("4 cổng không có", r.get("status") == "not_found" and len(R._preview_processes) == 2, str(r))
r = run({"session_id": "preview_1791250000"})
check("4b mốc thời gian lạ không bị hiểu là cổng", r.get("status") == "not_found" and len(R._preview_processes) == 2, str(r))
r = run({})
check("4c body rỗng", r.get("status") == "not_found" and len(R._preview_processes) == 2, str(r))

R._preview_processes.clear()
if failures:
    print("\n".join("FAIL " + f for f in failures))
    sys.exit(1)
print(f"{checks}/{checks} PASS")
