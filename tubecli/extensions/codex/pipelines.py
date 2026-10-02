"""Sổ đăng ký pipeline + loại việc do EXTENSION khai cho Bảng việc (Codex).

Vì sao: executor từng chỉ biết hai nhà (`video_studio.*`, `content_video.*`) viết cứng, nên extension ngoài (Pod
Studio…) muốn có một "pipe" trên Bảng việc thì phải sửa lõi. Giờ extension gọi `register_pipeline` lúc bật
(on_enable) và — nếu muốn có ô trong cửa sổ «Nhiệm vụ mới» — `register_task_kind` với mô tả form; codex.js vẽ form
từ mô tả ấy, không biết gì về extension.

  register_pipeline("pod_studio.", runner, steps={"board": "pod_studio", ...})
      runner(kind, payload, report, is_cancelled) -> str   (hàm đồng bộ chạy trong thread, hoặc async)
  register_task_kind({
      "id": "pod_studio.ad",                # = kind ghi vào event log của task
      "label": {"en": "...", "vi": "..."},  # hay chuỗi; thiếu ngôn ngữ thì về en
      "hint": {...}, "icon": "shopping_bag",
      "submit_url": "/api/v1/pod_studio/ad-pipeline/run",   # POST JSON {fields…, created_by} → {status:"queued", task}
      "upload_url": "/api/v1/pod_studio/gallery/upload-image",  # POST multipart file → {url, filepath}
      "fields": [{"key": "model_images", "type": "images", "label": {...}, "required": True, "max": 4},
                 {"key": "request", "type": "textarea", "label": {...}, "required": True, "rows": 5},
                 {"key": "profile", "type": "select", "label": {...}, "options_url": "/api/v1/pod_studio/browser-profiles"},
                 {"key": "clips", "type": "number", "label": {...}, "default": 3, "min": 1, "max": 6}],
      "order": 50,
  })
Mọi hàm ở đây KHÔNG ném: extension hỏng không được làm Bảng việc chết.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Codex")

_PIPELINES: Dict[str, Dict[str, Any]] = {}      # prefix → {"runner", "steps", "extension"}
_KINDS: Dict[str, Dict[str, Any]] = {}          # kind id → spec

FIELD_TYPES = ("text", "textarea", "number", "select", "images", "checkbox")


def register_pipeline(prefix: str, runner: Callable[..., Any], steps: Optional[Dict[str, str]] = None,
                      extension: str = "") -> None:
    """Đăng ký (hoặc thay) bộ chạy cho mọi kind bắt đầu bằng `prefix` (vd "pod_studio.")."""
    p = str(prefix or "").strip()
    if not p or not callable(runner):
        logger.warning("[Codex] register_pipeline: thiếu prefix hoặc runner")
        return
    _PIPELINES[p] = {"runner": runner, "steps": dict(steps or {}), "extension": str(extension or p.rstrip(".") )}
    logger.info(f"[Codex] pipeline registered: {p}* ({_PIPELINES[p]['extension']})")


def unregister_pipeline(prefix: str) -> None:
    _PIPELINES.pop(str(prefix or "").strip(), None)
    for kid in [k for k, s in _KINDS.items() if str(k).startswith(str(prefix or ""))]:
        _KINDS.pop(kid, None)


def find_pipeline(kind: str) -> Optional[Dict[str, Any]]:
    """Mục đăng ký khớp kind (prefix dài nhất thắng), hoặc None."""
    k = str(kind or "")
    best = None
    for p, entry in _PIPELINES.items():
        if k.startswith(p) and (best is None or len(p) > len(best[0])):
            best = (p, entry)
    return best[1] if best else None


async def run_registered(kind: str, payload: Dict[str, Any], report, is_cancelled) -> Optional[str]:
    """Chạy kind qua sổ đăng ký; None khi không ai nhận. Hàm đồng bộ chạy trong thread (như content_video)."""
    entry = find_pipeline(kind)
    if not entry:
        return None
    import asyncio
    runner = entry["runner"]
    if inspect.iscoroutinefunction(runner):
        return await runner(kind, payload, report, is_cancelled)
    return await asyncio.to_thread(runner, kind, payload, report, is_cancelled)


def step_extension(step: str) -> Optional[str]:
    """Extension vẽ cho một bước (trang chủ vẽ extension, không vẽ tên bước) — None nếu không ai khai."""
    s = str(step or "").strip().lower()
    for entry in _PIPELINES.values():
        if s in entry["steps"]:
            return entry["steps"][s]
    return None


def register_task_kind(spec: Dict[str, Any]) -> None:
    """Khai một loại việc cho cửa sổ «Nhiệm vụ mới». Khai sai thì bỏ qua và ghi log, không ném."""
    try:
        kid = str((spec or {}).get("id") or "").strip()
        if not kid or not str(spec.get("submit_url") or "").startswith("/"):
            raise ValueError("cần id và submit_url bắt đầu bằng /")
        fields = []
        for f in spec.get("fields") or []:
            key, ftype = str(f.get("key") or "").strip(), str(f.get("type") or "text").strip().lower()
            if not key or ftype not in FIELD_TYPES:
                raise ValueError(f"trường {key!r} kiểu {ftype!r} không hợp lệ")
            fields.append({**f, "key": key, "type": ftype})
        _KINDS[kid] = {**spec, "id": kid, "fields": fields, "order": int(spec.get("order") or 100)}
        logger.info(f"[Codex] task kind registered: {kid}")
    except Exception as e:      # noqa: BLE001
        logger.warning(f"[Codex] register_task_kind bỏ qua: {e}")


def _pick_lang(value: Any, lang: str) -> Any:
    """{"en": …, "vi": …} → chuỗi theo ngôn ngữ (thiếu thì en, rồi bất kỳ); chuỗi trần giữ nguyên."""
    if isinstance(value, dict):
        return value.get(lang) or value.get("en") or next((v for v in value.values() if v), "")
    return value


def task_kinds(lang: str = "en") -> List[Dict[str, Any]]:
    """Danh sách loại việc đã dịch, theo `order`. Chỉ loại có pipeline đang đăng ký (extension đã bật)."""
    lang = str(lang or "en").split("-")[0].lower() if lang not in ("zh-TW",) else lang
    out = []
    for kid, spec in _KINDS.items():
        if not find_pipeline(kid):
            continue
        item = {k: _pick_lang(v, lang) for k, v in spec.items() if k != "fields"}
        item["fields"] = []
        for f in spec["fields"]:
            ff = {k: _pick_lang(v, lang) for k, v in f.items() if k != "options"}
            if isinstance(f.get("options"), list):
                ff["options"] = [{"value": o.get("value") if isinstance(o, dict) else o,
                                  "label": _pick_lang(o.get("label"), lang) if isinstance(o, dict) else o}
                                 for o in f["options"]]
            item["fields"].append(ff)
        out.append(item)
    out.sort(key=lambda s: (s.get("order", 100), s["id"]))
    return out
