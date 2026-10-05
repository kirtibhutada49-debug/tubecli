# -*- coding: utf-8 -*-
"""Trần lượt/ngày của agent công khai KHÔNG bị ăn bởi lượt «xem còn chỗ» (info) và «trả phiên» (stop) của
browser.remote.

User 5/10/2026: Town báo «This agent has reached its limit for today» trong khi thanh lượt hiện 13/100 — cloud gọi info
với meter=false (không đếm), máy thì đếm cả → khung thuê mở lâu là cạn 100 lượt; hết lượt thì khách còn không dừng
được phiên đang trả tiền. Skill giả, không mở trình duyệt thật, không ghi file thật.
Chạy:  python tests/public_browser_quota_test.py      (exit 0 = pass)
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from tubecli.core import public_agents as P  # noqa: E402
from tubecli.core import town_telemetry       # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} -> {detail}")


CALLS = []
REPORTS = []


async def fake_browser(text, opts=None):
    CALLS.append(json.loads(text).get("action"))
    return {"kind": "browserinfo"}


P.PUBLIC_SKILLS["browser.remote"] = P.PublicSkill("browser.remote", "browser", fake_browser, 2000)
H = "b" * 16
ST = {"enabled": True, "skills": ["browser.remote"], "daily_cap": 3, "visibility": "public"}
P.public_entries = lambda: [{"agent_id": "agB", "hash": H, "settings": ST}]
P.is_tired = lambda st, load: False
town_telemetry.report = lambda *a, **k: REPORTS.append(a)
P._gate = P._Gate()


async def call(action):
    try:
        await P.invoke({"agent": H, "skill": "browser.remote", "input": json.dumps({"action": action})})
        return "ok"
    except P.PublicSkillError as e:
        return e.code


async def main():
    for _ in range(25):
        await call("info")
    check("1 hỏi «còn chỗ» 25 lần (khung thuê mở lâu) KHÔNG ăn lượt ngày (trần 3)",
          P.usage("agB")["used"] == 0 and CALLS.count("info") == 25, P.usage("agB"))
    check("2 lượt info không báo lên bản đồ Town (không phồng số lượt chạy)", REPORTS == [], REPORTS[:3])
    r = [await call("start") for _ in range(4)]
    check("3 mở phiên thật vẫn tính lượt — tới trần 3 thì «daily_cap»", r == ["ok", "ok", "ok", "daily_cap"]
          and P.usage("agB")["used"] == 3, r)
    check("4 hết lượt rồi vẫn hỏi được còn chỗ", await call("info") == "ok")
    check("5 hết lượt rồi khách VẪN trả phiên sớm được (không bị tính đủ giờ)", await call("stop") == "ok"
          and CALLS[-1] == "stop")
    P.is_tired = lambda st, load: True
    check("6 máy «mệt»: không nhận phiên mới nhưng vẫn cho trả phiên + xem chỗ",
          await call("start") == "tired" and await call("stop") == "ok" and await call("info") == "ok")
    check("7 thân không phải JSON → coi là lượt thường (không lách được trần)", P._browser_action("{bad") == ""
          and P._browser_action('["info"]') == "" and P._browser_action('{"action":"info"}') == "info")


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
