# -*- coding: utf-8 -*-
"""Codex GPT (node Flow + trang /codex-gpt) — cầu nối tới `codex app-server`, nhiều tài khoản, phiên dùng chung.

User 3/10/2026: «thêm node codex gpt (khác với codex task board) … cài vào sử dụng dòng lệnh codex như bình
thường nhưng giúp người dùng quản lý multi subscribe và quản lý các phiên chat UI/UX dễ dàng trong flow».

Chạy bằng app-server GIẢ (tests/_fake_codex_app_server.py, đúng JSON-RPC stdio của codex 0.160):
  A. tìm lệnh Codex (bản riêng / biến môi trường)
  B. đăng nhập bằng mã thiết bị, API key, nhập auth.json — khoá vào két, không lộ ra ngoài
  C. phiên: tạo, gửi, chữ chạy dần, đổi tên, lưu trữ, xoá; token làm mới được chép ngược về két
  D. hết hạn mức → tự chuyển sang tài khoản còn hạn mức (gói ChatGPT trước API key) và làm tiếp phiên
  E. đổi tay khi đang bận; duyệt lệnh qua trang; xoá tài khoản đang dùng
  F. route: trang, file tĩnh (chặn ../), WS có kiểm quyền, nằm trong danh sách nhạy cảm

Chạy:  python tests/codex_gpt_test.py   (exit 0 = pass)
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} -> {detail}")


TMP = Path(tempfile.mkdtemp(prefix="codex_gpt_test_"))
CTRL = TMP / "ctrl.json"
LOGF = TMP / "log.jsonl"
os.environ["TUBECLI_CODEX_GPT_DIR"] = str(TMP / "data")
os.environ["TUBECLI_CODEX_CMD"] = json.dumps([sys.executable, str(ROOT / "tests" / "_fake_codex_app_server.py")])
os.environ["FAKE_CODEX_CTRL"] = str(CTRL)
os.environ["FAKE_CODEX_LOG"] = str(LOGF)
os.environ["OPENAI_API_KEY"] = "sk-SHOULD-NOT-LEAK-INTO-CODEX"


def set_ctrl(**kw):
    CTRL.write_text(json.dumps(kw), encoding="utf-8")


def log_rows():
    try:
        return [json.loads(x) for x in LOGF.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


set_ctrl()

from tubecli.extensions.codex_gpt import accounts as A          # noqa: E402
from tubecli.extensions.codex_gpt import cli                    # noqa: E402
from tubecli.extensions.codex_gpt import service as SV          # noqa: E402
from tubecli.extensions.codex_gpt.service import GptError       # noqa: E402


async def wait_for(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(step)
    return pred()


async def collect(q, pred, timeout=10.0):
    """Đọc hàng sự kiện tới khi pred(msg) đúng; trả mọi tin đã đọc."""
    got = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = await asyncio.wait_for(q.get(), max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            break
        got.append(m)
        if pred(m):
            break
    return got


async def err_code(coro):
    try:
        await coro
        return "ok"
    except GptError as e:
        return e.code


async def main():
    svc = SV.CodexGptService()

    # ── A. lệnh Codex ────────────────────────────────────────────────────────
    check("A1 lệnh lấy từ TUBECLI_CODEX_CMD", cli.command() == json.loads(os.environ["TUBECLI_CODEX_CMD"]) and cli.source() == "env")
    saved = os.environ.pop("TUBECLI_CODEX_CMD")
    js = cli.npm_prefix() / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text("// giả", encoding="utf-8")
    if shutil.which("node"):
        c = cli.command()
        check("A2 có bản riêng của TubeCLI → node <…/codex.js> (không cần sudo, không đụng bản toàn cục)",
              c and c[-1] == str(js) and cli.source() == "private", c)
    shutil.rmtree(cli.npm_prefix(), ignore_errors=True)
    os.environ["TUBECLI_CODEX_CMD"] = saved
    st = await svc.status()
    check("A3 status: đã cài, chưa tài khoản, phiên bản đọc từ --version", st["installed"] and not st["accounts"] and st["version"] == "0.160.0", st)

    # ── B. đăng nhập ─────────────────────────────────────────────────────────
    q = svc.subscribe()
    lg = await svc.start_login("chatgpt", "Plus công việc")
    check("B1 mã thiết bị: trả link + mã, đang chờ", lg["state"] == "pending" and lg["code"] == "ABCD-1234" and lg["url"].startswith("https://"), lg)
    done = await wait_for(lambda: svc.login_public(lg["id"])["state"] in ("done", "error"))
    lp = svc.login_public(lg["id"])
    acc_a = (lp.get("account") or {})
    check("B2 người dùng đồng ý → tài khoản vào két: email, gói, hạn mức", done and lp["state"] == "done"
          and acc_a.get("email") == "dev@x.com" and acc_a.get("plan") == "plus" and (acc_a.get("limits") or {}).get("primary"), lp)
    check("B3 tài khoản đầu tiên thành tài khoản đang dùng", svc.active_id() == acc_a.get("id"))
    check("B4 thư mục đăng nhập tạm đã dọn", not any((TMP / "data" / "tmp").glob("login-*")))
    check("B5 khoá nằm trong két (auth.json riêng)", A.auth_path(acc_a["id"]).is_file()
          and b"SECRET-TOKEN" in A.read_auth(acc_a["id"]))
    check("B6 API key sai dạng → bad_key", await err_code(svc.start_login("apiKey", "", api_key="abc")) == "bad_key")
    lk = await svc.start_login("apiKey", "", api_key="sk-test-0123456789abcdefghijklmnop")
    check("B7 API key → xong ngay, tên mặc định theo 4 ký tự cuối", lk["state"] == "done" and lk["account"]["kind"] == "apiKey"
          and lk["account"]["label"] == "API key …mnop", lk)
    check("B8 auth.json hỏng → bad_auth", await err_code(svc.start_login("import", "", auth_json="{không phải json")) == "bad_auth"
          and await err_code(svc.start_login("import", "", auth_json='{"x": 1}')) == "bad_auth")
    li = await svc.start_login("import", "Pro nhà", auth_json=json.dumps({"tokens": {"email": "b@x.com", "plan": "pro", "access_token": "SECRET-B"}}))
    check("B9 nhập auth.json của máy khác → tài khoản Pro", li["state"] == "done" and li["account"]["plan"] == "pro", li)
    n_before = len(A.all_accounts())
    li2 = await svc.start_login("import", "", auth_json=json.dumps({"tokens": {"email": "b@x.com", "plan": "pro", "access_token": "SECRET-B2"}}))
    check("B10 cùng email đăng nhập lại → thay khoá, KHÔNG thêm tài khoản", len(A.all_accounts()) == n_before
          and li2["account"]["id"] == li["account"]["id"] and b"SECRET-B2" in A.read_auth(li["account"]["id"]))
    acc_b, acc_k = li["account"], lk["account"]
    blob = json.dumps(await svc.status(), ensure_ascii=False) + json.dumps(svc.accounts_public())
    check("B11 status / danh sách tài khoản KHÔNG lộ token / API key", "SECRET" not in blob and "sk-test" not in blob and "access_token" not in blob)

    # ── C. phiên ─────────────────────────────────────────────────────────────
    t = await svc.start_thread()
    from tubecli.config import BASE_DIR
    check("C1 phiên mới: thư mục làm việc mặc định = GỐC TubeCLI",
          t["id"] and t["cwd"] == str(BASE_DIR), t)
    check("C1b gốc TubeCLI nằm trong vùng Codex được ghi (nếu không, _cwd_allowed chặn phiên mới)",
          str(BASE_DIR) in svc.writable_roots(), svc.writable_roots())
    r = await svc.send(t["id"], "chào Codex")
    got = await collect(q, lambda m: m.get("type") == "event" and m["method"] == "turn/completed")
    meths = [m.get("method") for m in got if m.get("type") == "event"]
    deltas = "".join(m["params"].get("delta", "") for m in got if m.get("method") == "item/agentMessage/delta")
    check("C2 gửi → lượt chạy, chữ chạy dần, xong", r["turnId"] and "turn/started" in meths and deltas == "Xin chào"
          and meths[-1] == "turn/completed", meths)
    check("C3 xong lượt thì không còn trong «đang chạy»", t["id"] not in svc.active_turns)
    rows = log_rows()
    check("C4 OPENAI_API_KEY của TubeCLI không lọt sang Codex (lượt chạy bằng tài khoản đang chọn)",
          any(x.get("method") == "turn/run" and x.get("account") == "dev@x.com" for x in rows), rows[-3:])
    check("C5 token Codex làm mới → chép ngược về két", b'"refreshed": 1' in A.read_auth(acc_a["id"]) or b'"refreshed":1' in A.read_auth(acc_a["id"]),
          A.read_auth(acc_a["id"])[:200])
    lst = await svc.list_threads()
    check("C6 danh sách phiên", any(x["id"] == t["id"] for x in lst["threads"]), lst)
    await svc.thread_call("thread/name/set", t["id"], name="Sửa test")
    lst = await svc.list_threads()
    check("C7 đổi tên phiên", next(x for x in lst["threads"] if x["id"] == t["id"])["name"] == "Sửa test")
    rd = await svc.read_thread(t["id"])
    check("C8 đọc phiên có các lượt", len(rd["thread"].get("turns") or []) == 1 and not rd["running"], rd)
    t2 = await svc.start_thread()
    await svc.thread_call("thread/archive", t2["id"])
    check("C9 lưu trữ → khỏi danh sách thường, vào danh sách lưu trữ",
          not any(x["id"] == t2["id"] for x in (await svc.list_threads())["threads"])
          and any(x["id"] == t2["id"] for x in (await svc.list_threads(archived=True))["threads"]))
    await svc.thread_call("thread/delete", t2["id"])
    check("C10 xoá phiên", not any(x["id"] == t2["id"] for x in (await svc.list_threads(archived=True))["threads"]))
    ms = await svc.models()
    check("C11 model: bỏ model ẩn, có mức suy luận", [m["id"] for m in ms] == ["gpt-5.5"] and ms[0]["efforts"] == ["low", "medium", "high"], ms)
    check("C12 thư mục không có → cwd_missing; đường tương đối → bad_cwd",
          await err_code(svc.start_thread(cwd=str(TMP / "khong-co"))) == "cwd_missing"
          and await err_code(svc.start_thread(cwd="relative/dir")) == "bad_cwd")

    # ── D. hết hạn mức → tự chuyển ───────────────────────────────────────────
    set_ctrl(limited=["dev@x.com"], used={"b@x.com": 30})
    while not q.empty():
        q.get_nowait()
    await svc.send(t["id"], "làm tiếp việc lớn")
    got = await collect(q, lambda m: m.get("type") == "switched", timeout=15)
    sw = next((m for m in got if m.get("type") == "switched"), None)
    check("D1 hết hạn mức → chuyển sang gói ChatGPT còn hạn mức (KHÔNG phải API key)", sw and sw["to"] == acc_b["id"], got[-4:])
    got = await collect(q, lambda m: m.get("type") == "event" and m["method"] == "turn/completed"
                        and m["params"]["turn"]["status"] == "completed", timeout=15)
    rows = log_rows()
    cont = [x for x in rows if x.get("method") == "turn/run" and x.get("account") == "b@x.com"]
    check("D2 phiên mở lại trên app-server mới và gửi lời «làm tiếp» bằng tài khoản mới",
          cont and cont[-1]["text"] == SV.CONTINUE_TEXT and any(x.get("method") == "thread/resume" and x.get("account") == "b@x.com" for x in rows),
          rows[-4:])
    check("D3 tài khoản hết hạn mức bị đánh dấu tới giờ đặt lại", int((A.get(acc_a["id"]) or {}).get("limited_until") or 0) > time.time())
    check("D4 tài khoản đang dùng giờ là tài khoản mới", svc.active_id() == acc_b["id"])
    # mọi gói đều hết → rơi xuống API key; hết luôn API key thì báo
    set_ctrl(limited=["b@x.com", "apikey"])
    while not q.empty():
        q.get_nowait()
    await svc.send(t["id"], "lần nữa")
    got = await collect(q, lambda m: m.get("type") == "limit", timeout=20)
    sws = [m for m in got if m.get("type") == "switched"]
    lim = next((m for m in got if m.get("type") == "limit"), None)
    check("D5 gói cuối hết → dùng API key; API key cũng hết → báo, không vòng lặp vô hạn",
          [m["to"] for m in sws] == [acc_k["id"]] and lim and lim["switched"] is False, [m.get("type") for m in got])
    # tắt «tự chuyển» thì chỉ báo
    set_ctrl()
    A.update(acc_a["id"], limited_until=0)
    A.update(acc_b["id"], limited_until=0)
    await svc.switch(acc_a["id"], force=True)
    svc.update_settings({"auto_switch": False})
    set_ctrl(limited=["dev@x.com"])
    while not q.empty():
        q.get_nowait()
    await svc.send(t["id"], "không tự chuyển")
    got = await collect(q, lambda m: m.get("type") == "limit", timeout=10)
    check("D6 tắt «tự chuyển» → chỉ báo hết hạn mức, giữ nguyên tài khoản",
          any(m.get("type") == "limit" and m.get("auto") is False for m in got)
          and not any(m.get("type") == "switched" for m in got) and svc.active_id() == acc_a["id"])
    svc.update_settings({"auto_switch": True})
    set_ctrl()
    A.update(acc_a["id"], limited_until=0)

    # ── E. đổi tay / duyệt lệnh / xoá tài khoản ──────────────────────────────
    set_ctrl(hang=True)
    await svc.send(t["id"], "việc treo")
    await wait_for(lambda: t["id"] in svc.active_turns)
    check("E1 đang có lượt chạy → đổi tài khoản bị chặn (busy)", await err_code(svc.switch(acc_b["id"])) == "busy")
    while not q.empty():
        q.get_nowait()
    r = await svc.switch(acc_b["id"], force=True)
    got = await collect(q, lambda m: m.get("type") == "account")
    ev = next((m for m in got if m.get("type") == "account"), {})
    check("E2 ép đổi → lượt đang chạy bị dừng, báo cho trang", r["changed"] and t["id"] in (ev.get("interrupted") or []), ev)
    set_ctrl(approval=True)
    svc.update_settings({"approval": "on-request"})
    while not q.empty():
        q.get_nowait()
    await svc.send(t["id"], "liệt kê file")
    got = await collect(q, lambda m: m.get("type") == "approval", timeout=10)
    ap = next((m for m in got if m.get("type") == "approval"), None)
    check("E3 Codex hỏi duyệt lệnh → chuyển tới trang (kèm lệnh)", ap and ap["params"]["command"] == "ls -la", got[-2:])
    check("E4 trang mở sau vẫn thấy câu hỏi đang chờ", any(a["key"] == ap["key"] for a in svc.pending_approvals()))
    ok = await svc.answer_approval(ap["key"], "accept")
    await collect(q, lambda m: m.get("type") == "event" and m["method"] == "turn/completed", timeout=10)
    check("E5 bấm «Cho phép» → Codex nhận accept, lượt chạy xong",
          ok and any(x.get("method") == "approval/answer" and x.get("decision") == "accept" for x in log_rows()))
    check("E6 trả lời lần hai cùng câu hỏi → bỏ qua", await svc.answer_approval(ap["key"], "decline") is False)
    sent = []

    async def fake_respond(rid, result=None, error=None):
        sent.append(result)
    real = svc.bridge.respond
    svc.bridge.respond = fake_respond
    svc.approvals["x-1"] = {"rid": 1, "method": "execCommandApproval", "params": {}, "bridge": svc.bridge, "at": 0}
    await svc.answer_approval("x-1", "acceptForSession")
    svc.bridge.respond = real
    check("E7 giao thức cũ (execCommandApproval) → đổi sang approved_for_session", sent == [{"decision": "approved_for_session"}], sent)

    # ── E8 «Cho phép, không hỏi lại trong phiên này» (user 5/10/2026) ─────────
    # acceptForSession của Codex chỉ nhớ ĐÚNG lệnh đó, nên Codex dò 20 file là hỏi 20 lần.
    # Cờ này nằm ở TubeCLI, theo PHIÊN CHAT, và chết theo app-server.
    sent2 = []

    async def fake_respond2(rid, result=None, error=None):
        sent2.append(result)
    real2 = svc.bridge.respond
    svc.bridge.respond = fake_respond2
    tid = t["id"]
    svc.approvals["y-1"] = {"rid": 11, "method": "execCommandApproval",
                            "params": {"threadId": tid, "command": "ls"}, "bridge": svc.bridge, "at": 0}
    await svc.answer_approval("y-1", "acceptAlways")
    check("E8 acceptAlways → nhận ngay (giao thức cũ: approved)",
          sent2 == [{"decision": "approved"}], sent2)
    check("E8b …và ghi sổ «không hỏi lại» cho ĐÚNG phiên đó", tid in svc.no_ask, svc.no_ask)
    # Yêu cầu duyệt kế tiếp của phiên đó: tự nhận, KHÔNG bày thẻ hỏi nữa
    while not q.empty():
        q.get_nowait()
    sent2.clear()
    await svc._on_request({"method": "execCommandApproval", "id": 12,
                           "params": {"threadId": tid, "command": "rg abc"}})
    auto = await collect(q, lambda m: m.get("type") == "approval_auto", timeout=5)
    check("E8c yêu cầu sau trong phiên đó → tự nhận, không hỏi lại",
          sent2 == [{"decision": "approved"}] and not svc.approvals.get("%s-12" % svc._gen), sent2)
    check("E8d …nhưng VẪN báo cho trang biết đã tự nhận cái gì",
          any(m.get("type") == "approval_auto" and (m.get("params") or {}).get("command") == "rg abc" for m in auto),
          auto[-2:])
    # Phiên KHÁC không ăn theo lòng tin đó
    sent2.clear()
    await svc._on_request({"method": "execCommandApproval", "id": 13,
                           "params": {"threadId": "phien-khac", "command": "rm -rf /"}})
    check("E8e phiên KHÁC vẫn phải hỏi (lòng tin theo từng phiên)",
          sent2 == [] and bool(svc.approvals.get("%s-13" % svc._gen)), sent2)
    svc.approvals.pop("%s-13" % svc._gen, None)
    # Siết lại chế độ duyệt ⇒ quên mọi «không hỏi lại» đã cho
    svc.update_settings({"approval": "untrusted"})
    check("E8f đổi chế độ duyệt → bỏ mọi «không hỏi lại» đã cho trước đó", not svc.no_ask, svc.no_ask)
    svc.bridge.respond = real2
    set_ctrl()
    svc.update_settings({"approval": "never"})
    check("E8 cài đặt sai → bad_setting", await err_code(asyncio.sleep(0, result=None)) == "ok"
          and _raises(lambda: svc.update_settings({"approval": "yolo"})) == "bad_setting"
          and _raises(lambda: svc.update_settings({"sandbox": "chroot"})) == "bad_setting")
    await svc.remove_account(svc.active_id())
    check("E9 xoá tài khoản đang dùng → tự chuyển sang tài khoản khác", svc.active_id() in (acc_a["id"], acc_k["id"]) and A.get(acc_b["id"]) is None)
    for a in A.all_accounts():
        await svc.remove_account(a["id"])
    check("E10 xoá hết → không còn tài khoản đang dùng, home không giữ khoá",
          svc.active_id() is None and not (svc.home / "auth.json").exists())
    check("E11 hết tài khoản → gửi bị chặn no_account", await err_code(svc.send(t["id"], "x")) == "no_account")
    await svc._stop_bridge()
    svc.unsubscribe(q)


def _raises(fn):
    try:
        fn()
        return "ok"
    except GptError as e:
        return e.code


try:
    asyncio.run(main())
except Exception as e:      # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("chạy hết kịch bản không ném lỗi", False, repr(e))

# ── F. route ───────────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tubecli.extensions.codex_gpt import routes as R
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app)
    p = c.get("/codex-gpt")
    check("F1 trang /codex-gpt: nạp app.js/app.css kèm phiên bản, không đệm HTML",
          p.status_code == 200 and "/codex-gpt-static/app.js?v=" in p.text and "__ASSET_VER__" not in p.text
          and "no-store" in p.headers.get("cache-control", ""))
    check("F2 file tĩnh phục vụ được", c.get("/codex-gpt-static/app.css?v=1").status_code == 200)
    check("F3 chặn ../ ra ngoài thư mục tĩnh", c.get("/codex-gpt-static/..%2Froutes.py").status_code == 404
          and c.get("/codex-gpt-static/../routes.py").status_code == 404)
    s = c.get("/api/v1/codex-gpt/status").json()
    check("F4 GET status qua route", s.get("ok") and "accounts" in s and s.get("installed") is True, s)
    bad = c.post("/api/v1/codex-gpt/accounts/login", json={"kind": "apiKey", "api_key": "x"})
    check("F5 lỗi trả mã ổn định cho trang dịch", bad.status_code == 400 and bad.json().get("code") == "bad_key", bad.text)
except ImportError as e:
    print("[skip] FastAPI TestClient:", e)

src_routes = (ROOT / "tubecli" / "extensions" / "codex_gpt" / "routes.py").read_text(encoding="utf-8")
ws_body = src_routes[src_routes.index("async def ws("):]
check("F6 WebSocket kiểm quyền (cookie + Origin) TRƯỚC khi accept",
      ws_body.index("reject_unless_allowed(websocket)") < ws_body.index("websocket.accept()"))
server = (ROOT / "tubecli" / "api" / "server.py").read_text(encoding="utf-8")
check("F7 gắn vào server + /api/v1/codex-gpt nằm trong danh sách nhạy cảm (người được chia sẻ nhóm không dùng được)",
      "codex_gpt.routes import router" in server and '"/api/v1/codex-gpt"' in server[server.index("_SENSITIVE = ("):server.index("_SENSITIVE = (") + 400])
loc = ROOT / "tubecli" / "extensions" / "codex_gpt" / "locales"
en = json.loads((loc / "en.json").read_text(encoding="utf-8"))
js = (ROOT / "tubecli" / "extensions" / "codex_gpt" / "static" / "app.js").read_text(encoding="utf-8")
import re  # noqa: E402
used = {k for k in re.findall(r"'(cg\.[\w.]+)'", js) if not k.endswith(".") and not k.endswith("_")}
missing_en = sorted(k for k in used if k not in en)
check("F8 mọi khoá dùng trong app.js có trong en.json", not missing_en, missing_en[:8])
bad_lang = []
for lang in ("vi", "es", "ja", "ko", "ru", "tr", "zh", "zh-TW"):
    f = loc / f"{lang}.json"
    if not f.is_file():
        bad_lang.append(f"{lang}: missing")
        continue
    d = json.loads(f.read_text(encoding="utf-8"))
    miss = [k for k in en if k not in d]
    ph = [k for k in en if k in d and sorted(re.findall(r"\{\w+\}", en[k])) != sorted(re.findall(r"\{\w+\}", d[k]))]
    if miss or ph:
        bad_lang.append(f"{lang}: thiếu {miss[:3]} chỗ giữ lệch {ph[:3]}")
check("F9 đủ 9 ngôn ngữ, chỗ giữ {…} khớp tiếng Anh", not bad_lang, bad_lang)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{PASS} pass, {FAIL} fail")
sys.exit(1 if FAIL else 0)
