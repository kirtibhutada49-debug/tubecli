"""Giọng đọc + bộ vẽ ảnh MẶC ĐỊNH của từng agent (user 28/9/2026: «agent chưa có provider
image và voice mặc định… ví dụ setup CapCut TTS làm mặc định cho subtitle»).

Bốn trường trên Agent (agents.json):
  tts_engine      edge | capcut | "" (= tự động)
  tts_voice       mã giọng của engine đó ("" = giọng mặc định của engine / theo ngôn ngữ)
  image_provider  cloudflare | gemini | 9router | "" (= theo Cài đặt chung của máy)
  image_model     "" = model mặc định của nhà đó

Thứ tự ưu tiên ở MỌI nơi dùng: lựa chọn của chính lượt chạy (chat, task, người xem trên
Town) > mẫu Studio > MẶC ĐỊNH CỦA AGENT > tự động. Agent chỉ lấp chỗ trống, không bao giờ
đè một lựa chọn cụ thể hơn.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Tuple

VOICE_ENGINES = ("edge", "capcut")
# = image_gen.PROVIDERS — chép lại để agent.py không phải import bộ vẽ ảnh khi nạp agents.json
IMAGE_PROVIDERS = ("cloudflare", "gemini", "9router", "muse")
MEDIA_FIELDS = ("tts_engine", "tts_voice", "image_provider", "image_model")

_VOICE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9@/_.:+-]{1,160}$")

EDGE_BY_LANG = {
    "vi": "vi-VN-HoaiMyNeural", "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural", "zh": "zh-CN-XiaoxiaoNeural",
}


def clean_media_fields(values: Dict[str, Any]) -> Dict[str, Any]:
    """Ép bốn trường về dạng hợp lệ, khoá khác để nguyên. Giá trị lạ về "" (= tự động)
    chứ không báo lỗi: agents.json do người dùng/bản cũ ghi, một giá trị hỏng không được
    làm agent không nạp nổi."""
    out = dict(values)
    if "tts_engine" in out:
        e = str(out["tts_engine"] or "").strip().lower()
        out["tts_engine"] = e if e in VOICE_ENGINES else ""
    if "tts_voice" in out:
        v = str(out["tts_voice"] or "").strip()
        out["tts_voice"] = v if _VOICE_RE.match(v) else ""
    if "image_provider" in out:
        p = str(out["image_provider"] or "").strip().lower()
        out["image_provider"] = p if p in IMAGE_PROVIDERS else ""
    if "image_model" in out:
        m = str(out["image_model"] or "").strip()
        out["image_model"] = m if _MODEL_RE.match(m) else ""
    return out


def _agent(agent_or_id: Any):
    if agent_or_id is None or agent_or_id == "":
        return None
    if isinstance(agent_or_id, str):
        try:
            from tubecli.core.agent import agent_manager
            return agent_manager.get(agent_or_id)
        except Exception:      # noqa: BLE001
            return None
    return agent_or_id


def agent_voice(agent_or_id: Any) -> Tuple[str, str]:
    """(engine, voice) mặc định của agent; ("", "") khi chưa đặt hay không thấy agent."""
    a = _agent(agent_or_id)
    if a is None:
        return "", ""
    c = clean_media_fields({"tts_engine": getattr(a, "tts_engine", ""), "tts_voice": getattr(a, "tts_voice", "")})
    return c["tts_engine"], (c["tts_voice"] if c["tts_engine"] else "")


def agent_image(agent_or_id: Any) -> Tuple[str, str]:
    """(provider, model) mặc định của agent; ("", "") = theo Cài đặt chung."""
    a = _agent(agent_or_id)
    if a is None:
        return "", ""
    c = clean_media_fields({"image_provider": getattr(a, "image_provider", ""),
                            "image_model": getattr(a, "image_model", "")})
    return c["image_provider"], (c["image_model"] if c["image_provider"] else "")


# Mã giọng CapCut không theo một khuôn: vi_female_huong, BV075_streaming (vi),
# BV421_vivn_streaming, en_us_002, ICL_en_male_…, ICL_jp_female_… Đoán theo các mẩu đã
# gặp; không đoán được thì trả "" và bên gọi cứ dùng giọng đó.
_CAPCUT_LANG = (
    (re.compile(r"(^|_)(vi|vivn)(_|$)"), "vi"), (re.compile(r"^bv0(75|74)_"), "vi"),
    (re.compile(r"(^|_)en(_|$)"), "en"), (re.compile(r"(^|_)(jp|ja)(_|$)"), "ja"),
    (re.compile(r"(^|_)(zh|cn)(_|$)"), "zh"), (re.compile(r"(^|_)(kr|ko)(_|$)"), "ko"),
)


def voice_lang(engine: str, voice: str) -> str:
    v = str(voice or "").strip().lower()
    if not v:
        return ""
    if engine == "edge":
        m = re.match(r"^([a-z]{2})-[a-z]{2}-", v)
        return m.group(1) if m else ""
    for rx, lang in _CAPCUT_LANG:
        if rx.search(v):
            return lang
    return ""


def guess_lang(texts: Iterable[str]) -> str:
    """Ngôn ngữ của phụ đề khi người dùng chọn «giữ nguyên tiếng gốc» — đủ để chọn giọng."""
    s = " ".join(str(t or "") for t in texts)[:4000]
    if not s.strip():
        return ""
    kana = len(re.findall(r"[぀-ヿ]", s))
    han = len(re.findall(r"[一-鿿]", s))
    if kana >= 3:
        return "ja"
    if han >= max(3, len(s) // 10):
        return "zh"
    if re.search(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", s.lower()):
        return "vi"
    # chỉ số / ký hiệu thì không nói được là tiếng gì
    return "en" if re.search(r"[A-Za-z]{2,}", s) else ""
