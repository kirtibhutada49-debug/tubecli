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
# Báo «còn sống» cả khi bước KHÔNG đổi: việc Pod xếp hàng chờ Muse (mỗi lúc một lượt) có thể đứng một bước hàng chục
# phút — cloud thấy im 30 phút (SILENT_WINDOW_SEC) là hoàn tiền trong khi task vẫn chạy thành mồ côi.
HEARTBEAT_SEC = 300
MAX_BRIEF = 4000
# Việc «video quảng cáo từ ảnh» (pod.video): ảnh khách gửi đi KÈM lệnh nhận việc (base64, cloud không cất).
POD_MAX_MODELS, POD_MAX_PRODUCTS = 3, 2
POD_IMG_MAX = 3 * 1024 * 1024           # mỗi ảnh sau giải mã — trình duyệt đã thu về ≤ 1600 px
_POD_STEP_PCT = {"intake": 5, "character": 15, "shots": 25, "board": 40, "clips": 60, "render": 90}
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
                  files: Optional[list] = None, seconds: int = 0, err: str = "", note: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {"job": job["code"], "status": status, "step": step[:48],
                            "pct": max(0, min(100, int(pct)))}
    if files is not None:
        body["files"] = files
    if seconds:
        body["seconds"] = int(seconds)
    if err:
        body["err"] = err[:40]
    if note:
        body["note"] = note[:400]        # cloud cũ bỏ qua khoá lạ
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
    if skill not in ("content.video", "pod.video"):
        raise PublicSkillError("skill_unavailable", status=503)

    entry = next((e for e in public_agents.public_entries() if e["hash"] == h), None)
    if not entry:
        raise PublicSkillError("agent_not_public", status=404)
    if skill == "pod.video":
        return await _receive_pod(payload, code, entry, brief)
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
        # Độ dài MÁY NHẮM (cloud ≥ 29/9): 0 = TỰ ĐỘNG theo nội dung — user: «dựa vào text đầu vào
        # để tạo video auto thời lượng chứ không ép thời lượng»; n = nhắm n phút (≤ minutes, số
        # phút khách đã trả). Cloud cũ không gửi → nhắm đúng `minutes` như trước.
        try:
            target = max(0, min(minutes, int(payload["target"]))) if payload.get("target") not in (None, "") else minutes
        except (TypeError, ValueError):
            target = minutes
        job = {"code": code, "agent_id": entry["agent_id"], "preset": preset, "brief": brief,
               "unit": "minute" if payload.get("unit") == "minute" else "job",
               "minutes": minutes, "target": target, "price": int(payload.get("price") or 0),
               "voice": voice, "title": title, "ratio": ratio,
               "status": "accepted", "task_id": "", "files": [], "paths": [],
               "seconds": 0, "at": time.time()}
        _jobs[code] = job
        _save(job)
        job["_task"] = asyncio.create_task(_run(code))
    logger.info("[hire] nhận việc %s: mẫu «%s», %s phút", code, preset, minutes)
    return {"ok": True, "job": code}


def _pod_images(items: Any, limit: int, code: str, tag: str) -> list:
    """Ảnh khách gửi (base64) → file trong kho ảnh Pod Studio. Chỉ JPEG/PNG/WEBP mở được bằng PIL, ≤ POD_IMG_MAX;
    sai một tấm là từ chối CẢ việc (khách biết ngay, chưa mất xu — cloud hoàn khi máy từ chối)."""
    import base64
    import io

    from tubecli.config import DATA_DIR
    if not items:
        return []
    if not isinstance(items, list) or len(items) > limit:
        raise PublicSkillError("bad_images", status=422)
    gal = os.path.join(str(DATA_DIR), "pod_studio", "gallery")
    os.makedirs(gal, exist_ok=True)
    out = []
    for i, it in enumerate(items, 1):
        raw_b64 = str((it or {}).get("b64") or "") if isinstance(it, dict) else ""
        try:
            data = base64.b64decode(raw_b64, validate=True)
        except Exception:      # noqa: BLE001
            raise PublicSkillError("bad_images", status=422)
        if not data or len(data) > POD_IMG_MAX:
            raise PublicSkillError("image_too_large" if data else "bad_images", status=422)
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            fmt = (im.format or "").upper()
            im.verify()
        except Exception:      # noqa: BLE001
            raise PublicSkillError("bad_images", status=422)
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt)
        if not ext:
            raise PublicSkillError("bad_images", status=422)
        p = os.path.join(gal, f"hire_{code}_{tag}{i}.{ext}")
        with open(p, "wb") as f:
            f.write(data)
        out.append(p)
    return out


