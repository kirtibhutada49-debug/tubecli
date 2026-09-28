# -*- coding: utf-8 -*-
"""Skill browser.remote + các lớp chặn cho NGƯỜI LẠ điều khiển trình duyệt (28/9/2026).

Chạy: python tests/public_browser_test.py — không mạng, không mở trình duyệt thật; token
khách ghi vào thư mục tạm (không đụng guest_tokens.json của máy)."""
import asyncio
import io
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tubecli.core import auth  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pubbrowser_")
auth._guest_file = lambda: __import__("pathlib").Path(_tmp) / "guest_tokens.json"
auth._guest_cache.clear()

from tubecli.api import server  # noqa: E402
from tubecli.core import origin_guard as og  # noqa: E402
from tubecli.core import public_agents as pa  # noqa: E402
from tubecli.core import public_browser as pb  # noqa: E402
from tubecli.core.ws_auth import origin_ok  # noqa: E402
from tubecli.extensions.browser import routes as br  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


class _Proc:
    def poll(self):
        return None


def _req(method, path, body=b""):
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}, receive)


def allowed(method, path, scope, body=b""):
    return asyncio.run(server._guest_allowed(_req(method, path, body), scope))


# ── 1. Gate: khách CÔNG KHAI hẹp hơn hẳn khách workspace ──────────────────────
br._preview_processes.clear()
br._preview_processes["public_1"] = {"proc": _Proc(), "port": 41001, "profile": "shared", "isolated": True}
br._preview_processes["preview_1"] = {"proc": _Proc(), "port": 41002, "profile": "shared"}   # preview trần của chủ
PUB = {"public": True, "profiles": ["shared"], "access": "control", "upload": "media"}
check("xem khung hình preview ĐÃ CÔ LẬP của đúng hồ sơ", allowed("GET", "/api/v1/browser/preview/screenshot/41001", PUB))
check("preview TRẦN của chủ (cùng hồ sơ) → chặn", not allowed("GET", "/api/v1/browser/preview/screenshot/41002", PUB))
check("tải lên (media) vào preview cô lập", allowed("POST", "/api/v1/browser/preview/upload/41001", PUB))
check("tải lên khi chủ tắt (upload=off) → chặn",
      not allowed("POST", "/api/v1/browser/preview/upload/41001", {**PUB, "upload": "off"}))
for m, p, body in [("POST", "/api/v1/browser/preview/launch", b'{"profile":"shared"}'),
                   ("POST", "/api/v1/browser/stop", b'{"profile":"shared"}'),
                   ("POST", "/api/v1/browser/preview/stop", b'{"profile":"shared"}'),
                   ("GET", "/api/v1/browser/profiles", b""), ("GET", "/api/v1/browser/status", b""),
                   ("POST", "/api/v1/browser/preview/attach-file", b'{"profile":"shared","path":"C:/x"}'),
                   ("POST", "/api/v1/browser/preview/upload-local/41001", b'{"paths":["C:/x"]}'),
                   ("POST", "/api/v1/browser/preview/set-input/41001", b"{}"),
                   ("POST", "/api/v1/browser/preview/drive-attach/41001", b'{"file_id":"x"}'),
                   ("GET", "/api/v1/file-manager/roots", b""), ("GET", "/terminal", b"")]:
    check(f"khách công khai KHÔNG được {m} {p}", not allowed(m, p, PUB, body))
check("khách workspace (không public) vẫn tự mở browser như cũ",
      allowed("POST", "/api/v1/browser/preview/launch", {"profiles": ["shared"], "access": "control"}, b'{"profile":"shared"}'))

# ── 2. WebSocket phải qua luật Origin (terminal WS từng mở cho mọi trang web) ──
def ws(origin, host="127.0.0.1:5295", proxied=False):
    h = {"origin": origin, "host": host}
    if proxied:
        h["cf-connecting-ip"] = "1.2.3.4"
    return types.SimpleNamespace(headers=h)


check("WS không Origin (không phải trình duyệt) → qua như HTTP", origin_ok(ws("")))
check("WS từ dashboard loopback → qua", origin_ok(ws("http://127.0.0.1:5295")))
check("WS từ trang lạ (evil.com) vào loopback → CHẶN", not origin_ok(ws("https://evil.com")))
check("DNS rebinding (Origin=Host=evil) trên loopback → CHẶN", not origin_ok(ws("http://evil.com:5295", "evil.com:5295")))
check("qua tunnel, cloud.tubecreate.com → tunnel cùng site → qua",
      origin_ok(ws("https://cloud.tubecreate.com", "tuan4-13.tubecreate.com", proxied=True)))
check("qua tunnel, site khác → chặn", not origin_ok(ws("https://evil.com", "tuan4-13.tubecreate.com", proxied=True)))

