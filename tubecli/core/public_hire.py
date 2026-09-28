# -*- coding: utf-8 -*-
"""Nhận VIỆC THUÊ từ Town (Chợ mẫu): khách trả xu, agent làm VIDEO TỪ MẪU của chính máy này.

Hợp đồng với cloud (lib/hire.js — cloud giữ tiền trước, trả sau khi tự kiểm file):
  1. Cloud POST /api/v1/public/hire (ký HMAC miền «hire», cùng nonce/cửa sổ với invoke):
     {job, agent, skill, brief, price, unit, minutes, caller, preset, template{...}}.
     Máy chỉ cần NHẬN (trả 2xx nhanh) — video mất hàng chục phút.
  2. Máy chạy việc bằng CHÍNH dây chuyền content_video (một task Codex trên bảng của chủ,
     lane video — chủ nhìn thấy việc thuê như mọi việc khác), báo tiến độ về
     POST {cloud}/api/town/hire/report (ký miền «hire-report» bằng town_key).
  3. Xong: báo `ready` kèm danh sách file + SỐ GIÂY video (thuê theo phút trả theo phút
     thực); cloud tự HEAD file trên máy rồi mới trả tiền. File đi qua
     GET /api/v1/public/hire/file/<việc>/<n> (ký miền «hire-file» trên CHÍNH đường dẫn).

MẪU KHÔNG TẢI TỪ CLOUD (user chốt 28/9/2026): mẫu là đồ nghề của agent — chủ đã cài trên
máy và khai trong hire_presets; cloud gửi TÊN preset, thiếu thì từ chối ngay lúc nhận.

Huỷ: cloud hoàn tiền cho khách rồi trả {closed:true} ở lượt báo cáo sau — máy NGỪNG báo và
đóng sổ việc, nhưng KHÔNG giết task Codex giữa chừng (giết bộ dựng dở là để lại tiến trình
mồ côi — xem tubecli-studio-store-race); chủ muốn dừng hẳn thì bấm huỷ trên bảng."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

logger = logging.getLogger("public_hire")

_CODE_RE = re.compile(r"^[a-z0-9]{12}$")
_jobs: Dict[str, Dict[str, Any]] = {}
_lock = asyncio.Lock()

POLL_SEC = 15
REPORT_MIN_GAP = 20            # đừng dội cloud: chỉ báo khi ĐỔI bước, tối đa ~3 lượt/phút
MAX_BRIEF = 4000
# Bước của content_video → phần trăm cho khách xem (thang thô, đủ để biết còn sống).
_STEP_PCT = {"capabilities": 5, "script": 15, "studio": 30, "images": 45, "tts": 60,
             "render": 80, "thumbnail": 92, "publish": 95, "drive": 95}


def _dir() -> str:
    from tubecli.config import DATA_DIR
    d = os.path.join(str(DATA_DIR), "hire_jobs")
    os.makedirs(d, exist_ok=True)
    return d


def _save(job: Dict[str, Any]) -> None:
    """Sổ việc trên đĩa: restart giữa chừng vẫn phục vụ được file đã giao.

    CHỈ ghi trường không bắt đầu bằng «_»: receive() treo `job["_task"]` (asyncio.Task —
    json.dump nổ TypeError) vào job. Bug 28/9: _save chỉ bắt OSError nên TypeError xuyên
    lên _run → mọi việc bị báo «failed» NGAY SAU khi tạo task thành công — khách được
    hoàn tiền trong khi video vẫn âm thầm dựng (task mồ côi trên bảng của chủ). Bắt
    Exception luôn: sổ hỏng chỉ được phép làm mất bản ghi, không được giết việc."""
    try:
        p = os.path.join(_dir(), f"{job['code']}.json")
        data = {k: v for k, v in job.items() if not str(k).startswith("_")}
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except Exception as e:      # noqa: BLE001
        logger.warning("[hire] không ghi được sổ việc %s: %s", job.get("code"), e)


def job_record(code: str) -> Optional[Dict[str, Any]]:
    code = str(code or "")
    if not _CODE_RE.match(code):
        return None
    j = _jobs.get(code)
    if j:
        return j
    try:
        with open(os.path.join(_dir(), f"{code}.json"), encoding="utf-8") as f:
            j = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(j, dict) and j.get("code") == code:
        _jobs[code] = j
        return j
    return None


def _report_blocking(code: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/town/hire/report — ký «hire-report.<ts>.<body>» bằng town_key của máy."""
    from tubecli.core import public_agents
    from tubecli.core.town_telemetry import CLOUD_URL

    ident = public_agents._identity()
    if not ident:
        return {"error": "not_configured"}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    req = urllib.request.Request(
        CLOUD_URL + "/api/town/hire/report", data=raw, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Town-Server": ident["code"], "X-Town-Ts": ts,
                 "X-Town-Sig": public_agents.sign(ident["key"], "hire-report", ts, raw),
                 "User-Agent": "TubeCLI-Town/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8", "replace")) or {}
    except urllib.error.HTTPError as e:      # 4xx của cloud vẫn mang JSON đáng đọc
        try:
            return json.loads(e.read().decode("utf-8", "replace")) or {}
        except Exception:      # noqa: BLE001
            return {"error": f"http_{e.code}"}
    except Exception as e:      # noqa: BLE001 — mạng chớp: lượt sau báo lại
        return {"error": str(e)[:120]}


