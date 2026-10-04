# -*- coding: utf-8 -*-
"""Việc THUÊ «chuẩn hoá văn bản Word theo NĐ 30» (skill `office.docx`) — khách trên Agent Town gửi file .docx, máy của
agent làm bằng extension Office Editor (docx_skill.py), tính tiền THEO TRANG.

User 4/10/2026: «bộ skill office … tất cả đều có thể tính tiền theo số lượng trang được cấu hình trong public» + chốt
«số trang tính tiền = file KHÁCH GỬI, báo giá TRƯỚC» + «bạn thử cho thuê skill chỉnh word tôi test trên town».

Hợp đồng với cloud — đi qua CÙNG cổng ký sẵn POST /api/v1/public/hire (miền «hire»), phân biệt bằng `step`:
  1. step=quote  {agent, skill:"office.docx", step:"quote", name, file(base64)} → máy kiểm .docx, ĐẾM TRANG, cất file
     tạm (2 giờ) → {ok, quote, pages, method, exact}. Cloud nhân giá/trang rồi cho khách xem TRƯỚC khi giữ tiền.
  2. job thật   {job, agent, skill:"office.docx", quote, minutes(=số trang đã báo), options, price} → máy lấy file theo
     mã báo giá (số trang PHẢI khớp — khách không bị tính khác cái đã thấy), chạy nền, báo `ready` kèm file + pages.
File trả về qua GET /api/v1/public/hire/file/<việc>/<n> như mọi việc thuê (public_hire.file_for).
Số trang: có LibreOffice → đếm thật (exact); không có → ƯỚC TÍNH (exact:false, Town ghi «≈»).
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import logging
import os
import re
import secrets
import shutil
import time
import zipfile
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

logger = logging.getLogger("public_office")

FILE_MAX = 12 * 1024 * 1024          # sau giải mã — thân lệnh của cổng hire ≤ 16 MB (base64 phình 4/3)
QUOTE_TTL = 2 * 3600
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_QID_RE = re.compile(r"^[a-f0-9]{16}$")
_skill_mod = None


def _ext_dir() -> Optional[str]:
    try:
        from tubecli.core.extension_manager import extension_manager
        e = extension_manager.get("office_editor")
        if e is not None and getattr(e, "extension_dir", None):
            return e.extension_dir
    except Exception:      # noqa: BLE001
        pass
    from tubecli.config import DATA_DIR
    d = os.path.join(str(DATA_DIR), "extensions_external", "office_editor")
    return d if os.path.isdir(d) else None


def _skill():
    """docx_skill.py của extension Office Editor — nạp theo ĐƯỜNG DẪN (extension ngoài, không phải gói của lõi)."""
    global _skill_mod
    if _skill_mod is None:
        d = _ext_dir()
        p = os.path.join(d, "docx_skill.py") if d else ""
        if not p or not os.path.isfile(p):
            raise PublicSkillError("skill_unavailable", status=503)
        spec = importlib.util.spec_from_file_location("office_editor_docx_skill_hire", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _skill_mod = mod
    return _skill_mod


def available() -> bool:
    d = _ext_dir()
    return bool(d and os.path.isfile(os.path.join(d, "docx_skill.py")))


def exact_pages() -> bool:
    """Máy đếm trang THẬT được không (có LibreOffice)."""
    try:
        return bool(_skill().lo.find_soffice())
    except Exception:      # noqa: BLE001
        return False


def _root(*parts: str) -> str:
    from tubecli.config import DATA_DIR
    d = os.path.join(str(DATA_DIR), "hire_office", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _clean_name(name: str) -> str:
    base = os.path.basename(str(name or "")).strip() or "van-ban.docx"
    stem = re.sub(r"[^\w\-. ]+", "", os.path.splitext(base)[0], flags=re.U).strip()[:80] or "van-ban"
    return stem + ".docx"


def _decode_docx(b64: Any) -> bytes:
    """base64 → bytes của một .docx THẬT (zip có word/document.xml). Sai là từ chối trước khi đụng tới đĩa."""
    if not isinstance(b64, str) or not b64:
        raise PublicSkillError("bad_file", status=422)
    s = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    if len(s) > FILE_MAX * 4 // 3 + 16:
        raise PublicSkillError("file_too_large", status=413)
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:      # noqa: BLE001
        raise PublicSkillError("bad_file", status=422)
    if len(raw) > FILE_MAX:
        raise PublicSkillError("file_too_large", status=413)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if "word/document.xml" not in z.namelist():
                raise PublicSkillError("bad_file", status=422)
    except zipfile.BadZipFile:
        raise PublicSkillError("bad_file", status=422)
    return raw


def _settings(entry: Dict[str, Any]) -> Dict[str, Any]:
    st = entry.get("settings") or {}
    if not st.get("hire_office_on"):
        raise PublicSkillError("hire_off", status=409)
    if not available():
        raise PublicSkillError("skill_unavailable", status=503)
    return st


def _sweep_quotes() -> None:
    d = _root("quotes")
    now = time.time()
    for f in os.listdir(d):
        p = os.path.join(d, f)
        try:
            if now - os.path.getmtime(p) > QUOTE_TTL:
                os.remove(p)
        except OSError:
            pass


async def receive(payload: Dict[str, Any]) -> Dict[str, Any]:
    from tubecli.core import public_agents
    h = str(payload.get("agent") or "")
    entry = next((e for e in public_agents.public_entries() if e["hash"] == h), None)
    if not entry:
        raise PublicSkillError("agent_not_public", status=404)
    if str(payload.get("step") or "") == "quote":
        return await quote(payload, entry)
    return await accept(payload, entry)


async def quote(payload: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    st = _settings(entry)
    raw = _decode_docx(payload.get("file"))
    _sweep_quotes()
    qid = secrets.token_hex(8)
    d = _root("quotes")
    path = os.path.join(d, qid + ".docx")
    with open(path, "wb") as f:
        f.write(raw)
    try:
        counted = await asyncio.to_thread(_skill().lo.count_pages, path)
    except Exception as e:      # noqa: BLE001 — file mở không được bằng python-docx/LibreOffice
        logger.info("[office-hire] báo giá hỏng: %s", e)
        os.remove(path)
        raise PublicSkillError("bad_file", status=422)
    pages = max(1, int(counted.get("pages") or 1))
    pmax = int(st.get("hire_office_pages_max") or 50)
    meta = {"agent": entry["hash"], "name": _clean_name(payload.get("name")), "pages": pages,
            "method": str(counted.get("method") or "estimate"), "at": time.time()}
    with open(os.path.join(d, qid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    out = {"ok": True, "quote": qid, "pages": pages, "method": meta["method"], "exact": meta["method"] != "estimate",
           "price": int(st.get("hire_office_price") or 0), "pages_max": pmax}
    if pages > pmax:
        out["too_many"] = True            # cloud vẫn báo cho khách số trang, nhưng không cho đặt
    return out


def _options(raw: Any) -> Dict[str, Any]:
    o = raw if isinstance(raw, dict) else {}
    size = 14.0 if str(o.get("size")) == "14" else 13.0
    try:
        line = float(o.get("line") or 1.5)
    except (TypeError, ValueError):
        line = 1.5
    line = line if line in (1.0, 1.15, 1.3, 1.5) else 1.5
    toc = o.get("toc", "auto")
    toc = "auto" if toc in ("auto", None, "") else bool(toc)
    return {"size": size, "line": line, "toc": toc, "number_headings": o.get("number_headings", True) is not False,
            "fix_dates": True}


async def accept(payload: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    from tubecli.core import public_hire as H
    st = _settings(entry)
    code = str(payload.get("job") or "")
    qid = str(payload.get("quote") or "")
    if not H._CODE_RE.match(code) or not _QID_RE.match(qid):
        raise PublicSkillError("bad_request", status=400)
    if code in H._jobs:                  # cloud gọi lại (mạng chớp) — file báo giá đã chuyển vào việc rồi
        return {"ok": True, "job": code}
    qdir = _root("quotes")
    try:
        with open(os.path.join(qdir, qid + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        raise PublicSkillError("quote_expired", status=409)
    src_q = os.path.join(qdir, qid + ".docx")
    if meta.get("agent") != entry["hash"] or not os.path.isfile(src_q):
        raise PublicSkillError("quote_expired", status=409)
    try:
        held = int(payload.get("minutes") or 0)          # cột «minutes» của hire_jobs = số trang đã giữ tiền
    except (TypeError, ValueError):
        held = 0
    if held != int(meta.get("pages") or 0):
        raise PublicSkillError("quote_mismatch", status=409)
    if held > int(st.get("hire_office_pages_max") or 50):
        raise PublicSkillError("too_many_pages", status=422)
    async with H._lock:
        if code in H._jobs:              # cloud gọi lại (mạng chớp) — không mở việc thứ hai
            return {"ok": True, "job": code}
        jdir = _root("jobs", code)
        src = os.path.join(jdir, meta.get("name") or "van-ban.docx")
        shutil.move(src_q, src)
        try:
            os.remove(os.path.join(qdir, qid + ".json"))
        except OSError:
            pass
        job = {"code": code, "kind": "office", "agent_id": entry["agent_id"], "unit": "page", "pages": held,
               "price": int(payload.get("price") or 0), "src": src, "options": _options(payload.get("options")),
               "model": str(st.get("hire_office_model") or ""), "status": "accepted", "files": [], "paths": [],
               "seconds": 0, "at": time.time()}
        H._jobs[code] = job
        H._save(job)
        job["_task"] = asyncio.create_task(H._run(code))
    logger.info("[office-hire] nhận việc %s: %s, %s trang", code, os.path.basename(src), held)
    return {"ok": True, "job": code}


def _report(code: str, body: Dict[str, Any]) -> Dict[str, Any]:
    from tubecli.core import public_hire as H
    return H._report_blocking(code, {"job": code, **body})


async def run(code: str) -> None:
    """Chạy skill NĐ 30 (vài giây; có AI thì vài chục giây) rồi giao file. Hỏng → báo failed để cloud hoàn tiền."""
    from tubecli.core import public_hire as H
    job = H._jobs.get(code)
    if not job:
        return
    try:
        out = await asyncio.to_thread(_report, code, {"status": "running", "step": "format", "pct": 10})
        if out.get("closed"):
            job["status"] = "closed"
            H._save(job)
            return
        opts = dict(job.get("options") or {})
        if job.get("model"):
            opts["model"] = job["model"]
        res = await asyncio.to_thread(_skill().format_docx, job["src"], None, opts)
        path = res["output"]
        name = os.path.splitext(os.path.basename(job["src"]))[0] + " - ND30.docx"
        job["paths"] = [path]
        job["files"] = [{"name": name, "bytes": os.path.getsize(path), "type": DOCX_MIME}]
        job["result_pages"] = int(res.get("pages") or 0)
        H._save(job)
        out = await asyncio.to_thread(_report, code, {"status": "ready", "step": "done", "pct": 100,
                                                       "files": job["files"], "pages": int(job["pages"])})
        job["status"] = "delivered" if out.get("status") == "delivered" else str(out.get("status") or "reported")
        H._save(job)
        logger.info("[office-hire] %s giao xong (%s trang) → cloud nói %s", code, job["pages"], out)
    except Exception as e:      # noqa: BLE001 — lỗi nào cũng phải báo để khách được hoàn
        logger.warning("[office-hire] %s hỏng: %s", code, e)
        try:
            await asyncio.to_thread(_report, code, {"status": "failed", "err": "job_failed"})
        except Exception:      # noqa: BLE001
            pass
        job["status"] = "failed"
        H._save(job)