_VOICE_PRESETS = ("bright", "warm", "elegant", "sweet", "pro")      # = VOICE_PRESETS của Pod Studio


def _voice_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """voice / voice_gender / voice_custom / voices[] (theo từng ảnh người mẫu) — kẹp giá trị, chỉ giữ cái có."""
    def one(d: Any) -> Dict[str, str]:
        d = d if isinstance(d, dict) else {}
        g = str(d.get("gender") or d.get("voice_gender") or "").strip().lower()
        v = str(d.get("voice") or "").strip().lower()
        return {"gender": g if g in ("male", "female") else "", "voice": v if v in _VOICE_PRESETS else "",
                "voice_custom": " ".join(str(d.get("voice_custom") or "").split())[:200]}
    top = one(payload)
    out: Dict[str, Any] = {k: val for k, val in (("voice", top["voice"]), ("voice_gender", top["gender"]),
                                                  ("voice_custom", top["voice_custom"])) if val}
    voices = [one(x) for x in (payload.get("voices") or [])[:POD_MAX_MODELS]] if isinstance(payload.get("voices"), list) else []
    voices = [{k: val for k, val in v.items() if val} for v in voices]
    if any(voices):
        out["voices"] = voices
    return out


def _pod_template(name: str) -> Optional[Dict[str, Any]]:
    try:
        from tubecli.core import templates as T
    except ImportError:
        return None
    t = T.get_template(name)
    return T.section_view(t, "ref_video") if t else None


async def _receive_pod(payload: Dict[str, Any], code: str, entry: Dict[str, Any], brief: str) -> Dict[str, Any]:
    """Nhận việc «video quảng cáo từ ảnh» (pod.video): kiểm cài đặt chủ, mẫu, số clip, ảnh; lưu ảnh; chạy nền."""
    st = entry.get("settings") or {}
    if not st.get("hire_pod_on"):
        raise PublicSkillError("hire_off", status=409)
    tpl = " ".join(str(payload.get("preset") or "").split())[:60]
    names = {str(n).casefold(): str(n) for n in (st.get("hire_pod_templates") or [])}
    if not tpl or tpl.casefold() not in names:
        raise PublicSkillError("template_missing", status=409)
    tpl = names[tpl.casefold()]
    view = _pod_template(tpl)
    if view is None:
        raise PublicSkillError("template_missing", status=409)
    try:
        from tubecli.core import muse
        if not muse.settings()["profile"]:
            raise PublicSkillError("skill_unavailable", status=503)     # chưa cấu hình Muse: không làm được clip
    except ImportError:
        raise PublicSkillError("skill_unavailable", status=503)
    try:
        clips = int(payload.get("clips") or 0)
    except (TypeError, ValueError):
        clips = 0
    from tubecli.core.public_agents import HIRE_POD_CLIPS_MAX
    cmax = max(1, min(HIRE_POD_CLIPS_MAX, int(st.get("hire_pod_clips_max") or 6)))
    if not 1 <= clips <= cmax:
        raise PublicSkillError("bad_clips", status=422)
    raw_models = payload.get("models") or []
    if raw_models and not st.get("hire_pod_models", True):
        raise PublicSkillError("models_not_allowed", status=409)
    if raw_models and payload.get("consent") is not True:
        raise PublicSkillError("need_consent", status=422)
    own = [p for p in (view.get("model_images") or []) if isinstance(p, str) and os.path.isfile(p)]
    if not raw_models and not own:
        raise PublicSkillError("need_model", status=422)
    ratio = str(payload.get("ratio") or "")
    # Tuỳ biến của khách (Town 3/10/2026): kiểu bối cảnh + kiểu nhân vật — chữ thuần một dòng, ≤300 ký tự mỗi ô
    scene = " ".join(str(payload.get("scene") or "").split())[:300]
    character = " ".join(str(payload.get("character") or "").split())[:300]
    # Giọng khách chọn (3/10/2026 tối): kiểu giọng, giới, mô tả — chung cả đơn + theo từng ảnh người mẫu (voices[])
    voice = _voice_fields(payload)
    async with _lock:
        if code in _jobs:            # cloud gọi lại (mạng chớp) — không mở việc thứ hai
            return {"ok": True, "job": code}
        models = _pod_images(raw_models, POD_MAX_MODELS, code, "m")
        products = _pod_images(payload.get("products") or [], POD_MAX_PRODUCTS, code, "p")
        job = {"code": code, "kind": "pod", "agent_id": entry["agent_id"], "preset": tpl, "brief": brief,
               "unit": "clip", "clips": clips, "price": int(payload.get("price") or 0),
               "models": models, "products": products, "consent": bool(raw_models),
               "ratio": ratio if ratio in ("9:16", "16:9", "1:1") else "", "scene": scene, "character": character,
               **voice,
               "status": "accepted", "task_id": "", "files": [], "paths": [], "seconds": 0, "at": time.time()}
        _jobs[code] = job
        _save(job)
        job["_task"] = asyncio.create_task(_run(code))
    logger.info("[hire] nhận việc video quảng cáo %s: mẫu «%s», %s clip, %s ảnh người mẫu, %s ảnh sản phẩm",
                code, tpl, clips, len(models), len(products))
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