async def _report(job: Dict[str, Any], status: str, step: str = "", pct: int = 0,
                  files: Optional[list] = None, seconds: int = 0, err: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {"job": job["code"], "status": status, "step": step[:48],
                            "pct": max(0, min(100, int(pct)))}
    if files is not None:
        body["files"] = files
    if seconds:
        body["seconds"] = int(seconds)
    if err:
        body["err"] = err[:40]
    out = await asyncio.to_thread(_report_blocking, job["code"], body)
    if out.get("closed"):
        # Việc đã đóng phía cloud (khách huỷ / hết hạn / đã chốt) — ngừng theo, đóng sổ.
        job["status"] = "closed"
        _save(job)
    return out


def _studio_preset_exists(preset: str) -> bool:
    from tubecli.extensions.content_video.pipeline import _base_url
    try:
        req = urllib.request.Request(_base_url() + "/api/v1/studio/presets")
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8", "replace"))
        rows = data.get("presets") if isinstance(data, dict) else data
        names = set(rows.keys()) if isinstance(rows, dict) else {p.get("name") for p in rows or []}
        return preset in names
    except Exception:      # noqa: BLE001 — Studio chưa dậy: cứ nhận, pipeline sẽ nói rõ
        return True


def _hire_settings(entry: Dict[str, Any]) -> Dict[str, Any]:
    st = entry.get("settings") or {}
    return {"on": bool(st.get("hire_on")), "presets": [str(x) for x in (st.get("hire_presets") or [])]}


async def receive(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Cloud gọi nhận việc. Trả nhanh {ok: True}; lỗi ném PublicSkillError (route trả mã)."""
    from tubecli.core import public_agents

    if not isinstance(payload, dict):
        raise PublicSkillError("bad_request", status=400)
    code = str(payload.get("job") or "")
    h = str(payload.get("agent") or "")
    preset = " ".join(str(payload.get("preset") or "").split())[:80]
    brief = str(payload.get("brief") or "").strip()[:MAX_BRIEF]
    skill = str(payload.get("skill") or "")
    if not _CODE_RE.match(code) or not h or not brief:
        raise PublicSkillError("bad_request", status=400)
    if skill != "content.video":
        raise PublicSkillError("skill_unavailable", status=503)

    entry = next((e for e in public_agents.public_entries() if e["hash"] == h), None)
    if not entry:
        raise PublicSkillError("agent_not_public", status=404)
    hire = _hire_settings(entry)
    if not hire["on"]:
        raise PublicSkillError("hire_off", status=409)
    if not preset or preset not in hire["presets"]:
        raise PublicSkillError("template_missing", status=409)
    if not _studio_preset_exists(preset):
        raise PublicSkillError("template_missing", status=409)

    # Tuỳ chọn khách gửi kèm (form thuê trên Town, user 28/9): giọng CapCut của CHÍNH máy
    # này (khách chọn từ danh mục catalog) + tiêu đề video. Sai dạng → bỏ lặng lẽ, video
    # vẫn dựng bằng giọng/tiêu đề mặc định của mẫu — không phải lý do để từ chối việc.
    voice = str(payload.get("voice") or "")
    if not re.match(r"^[A-Za-z0-9_.-]{2,64}$", voice):
        voice = ""
    title = " ".join(str(payload.get("title") or "").split())[:120]
    ratio = str(payload.get("ratio") or "")
    if ratio not in ("16:9", "9:16"):
        ratio = ""
    async with _lock:
        if code in _jobs:            # cloud gọi lại (mạng chớp) — không mở việc thứ hai
            return {"ok": True, "job": code}
        minutes = max(1, min(60, int(payload.get("minutes") or 0) or 10))
        job = {"code": code, "agent_id": entry["agent_id"], "preset": preset, "brief": brief,
               "unit": "minute" if payload.get("unit") == "minute" else "job",
               "minutes": minutes, "price": int(payload.get("price") or 0),
               "voice": voice, "title": title, "ratio": ratio,
               "status": "accepted", "task_id": "", "files": [], "paths": [],
               "seconds": 0, "at": time.time()}
        _jobs[code] = job
        _save(job)
        job["_task"] = asyncio.create_task(_run(code))
    logger.info("[hire] nhận việc %s: mẫu «%s», %s phút", code, preset, minutes)
    return {"ok": True, "job": code}


def resume() -> None:
    """Gọi lúc server dậy: nạp việc đang dở từ đĩa và bám tiếp task Codex của nó.

    Không nối thì restart giữa lượt dựng = cloud thấy máy im 30 phút rồi hoàn tiền oan,
    trong khi task vẫn dựng tiếp thành mồ côi. Việc đã bị cloud đóng (hoàn/huỷ từ trước)
    tự thoát ở lượt báo cáo đầu của _run — cloud trả {closed:true} là đóng sổ, và vì
    task_id đã có nên _run KHÔNG tạo task Codex thứ hai."""
    try:
        names = [f for f in os.listdir(_dir()) if f.endswith(".json")]
    except OSError:
        return
    for name in names:
        j = job_record(name[:-5])
        if not j or j.get("status") not in ("accepted", "running") or j.get("_task"):
            continue
        j["_task"] = asyncio.create_task(_run(j["code"]))
        logger.info("[hire] nối lại việc %s sau restart (task %s)", j["code"], j.get("task_id") or "?")


def _checkpoint(task_id: str) -> Dict[str, Any]:
    """Checkpoint đọc TRONG TIẾN TRÌNH (pipeline._read_checkpoint), KHÔNG qua HTTP.

    Route /events cố tình lột sạch event checkpoint cho trình duyệt nhẹ
    (codex/routes._public_events, user 15/9 «tốn ram») — nên vòng poll HTTP không bao
    giờ thấy video_path: video 07:32 dựng XONG vẫn bị báo «no_files», khách được hoàn
    oan (bắt 28/9 ở việc 34gm07symal9). public_hire sống cùng tiến trình với pipeline
    nên đọc thẳng là đường ngắn nhất và không thể bị route công khai lọc mất."""
    from tubecli.extensions.content_video.pipeline import _read_checkpoint

    return _read_checkpoint(task_id) or {}


def _latest_step(events: list) -> str:
    for ev in reversed(events or []):
        d = ev.get("data") if isinstance(ev, dict) else None
        if isinstance(d, dict) and d.get("step"):
            return str(d["step"])
    return ""


def _http_json(path: str) -> Dict[str, Any]:
    from tubecli.extensions.content_video.pipeline import _base_url
    with urllib.request.urlopen(urllib.request.Request(_base_url() + path), timeout=20) as res:
        return json.loads(res.read().decode("utf-8", "replace")) or {}


async def _run(code: str) -> None:
    job = _jobs.get(code)
    if not job:
        return
    try:
        await _report(job, "running", "queued", 2)
        if job["status"] == "closed":
            return
        from tubecli.extensions.content_video.pipeline import create_auto_task, media_seconds

        # Độ dài đặt hàng → cỡ kịch bản (~150 chữ/phút — xem content_video). KHÔNG đăng,
        # KHÔNG Drive: sản phẩm giao cho KHÁCH, không phải kênh của chủ máy.
        options = {"source_text": job["brief"], "preset": job["preset"],
                   "target_words": max(120, min(9000, job["minutes"] * 150)),
                   "job_label": "Việc thuê từ Town"}
        # Khách chọn giọng/tiêu đề trên form thuê → đè lên mặc định của mẫu. Giọng là
        # capcut_speaker của CHÍNH máy này (khách chọn từ catalog); không chọn thì thôi.
        if job.get("voice"):
            options["tts_engine"] = "capcut"
            options["capcut_speaker"] = job["voice"]
        if job.get("title"):
            options["title"] = job["title"]
        if job.get("ratio"):
            # Khách chọn tỉ lệ trên form thuê → thắng cả tỉ lệ của mẫu. explicit vì
            # 16:9 trùng mặc định pipeline — thiếu cờ này thì mẫu dọc vẫn thắng.
            options["aspect_ratio"] = job["ratio"]
            options["aspect_ratio_explicit"] = True
        # resume() nối lại việc dở sau restart: task Codex ĐÃ có thì bám tiếp, không
        # tạo task thứ hai (hai bộ dựng cho một đơn là ác mộng RAM lẫn tiền).
        tid = str(job.get("task_id") or "")
        if not tid:
            task = await asyncio.to_thread(
                lambda: create_auto_task(job["agent_id"], options, created_by="hire",
                                         origin={"agent_id": job["agent_id"], "hire": code},
                                         job_label="Việc thuê từ Town"))
            tid = str((task or {}).get("id") or "")
            if not tid:
                await _report(job, "failed", err="queue_failed")
                job["status"] = "failed"
                _save(job)
                return
            job["task_id"] = tid
            job["status"] = "running"
            _save(job)

        last_said, said_at = "", 0.0
        deadline = time.time() + 5.5 * 3600          # cloud tự hoàn sau 6 h — dừng trước nó
        while time.time() < deadline:
            await asyncio.sleep(POLL_SEC)
            if job["status"] == "closed":
                logger.info("[hire] %s: cloud đã đóng — ngừng theo (task %s vẫn trên bảng)", code, tid)
                return
            try:
                t = (await asyncio.to_thread(_http_json, f"/api/v1/codex/tasks/{tid}")).get("task") or {}
                evs = (await asyncio.to_thread(_http_json, f"/api/v1/codex/tasks/{tid}/events")).get("events") or []
            except Exception as e:      # noqa: BLE001 — server nghẽn chốc lát
                logger.debug("[hire] %s poll lỗi: %s", code, e)
                continue
            st = str(t.get("status") or "").lower()
            step = _latest_step(evs)
            if st in ("failed", "error", "cancelled", "canceled"):
                await _report(job, "failed", err="job_failed")
                job["status"] = "failed"
                _save(job)
                return
            if st in ("review", "completed", "done", "success"):
                cp = await asyncio.to_thread(_checkpoint, tid)
                path = str(cp.get("video_path") or "")
                if not path or not os.path.isfile(path):
                    await _report(job, "failed", err="no_files")
                    job["status"] = "failed"
                    _save(job)
                    return
                secs = int(await asyncio.to_thread(media_seconds, path) or 0)
                name = re.sub(r'[\\/:*?"<>|]+', " ", os.path.basename(path)).strip() or "video.mp4"
                job["paths"] = [path]
                job["files"] = [{"name": name, "bytes": os.path.getsize(path), "type": "video/mp4"}]
                job["seconds"] = secs
                _save(job)
                out = await _report(job, "ready", "done", 100, files=job["files"], seconds=secs)
                job["status"] = "delivered" if out.get("status") == "delivered" else str(out.get("status") or "reported")
                _save(job)
                logger.info("[hire] %s giao xong: %s (%ss) → cloud nói %s", code, name, secs, out)
                return
            key = f"{st}:{step}"
            if key != last_said and time.time() - said_at >= REPORT_MIN_GAP:
                await _report(job, "running", step or st, _STEP_PCT.get(step, 10))
                last_said, said_at = key, time.time()
        await _report(job, "failed", err="timeout")
        job["status"] = "failed"
        _save(job)
    except Exception as e:      # noqa: BLE001 — lỗi bất ngờ: báo hỏng để khách được hoàn
        logger.warning("[hire] %s hỏng: %s", code, e)
        try:
            await _report(job, "failed", err="job_failed")
        except Exception:      # noqa: BLE001
            pass
        job["status"] = "failed"
        _save(job)


def file_for(code: str, n: int) -> Optional[Dict[str, Any]]:
    """File thứ n của một việc đã giao — cho route /api/v1/public/hire/file."""
    j = job_record(code)
    if not j:
        return None
    paths = j.get("paths") or []
    files = j.get("files") or []
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if not (0 <= n < len(paths)) or not os.path.isfile(paths[n]):
        return None
    meta = files[n] if n < len(files) else {}
    return {"path": paths[n], "name": str(meta.get("name") or os.path.basename(paths[n])),
            "type": str(meta.get("type") or "video/mp4")}
