# -*- coding: utf-8 -*-
"""`codex app-server` GIẢ cho tests/codex_gpt_test.py — nói đúng JSON-RPC stdio của Codex 0.160.

Tài khoản = nội dung CODEX_HOME/auth.json: {"tokens": {"email", "plan"}} (gói ChatGPT) hoặc
{"OPENAI_API_KEY": "sk-…"}. Hành vi đọc từ file điều khiển FAKE_CODEX_CTRL (dùng chung mọi tiến trình):
  limited  [email…]     lượt của tài khoản này thất bại usageLimitExceeded
  used     {email: %}   phần trăm đã dùng trả ở account/rateLimits/read
  approval true         lượt hỏi duyệt lệnh trước khi xong
  hang     true         lượt không bao giờ xong (test đổi tài khoản khi đang bận)
  device_email          email gán cho đăng nhập bằng mã thiết bị
Mọi yêu cầu ghi vào FAKE_CODEX_LOG (JSONL) để test đối chiếu.
"""
import json
import os
import sys
import threading
import time

HOME = os.environ.get("CODEX_HOME", ".")
CTRL = os.environ.get("FAKE_CODEX_CTRL", "")
LOG = os.environ.get("FAKE_CODEX_LOG", "")
_out = threading.Lock()
_pending = {}            # id câu hỏi gửi client → Event + kết quả
_nid = [1000]
_n = [0]
# Codex thật nói UTF-8 qua stdio; Python trên Windows mặc định đọc/ghi theo bảng mã máy (cp1252) → «—» hoá «â€”»
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

if "--version" in sys.argv:
    print("codex-cli 0.160.0")
    sys.exit(0)