def _http_post_json(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    from tubecli.extensions.content_video.pipeline import _base_url
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_base_url() + path, data=raw, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8", "replace")) or {}


def _pod_final(task_id: str) -> str:
    """Video đã ghép của task Pod — đọc checkpoint của pipe (data/pod_studio/ref_video/<task>/state.json)."""
    from tubecli.config import DATA_DIR
    d = os.path.join(str(DATA_DIR), "pod_studio", "ref_video", re.sub(r"[^\w.-]", "_", str(task_id)))
    try:
        with open(os.path.join(d, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        return str(((st or {}).get("final") or {}).get("path") or "")
    except (OSError, ValueError):
        return ""


# Muse trả CHỮ thay cho ảnh/video (từ chối nội dung, hay đề nghị vẽ khác) — việc thuê #164 (3/10/2026): khách chỉ thấy
# «việc hỏng» mà không biết sửa gì. Tách lời AI ra gửi kèm mã riêng để thẻ việc nói cho khách biết mà sửa bối cảnh/yêu
# cầu. Lỗi khác (máy, mạng, ffmpeg…) KHÔNG gửi chữ: người lạ không cần đọc đường dẫn hay lỗi nội bộ của máy chủ agent.
_MUSE_NO_OUTPUT_RE = re.compile(r"Muse did not (draw an image|make a video)\s*[:.]?\s*(.*)", re.S | re.I)
_NOTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|root|tmp|var|Users|opt)/)\S*")
_NOTE_URL_RE = re.compile(r"https?://\S+", re.I)


def pod_failure(error: str) -> tuple:
    """(mã lỗi cho cloud, lời giải thích cho khách) từ lỗi của task Pod."""
    m = _MUSE_NO_OUTPUT_RE.search(str(error or ""))
    if not m:
        return "job_failed", ""
    said = _NOTE_URL_RE.sub("", _NOTE_PATH_RE.sub("", m.group(2) or ""))
    said = " ".join(said.split())[:400]
    return ("image_refused" if m.group(1).lower().startswith("draw") else "video_refused"), said


async def _run_pod(code: str) -> None:
    """Việc «video quảng cáo từ ảnh»: xếp task «Video từ ảnh tham chiếu» của Pod Studio lên Bảng việc của chủ (origin mang
    mã việc; KHÔNG đóng nhãn AI — chủ dự án bỏ 3/10/2026), bám tiến độ, giao video đã ghép. Cấu trúc như _run của content_video."""
    job = _jobs[code]
    await _report(job, "running", "queued", 2)
    if job["status"] == "closed":
        return
    tid = str(job.get("task_id") or "")
    if not tid:
        body = {"model_images": job.get("models") or [], "product_images": job.get("products") or [],
                "request": job["brief"], "template": job["preset"], "clips": job["clips"],
                "hire": code, "created_by": "hire", "title": f"Town hire {code}"}
        if job.get("ratio"):
            body["aspect"] = job["ratio"]
        # Tuỳ biến của khách: chỉ gửi khi có — trống thì Pod lấy theo mẫu (_apply_template)
        if job.get("scene"):
            body["scene_custom"] = job["scene"]
        if job.get("character"):
            body["character_custom"] = job["character"]
        for k in ("voice", "voice_gender", "voice_custom", "voices"):
            if job.get(k):
                body[k] = job[k]
        try:
            out = await asyncio.to_thread(_http_post_json, "/api/v1/pod_studio/ref-video/run", body)
        except Exception as e:      # noqa: BLE001 — Pod Studio tắt / mẫu mất / thiếu ảnh
            logger.warning("[hire] %s: không xếp được task Pod: %s", code, e)
            out = {}
        tid = str(((out or {}).get("task") or {}).get("id") or "")
        if not tid:
            await _report(job, "failed", err="queue_failed")
            job["status"] = "failed"
            _save(job)
            return
        job["task_id"] = tid
        job["status"] = "running"
        _save(job)

    last_said, said_at = "", time.time()
    deadline = time.time() + 5.5 * 3600
    while time.time() < deadline:
        await asyncio.sleep(POLL_SEC)
        if job["status"] == "closed":
            logger.info("[hire] %s: cloud đã đóng — ngừng theo (task %s vẫn trên bảng)", code, tid)
            return
        try:
            t = (await asyncio.to_thread(_http_json, f"/api/v1/codex/tasks/{tid}")).get("task") or {}
            evs = (await asyncio.to_thread(_http_json, f"/api/v1/codex/tasks/{tid}/events")).get("events") or []
        except Exception as e:      # noqa: BLE001
            logger.debug("[hire] %s poll lỗi: %s", code, e)
            continue
        st = str(t.get("status") or "").lower()
        step = _latest_step(evs)
        if st in ("failed", "error", "cancelled", "canceled"):
            err, note = pod_failure(t.get("error")) if st in ("failed", "error") else ("job_failed", "")
            await _report(job, "failed", err=err, note=note)
            job["status"] = "failed"
            _save(job)
            return
        if st in ("review", "completed", "done", "success"):
            path = await asyncio.to_thread(_pod_final, tid)
            if not path or not os.path.isfile(path):
                await _report(job, "failed", err="no_files")
                job["status"] = "failed"
                _save(job)
                return
            from tubecli.extensions.content_video.pipeline import media_seconds
            secs = int(await asyncio.to_thread(media_seconds, path) or 0)
            name = f"video-quang-cao-{code}.mp4"
            job["paths"] = [path]
            job["files"] = [{"name": name, "bytes": os.path.getsize(path), "type": "video/mp4"}]
            job["seconds"] = secs
            _save(job)
            out = await _report(job, "ready", "done", 100, files=job["files"], seconds=secs)
            job["status"] = "delivered" if out.get("status") == "delivered" else str(out.get("status") or "reported")
            _save(job)
            logger.info("[hire] %s giao xong video quảng cáo (%ss) → cloud nói %s", code, secs, out)
            return
        key = f"{st}:{step}"
        now = time.time()
        if (key != last_said and now - said_at >= REPORT_MIN_GAP) or now - said_at >= HEARTBEAT_SEC:
            await _report(job, "running", step or st, _POD_STEP_PCT.get(step, 10))
            last_said, said_at = key, now
    await _report(job, "failed", err="timeout")
    job["status"] = "failed"
    _save(job)


async def _run(code: str) -> None:
    job = _jobs.get(code)
    if not job:
        return
    if job.get("kind") == "pod":
        try:
            await _run_pod(code)
        except Exception as e:      # noqa: BLE001 — lỗi bất ngờ: báo hỏng để khách được hoàn
            logger.warning("[hire] %s hỏng: %s", code, e)
            try:
                await _report(job, "failed", err="job_failed")
            except Exception:      # noqa: BLE001
                pass
            job["status"] = "failed"
            _save(job)
        return
    try:
        await _report(job, "running", "queued", 2)
        if job["status"] == "closed":
            return
        from tubecli.extensions.content_video.pipeline import create_auto_task, media_seconds

        # Độ dài đặt hàng → cỡ kịch bản (~150 chữ/phút — xem content_video). KHÔNG đăng,
        # KHÔNG Drive: sản phẩm giao cho KHÁCH, không phải kênh của chủ máy.
        options = {"source_text": job["brief"], "preset": job["preset"],
                   "job_label": "Việc thuê từ Town"}
        tgt = int(job.get("target", job["minutes"]) or 0)     # sổ việc trước .186 không có khoá → như cũ
        cap = max(120, min(9000, job["minutes"] * 150))
        if tgt > 0:
            options["target_words"] = max(120, min(9000, tgt * 150))
        else:
            # TỰ ĐỘNG: KHÔNG đặt target_words — pipeline đo chính bài dán (resolve_words → "content")
            # và giữ câu của khách. Chỉ kẹp khi bài dài hơn số phút khách đã trả (trần của chủ).
            try:
                from tubecli.extensions.content_video.pipeline import content_words
            except ImportError:
                content_words = lambda t: len(str(t or "").split())     # noqa: E731
            if content_words(job["brief"]) > cap:
                options["target_words"] = cap
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
            now = time.time()
            # đổi bước → báo (giãn REPORT_MIN_GAP); đứng một bước lâu (chờ làn video) → vẫn báo HEARTBEAT_SEC/lần
            if (key != last_said and now - said_at >= REPORT_MIN_GAP) or (said_at and now - said_at >= HEARTBEAT_SEC):
                await _report(job, "running", step or st, _STEP_PCT.get(step, 10))
                last_said, said_at = key, now
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
