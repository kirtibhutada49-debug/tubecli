"""Skill công khai «Reup Douyin» (user 28/9/2026): tải video Douyin → tách phụ đề
(Gemini, dịch 1 bước) → lồng tiếng Edge-TTS theo logic ReupDouyin (core/reup_dub.py)
→ tuỳ chọn ghi phụ đề → giao file qua link /s/ trên chính máy chủ agent.

Cả chuỗi chạy 1–3 phút, vượt xa trần 40 s/lượt invoke → kiểu JOB hai pha:
  · input là LINK Douyin  → mở job chạy NỀN, trả ngay thẻ tiến độ {kind: "reupjob"};
  · input là JSON {"job"} → trả trạng thái; xong thì {kind: "reupfile", share, ...}.
Mỗi lượt hỏi trạng thái là một lượt quota như mọi lượt chat — client chỉ hỏi khi người
xem bấm «Kiểm tra», không bao giờ tự hỏi ngầm.

Neo an toàn quen thuộc: link Douyin đi qua đúng pick_link của douyin_downloader; file
trả về là đường dẫn /s/ TƯƠNG ĐỐI để cloud tự ghép tunnel; giọng/ngôn ngữ/chế độ đều
whitelist hai tầng. Job của ai người nấy hỏi: id job ngẫu nhiên 16 hex, chỉ đi qua
phiên chat đã lưu của chính người mở.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from tubecli.core.public_agents import PublicSkillError

logger = logging.getLogger("public_reup")

JOB_TTL_SEC = 6 * 3600
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MAX_PARALLEL_JOBS = 2
EXTRACT_TIMEOUT_SEC = 10 * 60
LANGS = {"", "vi", "en", "ja", "zh"}
MODES = {"replace", "mix"}
_JOB_RE = re.compile(r"^[a-f0-9]{16}$")

_jobs: Dict[str, Dict[str, Any]] = {}   # id -> {status, step, done, total, code, result, created, task}


def _root() -> Path:
    from tubecli.config import DATA_DIR

    d = Path(str(DATA_DIR)) / "reup_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _base() -> str:
    from tubecli.config import get_api_port

    return f"http://127.0.0.1:{get_api_port()}"


def _sweep() -> None:
    now = time.time()
    for jid in [j for j, v in _jobs.items() if now - v.get("created", 0) > JOB_TTL_SEC]:
        _jobs.pop(jid, None)
    try:
        for p in _root().iterdir():
            if p.is_dir() and now - p.stat().st_mtime > JOB_TTL_SEC:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _set(jid: str, **kw) -> None:
    if jid in _jobs:
        _jobs[jid].update(kw)


def _download_blocking(url: str, dest: Path) -> None:
    import requests

    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "Mozilla/5.0 (TubeCLI Reup)"}) as r:
        if r.status_code != 200:
            raise PublicSkillError("download_failed", status=502)
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                written += len(chunk)
                if written > MAX_VIDEO_BYTES:
                    raise PublicSkillError("video_too_big")
                f.write(chunk)
    if written < 10_000:
        raise PublicSkillError("download_failed", status=502)


async def _extract(file_path: str, translate_to: str, jid: str) -> list:
    """Tách phụ đề qua route job async của subtitle_extractor, dịch 1 bước bằng Gemini."""
    import requests

    def _post():
        return requests.post(_base() + "/api/v1/subtitle/extract", json={
            "file_path": file_path, "engine": "gemini",
            "translate_to": translate_to or None,
        }, timeout=30)

    r = await asyncio.to_thread(_post)
    if r.status_code == 404:
        raise PublicSkillError("skill_unavailable", status=503)
    if r.status_code >= 400:
        raise PublicSkillError("subtitle_failed", status=502)
    tid = (r.json() or {}).get("task_id")
    if not tid:
        raise PublicSkillError("subtitle_failed", status=502)

    deadline = time.monotonic() + EXTRACT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        await asyncio.sleep(2)

        def _poll():
            return requests.get(_base() + f"/api/v1/subtitle/status/{tid}", timeout=15)

        try:
            s = (await asyncio.to_thread(_poll)).json() or {}
        except Exception:      # noqa: BLE001 — một cú poll hỏng không giết job
            continue
        _set(jid, done=int(s.get("progress") or 0), total=int(s.get("total") or 0))
        st = s.get("status")
        if st == "success":
            subs = ((s.get("result") or {}).get("subtitles")) or []
            if not subs:
                raise PublicSkillError("no_speech", status=422)
            return subs
        if st == "error":
            msg = str((s.get("result") or {}).get("message") or "")
            logger.warning("[reup %s] extract lỗi: %s", jid, msg[:200])
            raise PublicSkillError("subtitle_failed", status=502)
    raise PublicSkillError("timeout", status=504)


async def _run_job(jid: str, link: str, opts: Dict[str, Any]) -> None:
    from tubecli.core import public_share as ps
    from tubecli.core import reup_dub

    jdir = _root() / jid
    jdir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. Link → media gốc, đúng đường skill Douyin công khai (không cookie chủ)
        _set(jid, step="download")
        from tubecli.extensions.douyin_downloader.public_skill import resolve as dy_resolve

        res = await dy_resolve(link)
        if res.get("kind") not in ("video",):
            raise PublicSkillError("not_video", status=422)
        media = (res.get("media") or [{}])[0]
        url = media.get("direct") or media.get("url")
        if not url:
            raise PublicSkillError("download_failed", status=502)
        src = jdir / "src.mp4"
        await asyncio.to_thread(_download_blocking, url, src)

        # 2. Tách phụ đề (+ dịch nếu chọn)
        _set(jid, step="subtitle", done=0, total=0)
        subs = await _extract(str(src), str(opts.get("lang") or ""), jid)

        # 3. Lồng tiếng theo logic ReupDouyin
        _set(jid, step="dub", done=0, total=len(subs))
        out = jdir / "reup.mp4"

        def _prog(phase: str, done: int, total: int) -> None:
            _set(jid, step="dub" if phase in ("tts",) else phase, done=done, total=total)

        r = await reup_dub.dub_video(
            str(src), subs, str(opts.get("voice") or "vi-VN-HoaiMyNeural"), str(out),
            mode=str(opts.get("mode") or "replace"), bg_volume=0.15,
            burn=str(opts.get("burn") or "0") == "1", progress=_prog)
        if r.get("status") != "success":
            code = str(r.get("message") or "reup_failed")
            raise PublicSkillError(code if re.match(r"^[a-z_]{3,32}$", code) else "reup_failed",
                                   status=502)

        # 4. Giao hàng: file mp4 + phụ đề .srt qua kho share (chủ máy thấy, thu hồi được)
        _set(jid, step="share")
        srt = jdir / "reup.srt"
        reup_dub.write_srt(subs, srt)
        share = ps.share_path(str(out), f"reup_{jid}.mp4", JOB_TTL_SEC)
        srt_share = ps.share_path(str(srt), f"reup_{jid}.srt", JOB_TTL_SEC)
        dur = await reup_dub.media_duration(str(out))
        _set(jid, status="done", result={
            "share": share + "/download",
            "page": share,
            "srt": srt_share + "/download",
            "size": out.stat().st_size,
            "duration": f"{int(dur // 60)}:{int(dur % 60):02d}",
            "segments": int(r.get("segments") or 0),
        })
        try:
            src.unlink()               # video gốc không giao ra ngoài — dọn luôn
        except OSError:
            pass
    except PublicSkillError as e:
        _set(jid, status="error", code=e.code)
    except Exception as e:      # noqa: BLE001
        logger.warning("[reup %s] hỏng: %s", jid, e, exc_info=True)
        _set(jid, status="error", code="reup_failed")


def _snapshot(jid: str) -> Dict[str, Any]:
    j = _jobs.get(jid)
    if not j:
        raise PublicSkillError("job_not_found", status=404)
    if j.get("status") == "done":
        return {"kind": "reupfile", "job": jid, **j["result"], "expires_h": JOB_TTL_SEC // 3600}
    return {
        "kind": "reupjob", "job": jid,
        "status": "error" if j.get("status") == "error" else "running",
        "step": str(j.get("step") or "download"),
        "done": int(j.get("done") or 0), "total": int(j.get("total") or 0),
        **({"code": j["code"]} if j.get("code") else {}),
    }


async def resolve(text: str, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _sweep()
    t = str(text or "").strip()

    # Pha 2: hỏi trạng thái job
    if t.startswith("{"):
        try:
            jid = str((json.loads(t) or {}).get("job") or "")
        except ValueError:
            raise PublicSkillError("bad_input")
        if not _JOB_RE.match(jid):
            raise PublicSkillError("bad_input")
        return _snapshot(jid)

    # Pha 1: link Douyin → mở job nền
    from tubecli.extensions.douyin_downloader.public_skill import pick_link

    link = pick_link(t)
    if not link:
        raise PublicSkillError("need_douyin_link")
    if sum(1 for j in _jobs.values() if j.get("status") == "running") >= MAX_PARALLEL_JOBS:
        raise PublicSkillError("busy", status=429)

    o = opts or {}
    from tubecli.core.reup_dub import EDGE_VOICES

    job_opts = {
        "voice": o.get("voice") if o.get("voice") in EDGE_VOICES else "vi-VN-HoaiMyNeural",
        "lang": o.get("lang") if o.get("lang") in LANGS else "",
        "burn": "1" if str(o.get("burn") or "") == "1" else "0",
        "mode": o.get("mode") if o.get("mode") in MODES else "replace",
    }
    jid = uuid.uuid4().hex[:16]
    _jobs[jid] = {"status": "running", "step": "download", "done": 0, "total": 0,
                  "created": time.time()}
    # Giữ tham chiếu task trong job — không thì garbage collector nhặt mất giữa chừng.
    _jobs[jid]["task"] = asyncio.create_task(_run_job(jid, link, job_opts))
    return _snapshot(jid)