def ctrl():
    try:
        with open(CTRL, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def log(**kw):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def send(msg):
    with _out:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def auth():
    try:
        with open(os.path.join(HOME, "auth.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_auth(d):
    with open(os.path.join(HOME, "auth.json"), "w", encoding="utf-8") as f:
        json.dump(d, f)


def email():
    a = auth()
    return (a.get("tokens") or {}).get("email") or ("apikey" if a.get("OPENAI_API_KEY") else "")


def refresh_token():
    """Giả làm mới token: Codex ghi lại auth.json — bridge phải chép ngược về két."""
    a = auth()
    if a.get("tokens"):
        a["tokens"]["refreshed"] = int(a["tokens"].get("refreshed") or 0) + 1
        write_auth(a)


def store_path():
    return os.path.join(HOME, "threads.json")


def load_store():
    try:
        with open(store_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_store(s):
    with open(store_path(), "w", encoding="utf-8") as f:
        json.dump(s, f)


def ask_client(method, params, timeout=20):
    _nid[0] += 1
    rid = _nid[0]
    ev = threading.Event()
    _pending[rid] = {"ev": ev, "result": None}
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    ev.wait(timeout)
    return _pending.pop(rid, {}).get("result")


def run_turn(tid, turn_id, text):
    c = ctrl()
    who = email()
    log(method="turn/run", account=who, threadId=tid, text=text)
    notify("turn/started", {"threadId": tid, "turn": {"id": turn_id, "items": [], "status": "inProgress"}})
    um = {"type": "userMessage", "id": f"{turn_id}-u", "content": [{"type": "text", "text": text}]}
    notify("item/started", {"threadId": tid, "turnId": turn_id, "item": um, "startedAtMs": 0})
    notify("item/completed", {"threadId": tid, "turnId": turn_id, "item": um, "completedAtMs": 0})
    if c.get("hang"):
        return
    st = load_store()
    if who in (c.get("limited") or []):
        turn = {"id": turn_id, "status": "failed", "items": [um],
                "error": {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}}
        st.setdefault(tid, {}).setdefault("turns", []).append(turn)
        save_store(st)
        notify("turn/completed", {"threadId": tid, "turn": turn})
        return
    if c.get("approval"):
        res = ask_client("item/commandExecution/requestApproval",
                         {"threadId": tid, "turnId": turn_id, "itemId": f"{turn_id}-c", "command": "ls -la", "startedAtMs": 0})
        log(method="approval/answer", decision=(res or {}).get("decision"))
    am = {"type": "agentMessage", "id": f"{turn_id}-a", "text": "", "phase": "final_answer"}
    notify("item/started", {"threadId": tid, "turnId": turn_id, "item": am, "startedAtMs": 0})
    for part in ("Xin ", "chào"):
        notify("item/agentMessage/delta", {"threadId": tid, "turnId": turn_id, "itemId": am["id"], "delta": part})
    am = dict(am, text="Xin chào")
    notify("item/completed", {"threadId": tid, "turnId": turn_id, "item": am, "completedAtMs": 0})
    notify("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn_id, "tokenUsage": {"total": {"totalTokens": 1234}}})
    refresh_token()
    turn = {"id": turn_id, "status": "completed", "items": [um, am]}
    st = load_store()
    st.setdefault(tid, {}).setdefault("turns", []).append(turn)
    save_store(st)
    notify("turn/completed", {"threadId": tid, "turn": turn})


def handle(msg):
    method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None and rid in _pending:                    # client trả lời câu hỏi duyệt
        _pending[rid]["result"] = msg.get("result")
        _pending[rid]["ev"].set()
        return
    if rid is None:
        return                                                 # thông báo «initialized»
    log(method=method, account=email(), params=p)
    res, err = None, None
    c = ctrl()
    if method == "initialize":
        res = {"userAgent": "fake", "codexHome": HOME}
    elif method == "account/login/start":
        t = p.get("type")
        if t == "chatgptDeviceCode":
            res = {"type": t, "loginId": "L1", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD-1234"}

            def later():
                time.sleep(0.3)
                write_auth({"tokens": {"email": c.get("device_email") or "dev@x.com", "plan": "plus", "access_token": "SECRET-TOKEN"}})
                notify("account/login/completed", {"loginId": "L1", "success": True})
            threading.Thread(target=later, daemon=True).start()
        elif t == "apiKey":
            write_auth({"OPENAI_API_KEY": p.get("apiKey")})
            res = {"type": "apiKey"}
        else:
            err = {"code": -32602, "message": "bad type"}
    elif method == "account/login/cancel":
        res = {}
    elif method == "account/read":
        a = auth()
        if a.get("tokens"):
            res = {"account": {"type": "chatgpt", "email": a["tokens"].get("email"), "planType": a["tokens"].get("plan")},
                   "requiresOpenaiAuth": True}
        elif a.get("OPENAI_API_KEY"):
            res = {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}
        else:
            res = {"account": None, "requiresOpenaiAuth": True}
    elif method == "account/rateLimits/read":
        who = email()
        if who == "apikey":
            res = {"rateLimits": {"primary": None, "secondary": None}}
        else:
            used = int((c.get("used") or {}).get(who, 5))
            lim = who in (c.get("limited") or [])
            now = int(time.time())
            res = {"rateLimits": {"primary": {"usedPercent": 100 if lim else used, "windowDurationMins": 300, "resetsAt": now + 1800},
                                  "secondary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": now + 86400},
                                  "planType": (auth().get("tokens") or {}).get("plan"),
                                  "rateLimitReachedType": "rate_limit_reached" if lim else None}}
    elif method == "thread/start":
        _n[0] += 1
        st = load_store()
        tid = f"t{int(time.time() * 1000)}{_n[0]}"
        t = {"id": tid, "cwd": p.get("cwd"), "model": p.get("model") or "gpt-5.5", "name": None, "preview": "",
             "createdAt": int(time.time()), "modelProvider": "openai", "archived": False}
        st[tid] = {"meta": t, "turns": []}
        save_store(st)
        res = {"thread": t}
        send({"jsonrpc": "2.0", "id": rid, "result": res})
        notify("thread/started", {"thread": t})
        return
    elif method == "thread/list":
        st = load_store()
        arch = bool(p.get("archived"))
        res = {"data": [v["meta"] for v in st.values() if bool(v["meta"].get("archived")) == arch], "nextCursor": None}
    elif method in ("thread/read", "thread/resume"):
        v = load_store().get(p.get("threadId"))
        if not v:
            err = {"code": -32600, "message": "no such thread"}
        else:
            res = {"thread": dict(v["meta"], turns=v["turns"] if method == "thread/read" else [])}
    elif method == "thread/name/set":
        st = load_store()
        st[p["threadId"]]["meta"]["name"] = p.get("name")
        save_store(st)
        res = {}
        send({"jsonrpc": "2.0", "id": rid, "result": res})
        notify("thread/name/updated", {"threadId": p["threadId"], "threadName": p.get("name")})
        return
    elif method in ("thread/archive", "thread/unarchive"):
        st = load_store()
        st[p["threadId"]]["meta"]["archived"] = method == "thread/archive"
        save_store(st)
        res = {}
    elif method == "thread/delete":
        st = load_store()
        st.pop(p.get("threadId"), None)
        save_store(st)
        res = {}
    elif method == "turn/start":
        _n[0] += 1
        turn_id = f"u{_n[0]}"
        text = "".join(x.get("text", "") for x in p.get("input") or [])
        res = {"turn": {"id": turn_id, "status": "inProgress", "items": []}}
        send({"jsonrpc": "2.0", "id": rid, "result": res})
        threading.Thread(target=run_turn, args=(p["threadId"], turn_id, text), daemon=True).start()
        return
    elif method == "turn/steer":
        res = {}
    elif method == "turn/interrupt":
        res = {}
        send({"jsonrpc": "2.0", "id": rid, "result": res})
        notify("turn/completed", {"threadId": p["threadId"], "turn": {"id": p["turnId"], "status": "interrupted", "items": []}})
        return
    elif method == "model/list":
        res = {"data": [{"id": "gpt-5.5", "displayName": "GPT-5.5", "defaultReasoningEffort": "medium", "hidden": False,
                         "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]},
                        {"id": "hidden-x", "displayName": "X", "hidden": True}]}
    elif method == "windowsSandbox/readiness":
        res = {"status": c.get("winsb", "notConfigured")}
    elif method == "windowsSandbox/setupStart":
        send({"jsonrpc": "2.0", "id": rid, "result": {"started": True}})
        ok = c.get("winsb_setup", "ok") == "ok"
        notify("windowsSandbox/setupCompleted", {"mode": p.get("mode"), "success": ok, "error": None if ok else "denied"})
        return
    else:
        err = {"code": -32601, "message": f"unknown method {method}"}
    out = {"jsonrpc": "2.0", "id": rid}
    if err:
        out["error"] = err
    else:
        out["result"] = res
    send(out)


for line in sys.stdin:
    line = line.strip()
    if line:
        try:
            handle(json.loads(line))
        except Exception as e:      # noqa: BLE001
            sys.stderr.write(f"fake error: {e}\n")