# ── 3. Đăng nhập KHÁCH không được dạy máy tin Origin tuỳ ý ──────────────────
og._learned_hosts.clear()
og.remember_host("https://attacker.tubecreate.com", "victim.tubecreate.com", trust_origin=False)
check("token khách + Origin giả cùng site → KHÔNG học Origin đó",
      "attacker.tubecreate.com" not in og._learned_hosts and "victim.tubecreate.com" in og._learned_hosts)
og.remember_host("https://cloud.tubecreate.com", "victim.tubecreate.com", trust_origin=False)
check("…nhưng vẫn học cloud chính thức", "cloud.tubecreate.com" in og._learned_hosts)
og._learned_hosts.clear()

# ── 4. Lệnh WS của người lạ + URL ─────────────────────────────────────────────
check("lệnh chuột/phím qua", br._public_ws_message_ok('{"type":"mouse","action":"click","x":1,"y":2}'))
check("soi/chọn phần tử (công cụ của chủ) bị bỏ", not br._public_ws_message_ok('{"type":"pick_element","x":1,"y":1}'))
for u in ("file:///C:/Users", "chrome://settings", "http://127.0.0.1:5295/terminal", "http://localhost/",
          "http://10.0.0.8/", "http://169.254.169.254/latest", "view-source:https://a.com", "http://intranet/"):
    check(f"điều hướng tới {u} bị chặn", not br._public_ws_message_ok('{"type":"navigate","url":"%s"}' % u))
check("điều hướng https bình thường qua", br._public_ws_message_ok('{"type":"navigate","url":"https://www.google.com/"}'))

# ── 5. Tải lên của người lạ: ≤5 file, ≤25 MB, ảnh/video/PDF ────────────────────
from fastapi import HTTPException, UploadFile  # noqa: E402

pub_req = types.SimpleNamespace(state=types.SimpleNamespace(guest_scope=PUB))


def up(files):
    try:
        asyncio.run(br.api_preview_upload_files(pub_req, 41001, files))
        return 0
    except HTTPException as e:
        return e.status_code


check("file .exe bị từ chối (415)", up([UploadFile(io.BytesIO(b"MZ"), filename="x.exe")]) == 415)
check("6 file bị từ chối (400)", up([UploadFile(io.BytesIO(b"x"), filename=f"a{i}.png") for i in range(6)]) == 400)
check("file 26 MB bị cắt giữa chừng (413)",
      up([UploadFile(io.BytesIO(b"0" * (26 * 1024 * 1024)), filename="big.mp4")]) == 413)
import shutil  # noqa: E402

for n in os.listdir(br._upload_temp_dir()):
    if n.startswith(br.public_upload_prefix(41001)):
        shutil.rmtree(os.path.join(br._upload_temp_dir(), n), ignore_errors=True)

# ── 6. Cài đặt của chủ ─────────────────────────────────────────────────────────
pa._profile_names = lambda: {"shared", "khác"}
try:
    pa.normalise({"name": "Tro Ly", "enabled": True, "skills": ["browser.remote"]})
    check("bật skill mà chưa chọn hồ sơ → lỗi", False)
except ValueError as e:
    check("bật skill mà chưa chọn hồ sơ → no_browser_profile", str(e) == "no_browser_profile")
try:
    pa.normalise({"name": "Tro Ly", "enabled": True, "skills": ["browser.remote"], "browser_profile": "ma"})
    check("hồ sơ không có thật → lỗi", False)
except ValueError as e:
    check("hồ sơ không có thật → bad_browser_profile", str(e) == "bad_browser_profile")
n = pa.normalise({"name": "Tro Ly", "enabled": True, "skills": ["browser.remote"], "browser_profile": "shared",
                  "browser_minutes": 999, "browser_upload": "all"})
check("phút kẹp 5–60, upload lạ → media", n["browser_minutes"] == 60 and n["browser_upload"] == "media")
kept = pa.normalise({"name": "Tro Ly", "skills": []}, old=n)
check("client cũ không gửi trường → giữ hồ sơ đã chọn", kept["browser_profile"] == "shared")

# ── 7. Vòng đời phiên ─────────────────────────────────────────────────────────
launched, stopped = [], []


async def fake_launch(profile):
    launched.append(profile)
    port = 42000 + len(launched)
    br._preview_processes[f"public_x{port}"] = {"proc": _Proc(), "port": port, "profile": profile, "isolated": True}
    return {"ok": True, "port": port, "session_id": f"public_x{port}"}


br.launch_public_preview = fake_launch
br.stop_public_preview = lambda profile, port: stopped.append((profile, port))
ST = {"browser_profile": "shared", "browser_minutes": 10, "browser_upload": "off"}


def call(text, caller, settings=ST):
    try:
        return asyncio.run(pb.resolve(text, {"_agent_id": "A1", "_caller": caller, "_settings": settings}))
    except pa.PublicSkillError as e:
        return e.code


