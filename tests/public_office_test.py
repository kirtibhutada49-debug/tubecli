# -*- coding: utf-8 -*-
"""Việc thuê «chuẩn hoá Word NĐ 30» (skill office.docx) phía MÁY: báo giá theo trang → nhận việc → giao file.

User 4/10/2026: «bạn thử cho thuê skill chỉnh word tôi test trên town». Cloud GIẢ (chặn _report_blocking), skill THẬT
(extension Office Editor phải có trên máy chạy test — data/extensions_external/office_editor), dữ liệu ghi vào thư mục
tạm. Chạy:  python tests/public_office_test.py   (exit 0 = pass)
"""
import asyncio
import base64
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from tubecli.core import public_agents, public_hire, public_office  # noqa: E402
from tubecli.core.public_agents import PublicSkillError               # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} -> {detail}")


TMP = Path(tempfile.mkdtemp(prefix="_office_hire_", dir=str(ROOT / "tests")))
public_office._root = lambda *parts: (lambda d: (os.makedirs(d, exist_ok=True), d)[1])(os.path.join(str(TMP), *parts))
public_hire._dir = lambda: (lambda d: (os.makedirs(d, exist_ok=True), d)[1])(os.path.join(str(TMP), "hire_jobs"))
REPORTS = []
public_hire._report_blocking = lambda code, body: (REPORTS.append(body), {"ok": True, "status": "delivered"})[1]
SETTINGS = {"enabled": True, "skills": [], "hire_office_on": True, "hire_office_price": 30, "hire_office_pages_max": 20,
            "hire_office_model": ""}
ENTRY = {"agent_id": "a1", "hash": "h_agent_1", "settings": SETTINGS}
public_agents.public_entries = lambda: [ENTRY]

spec = importlib.util.spec_from_file_location(
    "oe_samples", str(ROOT / "data" / "extensions_external" / "office_editor" / "tests" / "samples.py"))
samples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(samples)


async def code_of(e):
    try:
        await e
        return None
    except PublicSkillError as x:
        return x.code


