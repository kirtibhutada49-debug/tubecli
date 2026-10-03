# -*- coding: utf-8 -*-
"""Ống MCP stdio ↔ TubeCLI cho Codex: Codex chạy file này (config.toml → [mcp_servers.tubecli]), mọi tools/list và
tools/call chuyển nguyên về POST /api/v1/codex-gpt/mcp của TubeCLI — công cụ thật nằm trong tools.py, chạy trong
tiến trình TubeCLI.

Chỉ dùng thư viện chuẩn và chạy bằng ĐƯỜNG DẪN file (không `-m tubecli…`): trên Windows TubeCLI không cài `pip -e`,
nên tiến trình con do Codex mở có thể không import được gói tubecli. Codex chạy MCP NGOÀI sandbox của nó, nên cách
này dùng được cả khi lệnh shell của Codex bị chặn mạng.

Biến môi trường: TUBECLI_CG_URL (địa chỉ /mcp), TUBECLI_CG_KEY (khoá máy này tự sinh), TUBECLI_CG_TIMEOUT (giây).
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

URL = os.environ.get("TUBECLI_CG_URL", "")
KEY = os.environ.get("TUBECLI_CG_KEY", "")
TIMEOUT = float(os.environ.get("TUBECLI_CG_TIMEOUT", "1800") or 1800)
INSTRUCTIONS = ("Tools for the TubeCLI automation server on this machine: Task Board, browser profiles, extensions "
                "and the TubeCLI API. Call tubecli_overview first. Changes may wait for the owner's approval.")

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", newline="\n")
_out = threading.Lock()
# Địa chỉ loopback: KHÔNG đi qua HTTP_PROXY của máy (urllib mặc định đọc biến môi trường proxy).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(obj):
    line = json.dumps(dict(obj, jsonrpc="2.0"), ensure_ascii=False)
    with _out:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def forward(method, params):
    req = urllib.request.Request(URL, data=json.dumps({"method": method, "params": params}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "X-Codex-GPT-Key": KEY}, method="POST")
    with _opener.open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return                                   # thông báo (initialized, cancelled…) — không trả lời
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        send({"id": mid, "result": {"protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}},
                                    "serverInfo": {"name": "tubecli", "title": "TubeCLI", "version": "1"},
                                    "instructions": INSTRUCTIONS}})
        return
    if method == "ping":
        send({"id": mid, "result": {}})
        return
    if method not in ("tools/list", "tools/call"):
        send({"id": mid, "error": {"code": -32601, "message": f"{method} is not supported"}})
        return
    try:
        out = forward(method, msg.get("params") or {})
    except urllib.error.HTTPError as e:
        out = {"error": {"code": -32000, "message": f"TubeCLI refused the call (HTTP {e.code})"}}
    except Exception as e:                       # TubeCLI tắt / chưa chạy
        out = {"error": {"code": -32000, "message": f"TubeCLI is not reachable at {URL}: {e}"}}
    if method == "tools/call" and "error" in out:
        # Lỗi của MỘT lời gọi công cụ đưa về cho model đọc được, thay vì làm hỏng lượt.
        out = {"result": {"content": [{"type": "text", "text": out["error"].get("message", "failed")}], "isError": True}}
    send(dict(out, id=mid))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            threading.Thread(target=handle, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