async def scenario():
    r1 = await pb.resolve('{"action":"start"}', {"_agent_id": "A1", "_caller": "aaaa1111", "_settings": ST})
    busy = None
    try:
        await pb.resolve('{"action":"start"}', {"_agent_id": "A1", "_caller": "bbbb2222", "_settings": ST})
    except pa.PublicSkillError as e:
        busy = e.code
    r2 = await pb.resolve('{"action":"start"}', {"_agent_id": "A1", "_caller": "aaaa1111", "_settings": ST})
    sc = auth.guest_scope_for(r1["token"])
    await pb.resolve('{"action":"stop","session":"%s"}' % r1["session"], {"_agent_id": "A1", "_caller": "bbbb2222", "_settings": ST})
    still = "A1" in pb._sessions
    await pb.resolve('{"action":"stop","session":"%s"}' % r1["session"], {"_agent_id": "A1", "_caller": "aaaa1111", "_settings": ST})
    return r1, busy, r2, sc, still


r1, busy, r2, sc, still = asyncio.run(scenario())
check("mở phiên: token khách + cổng preview cô lập + hạn 10 phút",
      r1.get("kind") == "browserlive" and r1["token"].startswith("gt_") and r1["port"] == 42001
      and 590 <= r1["expires_in"] <= 600, r1)
check("scope token: public, đúng hồ sơ, upload theo chủ, KHÔNG có thư mục/Drive",
      sc and sc["public"] and sc["profiles"] == ["shared"] and sc["upload"] == "off"
      and not sc.get("folders") and not sc.get("file_manager"), sc)
check("người thứ hai → browser_busy (mỗi lúc một người)", busy == "browser_busy")
check("cùng người mở lại → token mới, CÙNG phiên, không mở browser thứ hai",
      r2["session"] == r1["session"] and r2["token"] != r1["token"] and len(launched) == 1)
check("người khác không kết thúc được phiên của mình", still)
check("chủ phiên kết thúc → thu MỌI token của phiên + tắt browser",
      "A1" not in pb._sessions and auth.guest_scope_for(r1["token"]) is None
      and auth.guest_scope_for(r2["token"]) is None and stopped == [("shared", 42001)])
check("không có mã người gọi → login_required", call('{"action":"start"}', "") == "login_required")
check("lệnh lạ → bad_input", call('{"action":"format_c"}', "aaaa1111") == "bad_input")


async def expiry():
    s = await pb.resolve('{"action":"start"}', {"_agent_id": "A2", "_caller": "cccc3333",
                                                "_settings": {**ST, "browser_minutes": 5}})
    pb._sessions["A2"]["task"].cancel()
    pb._sessions["A2"]["task"] = asyncio.create_task(pb._expire_later("A2", s["session"], 0.05))
    await asyncio.sleep(1.5)
    return s


s = asyncio.run(expiry())
check("hết giờ → tự thu token + tắt browser", "A2" not in pb._sessions and auth.guest_scope_for(s["token"]) is None)
check("hạn cookie khách = hạn token", auth.guest_token_exp(r1["token"]) == 0)

# ── Hai lỗi chỉ lộ khi chạy thật (e2e 28/9) ──────────────────────────────────
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_bm = open(os.path.join(_root, "tubecli", "extensions", "browser", "browser_manager.js"), encoding="utf-8").read()
_iso = _bm[_bm.index("if (isolate) {\n            delete process.env"):]   # mở text mode: CRLF đã thành \n
_iso = _iso[:_iso.index("// ── 3. Launch")]
_m = re.search(r"if \(/(\^--\(proxy-bypass-list[^/]*)/", _iso)
_args = ["--no-first-run", "--proxy-bypass-list=localhost,127.0.0.1,::1", "--host-resolver-rules=MAP * 127.0.0.1",
         "--proxy-server=direct://", "--remote-debugging-port=0"]
_left = [a for a in _args if not (_m and re.match(_m.group(1), a))]
check("cô lập: bỏ cờ bypass/proxy/resolver tự đặt (Chromium lấy cờ CUỐI, đè <-loopback> của Playwright)",
      _left == ["--no-first-run", "--remote-debugging-port=0"], _left)
check("cô lập: ép loopback đi qua net_guard", "'--proxy-bypass-list=<-loopback>'" in _iso)
_rt = open(os.path.join(_root, "tubecli", "extensions", "browser", "routes.py"), encoding="utf-8").read()
_ws = _rt[_rt.index("async def ws_preview_proxy"):]
_ws = _ws[_ws.index("for task in pending:"):_ws.index("except ImportError:")]
check("preview tắt → proxy ĐÓNG socket khách (4001 khi đã thu quyền)",
      "websocket.close(code=_code" in _ws and "4001" in _ws)

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n{passed} pass, {failed} fail")
sys.exit(1 if failed else 0)