async def main():
    S = samples.build(str(TMP / "src"))
    raw = open(S["bao_cao"][0], "rb").read()
    b64 = base64.b64encode(raw).decode()

    # ── báo giá ─────────────────────────────────────────────────────────────
    q = await public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "h_agent_1",
                                   "name": "Báo cáo năm.docx", "file": b64})
    check("1 báo giá: đếm trang + giá/trang + mã báo giá, nói rõ đếm thật hay ước tính",
          q.get("ok") and len(q.get("quote", "")) == 16 and q.get("pages", 0) >= 2 and q.get("price") == 30
          and "exact" in q and q.get("method") in ("libreoffice", "estimate"), q)
    check("2 file báo giá cất tạm, tên sạch", os.path.isfile(TMP / "quotes" / (q["quote"] + ".docx")))
    bad = await code_of(public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "h_agent_1",
                                             "file": base64.b64encode(b"not a zip").decode()}))
    check("3 file không phải .docx → bad_file", bad == "bad_file", bad)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("x.txt", "hi")
    bad = await code_of(public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "h_agent_1",
                                             "file": base64.b64encode(zbuf.getvalue()).decode()}))
    check("4 zip nhưng không có word/document.xml → bad_file", bad == "bad_file", bad)
    bad = await code_of(public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "khong_co", "file": b64}))
    check("5 agent không công khai → agent_not_public", bad == "agent_not_public", bad)
    SETTINGS["hire_office_on"] = False
    bad = await code_of(public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "h_agent_1", "file": b64}))
    check("6 chủ tắt skill → hire_off", bad == "hire_off", bad)
    SETTINGS["hire_office_on"] = True

    # ── nhận việc ───────────────────────────────────────────────────────────
    bad = await code_of(public_hire.receive({"skill": "office.docx", "job": "abcdefabcdef", "agent": "h_agent_1",
                                             "quote": q["quote"], "minutes": q["pages"] + 1}))
    check("7 số trang khác báo giá → quote_mismatch (khách không bị tính khác cái đã thấy)", bad == "quote_mismatch", bad)
    bad = await code_of(public_hire.receive({"skill": "office.docx", "job": "abcdefabcdef", "agent": "h_agent_1",
                                             "quote": "0" * 16, "minutes": q["pages"]}))
    check("8 mã báo giá lạ / hết hạn → quote_expired", bad == "quote_expired", bad)
    REPORTS.clear()
    r = await public_hire.receive({"skill": "office.docx", "job": "job000000001", "agent": "h_agent_1",
                                   "quote": q["quote"], "minutes": q["pages"], "price": 30,
                                   "options": {"size": "14", "toc": "auto", "number_headings": True}})
    check("9 nhận việc trả nhanh {ok, job}", r == {"ok": True, "job": "job000000001"}, r)
    again = await public_hire.receive({"skill": "office.docx", "job": "job000000001", "agent": "h_agent_1",
                                       "quote": q["quote"], "minutes": q["pages"]})
    check("10 cloud gọi lại cùng việc → không mở việc thứ hai", again.get("ok"), again)
    end = time.time() + 60
    while time.time() < end and not any(b.get("status") in ("ready", "failed") for b in REPORTS):
        await asyncio.sleep(0.2)
    ready = next((b for b in REPORTS if b.get("status") == "ready"), None)
    check("11 báo cloud: running → ready kèm file .docx + số trang đã báo giá",
          REPORTS and REPORTS[0].get("status") == "running" and ready and ready["pages"] == q["pages"]
          and ready["files"][0]["name"] == "Báo cáo năm - ND30.docx"
          and ready["files"][0]["type"].endswith("wordprocessingml.document"), REPORTS)
    f = public_hire.file_for("job000000001", 0)
    check("12 file giao qua cổng hire/file: đúng đường dẫn + MIME Word", f and os.path.isfile(f["path"])
          and f["type"] == public_office.DOCX_MIME and f["path"].endswith(" - ND30.docx"), f)
    from docx import Document
    d = Document(f["path"])
    check("13 file giao đã chuẩn hoá (A4, cỡ 14 theo khách chọn, có MỤC LỤC)",
          abs(d.sections[0].page_width.cm - 21) < .01 and abs(d.styles["Normal"].font.size.pt - 14) < .1
          and any(p.text == "MỤC LỤC" for p in d.paragraphs))
    check("14 file báo giá đã chuyển vào việc (không còn trong kho báo giá)",
          not os.path.isfile(TMP / "quotes" / (q["quote"] + ".docx")))
    q2 = await public_hire.receive({"skill": "office.docx", "step": "quote", "agent": "h_agent_1", "name": "a.docx",
                                    "file": base64.b64encode(open(S["quyet_dinh"][0], "rb").read()).decode()})
    SETTINGS["hire_office_pages_max"] = 0
    st = public_agents._hire_office_settings({}, SETTINGS)
    check("15 trần trang kẹp ≥ 1; cài đặt mặc định đúng", st["hire_office_pages_max"] == 1 and st["hire_office_on"], st)
    SETTINGS["hire_office_pages_max"] = 20
    check("16 báo giá văn bản 1 trang", q2.get("pages") == 1, q2)
    st = public_agents._hire_office_settings({"hire_office_model": None}, {"hire_office_model": "cx/gpt-5.5"})
    check("17 model null → rỗng (không thành chuỗi «None»); model bẩn bị bỏ",
          st["hire_office_model"] == ""
          and public_agents._hire_office_settings({"hire_office_model": "a b;rm"}, {})["hire_office_model"] == ""
          and public_agents._hire_office_settings({}, {"hire_office_model": "cx/gpt-5.5"})["hire_office_model"] == "cx/gpt-5.5", st)
    entry = {"agent_id": "a1", "hash": "h_agent_1", "settings": dict(SETTINGS, name="Office", skills=[])}
    on_row = public_agents._profile_row(entry)
    real = public_office.available
    public_office.available = lambda: False
    try:
        off_row = public_agents._profile_row(entry)
    finally:
        public_office.available = real
    check("18 hồ sơ đẩy cloud: bật + có Office Editor → khối office + skill office.docx; mất extension → dấu TẮT tường minh",
          on_row.get("hire_office", {}).get("on") is True and on_row["hire_office"]["price"] == 30
          and "office.docx" in on_row["skills"]
          and off_row.get("hire_office") == {"on": False} and "office.docx" not in off_row["skills"], (on_row, off_row))
    models = public_office.ai_models()
    check("19 danh sách model cho ô chọn: [{provider, models[]}] (máy không có cloud_api thì rỗng, không nổ)",
          isinstance(models, list) and all(isinstance(g.get("provider"), str) and isinstance(g.get("models"), list)
                                           and g["models"] for g in models), models[:2])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
