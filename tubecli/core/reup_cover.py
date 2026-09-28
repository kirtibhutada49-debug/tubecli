"""Che PHỤ ĐỀ GỐC cháy sẵn trong video — port từ ReupDouyin core/subtitles/region_detect.py
+ pipeline.stage_clean (user 28/9/2026: «cái logic che sub nội suy chưa thấy tích hợp»).

  1. Trích 6 khung rải đều (bỏ 5 % đầu/cuối), gửi từng khung cho Gemini vision (khoá
     trong Cloud API Keys, xoay vòng khi 429) → ô phụ đề [x1,y1,x2,y2] chuẩn hoá 0..1.
  2. Gộp: ngang lấy HỢP (phủ câu dài nhất), dọc lấy TRUNG VỊ, đệm 3 %/1.5 %, dải rộng tối
     thiểu 72 % khung (6 khung mẫu rơi vào câu ngắn thì câu dài vẫn thò hai đầu).
     Thấy chữ ở ≥ 1/3 số khung mới coi là có phụ đề cháy sẵn.
  3. Che: «delogo» = NỘI SUY từ viền (gần như vô hình trên nền phẳng), «blur» mờ
     Gaussian, «pixel» khảm, «fill» tối hẳn — ba kiểu sau NHOÈ MÉP qua mặt nạ gblur.

Khác bản gốc: không encode riêng một lượt che — trả về MẢNH FILTER để reup_dub ghép chung
với bước ghi phụ đề mới vào một lần encode (bớt một lần nén lại video). Phần dò/xoá logo
của bản gốc (tắt mặc định bên đó) không mang sang.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("reup_cover")

COVER_MODES = ("delogo", "blur", "pixel", "fill")
SAMPLE_FRAMES = 6
MIN_BAND_W = 0.72
STRICT_STATIC = 0.7
GEMINI_MODEL = "gemini-2.5-flash"
_KEY_TRIES = 4

# Nguyên văn prompt bên ReupDouyin: một lượt vision trả CẢ phụ đề lẫn logo/watermark.
_DETECT_PROMPT = (
    "You are a video overlay detector. The attached image is ONE frame from a video.\n\n"
    "Find TWO separate kinds of overlay drawn ON TOP of the video:\n\n"
    "1) \"subtitles\" — hardcoded/burned-in CAPTION of spoken dialogue (usually one or "
    "two lines near the bottom). Give a TIGHT box enclosing all lines together.\n\n"
    "2) \"overlays\" — PERMANENT branding in the TOP-LEFT or TOP-RIGHT corner that stays "
    "on screen: channel logo, watermark, username/handle, site name, corner sticker, fixed "
    "label text. Look closely — these are often small and faint. Only report items in "
    "those two top corners. Give a TIGHT box per item and a `kind` among: logo, "
    "watermark, username, text.\n\n"
    "IMPORTANT — do NOT list as \"overlays\": anything that is part of the filmed "
    "scene (signs, shop names, product labels, clothing text, screens in the scene), "
    "moving objects, people, or the dialogue caption itself.\n\n"
    "OUTPUT: ONE JSON object only, no markdown, no explanation. Coordinates are "
    "NORMALIZED floats in [0,1] relative to image width/height, "
    "box = [x1,y1,x2,y2] = [left, top, right, bottom].\n"
    "Example:\n"
    '{"subtitles": [{"text": "字幕内容", "box": [0.12, 0.86, 0.88, 0.95]}], '
    '"overlays": [{"kind": "logo", "text": "bilibili", "box": [0.82, 0.02, 0.98, 0.09]}]}\n'
    'If the frame has neither, return exactly: {"subtitles": [], "overlays": []}'
)


def norm_box(box) -> Optional[List[float]]:
    """[x1,y1,x2,y2] trong 0..1; chịu cả thang 0..1000 (kiểu Gemini hay trả)."""
    try:
        vals = [float(v) for v in list(box)[:4]]
    except (TypeError, ValueError):
        return None
    if len(vals) < 4:
        return None
    if any(v > 1.5 for v in vals):
        vals = [v / 1000.0 for v in vals]
    x1, x2 = sorted((vals[0], vals[2]))
    y1, y2 = sorted((vals[1], vals[3]))
    x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(1.0, x2), min(1.0, y2)
    if x2 - x1 < 0.02 or y2 - y1 < 0.01:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def _items(arr) -> List[Dict]:
    out = []
    for it in arr if isinstance(arr, list) else []:
        if isinstance(it, dict):
            b = norm_box(it.get("box") or it.get("bbox") or [])
            if b:
                out.append({"text": str(it.get("text") or "").strip(),
                            "kind": str(it.get("kind") or "").strip().lower(), "box": b})
    return out


def parse_detection(text: str) -> Tuple[List[Dict], List[Dict]]:
    """(phụ đề, logo) từ phản hồi model — chịu cả object {"subtitles","overlays"} lẫn mảng
    trần (coi hết là phụ đề)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            if isinstance(d, dict) and ("subtitles" in d or "overlays" in d):
                return _items(d.get("subtitles")), _items(d.get("overlays"))
        except ValueError:
            pass
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if m:
        try:
            return _items(json.loads(m.group())), []
        except ValueError:
            pass
    return [], []


# ── Logo/watermark: 3 CỬA LỌC trước khi xoá (xoá nhầm cảnh vật là phá video) ──────
def _iou(a: List[float], b: List[float]) -> float:
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def cluster_overlays(per_frame: List[List[Dict]], total: int, min_ratio: float = 0.6,
                     iou_thr: float = 0.4, need: Optional[int] = None) -> List[Dict]:
    """CỬA 1 — BỀN: cùng chỗ ở ≥ 60 % số khung (logo thật khung nào cũng có).
    `need` ghi đè số khung tối thiểu (1 = lấy mọi cụm, để cửa tĩnh chặt quyết)."""
    clusters: List[Dict] = []
    for fi, items in enumerate(per_frame):
        for it in items:
            b = it["box"]
            for c in clusters:
                if _iou(c["box"], b) >= iou_thr:
                    c["frames"].add(fi)
                    # ô đại diện = BAO TRÙM các lần thấy (logo hơi lệch giữa các khung)
                    c["box"] = [min(c["box"][0], b[0]), min(c["box"][1], b[1]),
                                max(c["box"][2], b[2]), max(c["box"][3], b[3])]
                    c["text"] = c["text"] or it.get("text", "")
                    break
            else:
                clusters.append({"box": list(b), "frames": {fi}, "text": it.get("text", ""),
                                 "kind": it.get("kind") or "logo"})
    if need is None:
        need = max(2, int(round(total * min_ratio)))
    return [{"box": [round(v, 4) for v in c["box"]], "seen": len(c["frames"]), "total": total,
             "text": c["text"], "kind": c["kind"]} for c in clusters if len(c["frames"]) >= need]


TOP_BAND = 0.22      # logo chỉ được nằm trong dải 22 % trên cùng…
CORNER_X = 0.4       # …và lệch hẳn về một góc: tâm ≤ 40 % (trái) hoặc ≥ 60 % (phải)


def filter_by_size(items: List[Dict], max_area: float = 0.12) -> List[Dict]:
    """CỬA 2 — NHỎ & Ở HAI GÓC TRÊN. User 28/9: «chỉ cần kiểm tra logo ở 2 góc trên trái
    phải thôi» — bản gốc nhận mọi rìa (cả đáy, cạnh bên), ở đây chỉ góc trên-trái và
    trên-phải; ô to hay chỗ khác đều coi là cảnh vật, không xoá."""
    out = []
    for it in items:
        b = it["box"]
        if (b[2] - b[0]) * (b[3] - b[1]) > max_area or b[3] > TOP_BAND:
            continue
        cx = (b[0] + b[2]) / 2
        if CORNER_X < cx < 1 - CORNER_X:
            continue
        out.append(it)
    return out


def verify_static(items: List[Dict], frames: List[bytes], ratio_thr: float = 0.95) -> List[Dict]:
    """CỬA 3 — TĨNH: độ biến thiên theo thời gian của ô ≤ 95 % mặt bằng khung. Ngưỡng
    KHÔNG chặt hơn: logo bán trong suốt để nền xuyên qua nên std vẫn 70–85 % mặt bằng."""
    if not items or len(frames) < 3:
        return items
    try:
        import io

        import numpy as np
        from PIL import Image

        arr = np.stack([np.asarray(Image.open(io.BytesIO(f)).convert("L"), dtype=np.float32)
                        for f in frames])
    except Exception as e:      # noqa: BLE001 — thiếu thư viện/khung lệch cỡ: bỏ cửa này
        logger.info("bỏ qua kiểm tĩnh logo: %s", e)
        return items
    std = arr.std(axis=0)
    h, w = std.shape
    overall = float(std.mean()) or 1.0
    out = []
    for it in items:
        x1, y1, x2, y2 = it["box"]
        sl = std[int(y1 * h):max(int(y2 * h), int(y1 * h) + 1), int(x1 * w):max(int(x2 * w), int(x1 * w) + 1)]
        if sl.size and float(sl.mean()) <= overall * ratio_thr:
            out.append({**it, "static_ratio": round(float(sl.mean()) / overall, 3)})
    return out


def pad_logo(b: List[float]) -> List[float]:
    """Ô logo vision trả thường KHÍT sát nét chữ: nội suy đúng ô đó để lại vệt viền chữ
    (đo 28/9: nhãn góc 20 px còn đọc lờ mờ). Nới ~1.2 % ngang, 40 % chiều cao dọc (≥1.2 %)."""
    py = max(0.012, (b[3] - b[1]) * 0.4)
    return [round(max(0.0, b[0] - 0.012), 4), round(max(0.0, b[1] - py), 4),
            round(min(1.0, b[2] + 0.012), 4), round(min(1.0, b[3] + py), 4)]


def delogo_graph(boxes: List[List[float]], w: int, h: int, inp: str, out: str) -> str:
    """Nội suy NHIỀU ô trong một chuỗi filter."""
    parts = []
    for b in boxes:
        px = to_pixels(b, w, h)
        dx, dy = max(1, min(px["x"], w - 3)), max(1, min(px["y"], h - 3))
        dw, dh = max(1, min(px["w"], w - dx - 1)), max(1, min(px["h"], h - dy - 1))
        parts.append(f"delogo=x={dx}:y={dy}:w={dw}:h={dh}")
    return f"[{inp}]{','.join(parts)}[{out}]"


def aggregate(boxes: List[List[float]], pad_x: float = 0.03, pad_y: float = 0.015,
              min_w: float = MIN_BAND_W) -> Optional[List[float]]:
    if not boxes:
        return None
    x1 = max(0.0, min(b[0] for b in boxes) - pad_x)
    x2 = min(1.0, max(b[2] for b in boxes) + pad_x)
    y1 = max(0.0, median(b[1] for b in boxes) - pad_y)
    y2 = min(1.0, median(b[3] for b in boxes) + pad_y)
    if x2 - x1 < min_w:
        # chữ căn giữa → nới ĐỐI XỨNG quanh tâm; lệch tâm thì đẩy vào trong khung
        half = min_w / 2.0
        cx = min(max((x1 + x2) / 2.0, half), 1.0 - half)
        x1, x2 = max(0.0, cx - half), min(1.0, cx + half)
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def to_pixels(box: List[float], w: int, h: int) -> Dict[str, int]:
    """Ô 0..1 → pixel CHẴN {x,y,w,h} (ffmpeg cần số chẵn)."""
    x = int(box[0] * w) & ~1
    y = int(box[1] * h) & ~1
    bw = (int((box[2] - box[0]) * w) + 1) & ~1
    bh = (int((box[3] - box[1]) * h) + 1) & ~1
    return {"x": x, "y": y, "w": max(2, min(bw, w - x)), "h": max(2, min(bh, h - y))}


def cover_graph(box: List[float], w: int, h: int, mode: str, inp: str = "0:v",
                out: str = "vc", strength: int = 24) -> str:
    """Mảnh filter_complex che ô `box` trên luồng [inp] → [out]."""
    px = to_pixels(box, w, h)
    x, y, bw, bh = px["x"], px["y"], px["w"], px["h"]
    if mode == "delogo":
        # delogo đòi ô nằm GỌN trong khung, chừa ≥1px mỗi phía
        return delogo_graph([box], w, h, inp, out)
    # Nới vùng cắt F px mỗi phía cho biên tan; mặt nạ = ô trắng đúng box, gblur → mép nhoè
    F = max(12, min(int(round(bh * 0.5)), 64)) & ~1
    cx, cy = max(0, x - F) & ~1, max(0, y - F) & ~1
    cw, ch = max(2, min(w - cx, bw + 2 * F) & ~1), max(2, min(h - cy, bh + 2 * F) & ~1)
    ox, oy = x - cx, y - cy
    if mode == "blur":
        eff = f"gblur=sigma={max(6, strength)}"
    elif mode == "fill":
        eff = f"gblur=sigma=26,drawbox=0:0:{cw}:{ch}:color=black@0.72:t=fill"
    else:     # pixel (khảm)
        div = max(2, strength // 2)
        eff = f"scale={max(2, cw // div)}:{max(2, ch // div)}:flags=neighbor,scale={cw}:{ch}:flags=neighbor"
    return (
        f"[{inp}]split[cvbase][cvsrc];"
        f"[cvsrc]crop={cw}:{ch}:{cx}:{cy},split[cvreg][cvregm];"
        f"[cvreg]{eff}[cvfg0];"
        f"[cvregm]drawbox=0:0:{cw}:{ch}:color=black:t=fill,"
        f"drawbox={ox}:{oy}:{bw}:{bh}:color=white:t=fill,format=gray,gblur=sigma={max(4, F // 2)}[cvmask];"
        f"[cvfg0][cvmask]alphamerge[cvfg];"
        f"[cvbase][cvfg]overlay={cx}:{cy}[{out}]"
    )


def _gemini_keys() -> List[str]:
    try:
        from tubecli.extensions.cloud_api.extension import key_manager

        return [e["key"] for e in (key_manager._keys.get("gemini", {}) or {}).values()
                if isinstance(e, dict) and e.get("active") and e.get("key")]
    except Exception:      # noqa: BLE001
        return []


def _detect_frame_blocking(img: bytes, keys: List[str], start: int) -> Optional[Tuple[List[Dict], List[Dict]]]:
    """Một khung → (phụ đề, logo); None = mọi khoá thử đều hỏng (khác ([], []) = không có gì)."""
    import requests

    b64 = base64.b64encode(img).decode()
    payload = {"contents": [{"parts": [{"text": _DETECT_PROMPT},
                                       {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
               "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2048}}
    for k in range(min(_KEY_TRIES, len(keys))):
        key = keys[(start + k) % len(keys)]
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/"
                              f"{GEMINI_MODEL}:generateContent?key={key}", json=payload, timeout=90)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue      # 429/403 → khoá sau
        cands = (r.json() or {}).get("candidates") or []
        if not cands:
            return [], []
        text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts") or [])
        return parse_detection(text)
    return None


async def _sample_frames(ffmpeg: str, video: str, dur: float, tmp: Path) -> List[bytes]:
    from tubecli.core.reup_dub import _run_ff

    lo, hi = dur * 0.05, dur * 0.95
    step = (hi - lo) / SAMPLE_FRAMES
    frames: List[bytes] = []
    for i in range(SAMPLE_FRAMES):
        out = tmp / f"f{i:02d}.jpg"
        t = max(0.0, lo + step * (i + 0.5))
        r = await _run_ff([ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
                           "-vf", "scale=720:-2", "-q:v", "3", str(out)], 60)
        if r.returncode == 0 and out.exists():
            frames.append(out.read_bytes())
    return frames


async def detect_subtitle_box(video: str, ffmpeg: str, workdir: Path) -> Dict:
    """{found, box, overlays, frames_with_text, total, reason}. `overlays` = logo/watermark
    cố định ĐÃ qua 3 cửa lọc ([{box, kind, text, seen, total}]). Không bao giờ ném lỗi — dò
    hỏng thì found=False, overlays=[] và video giữ nguyên, như stage_clean bên ReupDouyin."""
    from tubecli.core.reup_dub import media_duration

    keys = _gemini_keys()
    if not keys:
        return {"found": False, "overlays": [], "reason": "no_vision_key"}
    tmp = workdir / f"cover_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        dur = await media_duration(video, ffmpeg) or 1.0
        frames = await _sample_frames(ffmpeg, video, dur, tmp)
        if not frames:
            return {"found": False, "overlays": [], "reason": "no_frames"}
        # 6 khung song song, mỗi khung bắt đầu ở một khoá khác → không dồn một khoá
        res = await asyncio.gather(*(asyncio.to_thread(_detect_frame_blocking, f, keys, i)
                                     for i, f in enumerate(frames)))
        answered = [r for r in res if r is not None]
        if not answered:
            return {"found": False, "overlays": [], "reason": "vision_failed"}
        boxes = []
        for subs, _ in answered:
            best = max(subs, key=lambda d: (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]),
                       default=None)
            if best:
                boxes.append(best["box"])
        box = aggregate(boxes)
        found = box is not None and len(boxes) >= max(1, len(frames) // 3)
        overlays: List[Dict] = []
        try:
            ok_frames = [f for f, r in zip(frames, res) if r is not None]
            n = len(answered)
            every = filter_by_size(cluster_overlays([o for _, o in answered], n, need=1))
            strong_need = max(2, int(round(n * 0.6)))
            # Logo NHỎ thì vision hay sót: đo 28/9 (video 1024×576, chữ góc cao ~20 px) Gemini
            # chỉ thấy ở 1/6 khung, cửa bền của bản gốc loại sạch. Bằng chứng thay thế = pixel:
            # ô thấy ít khung phải TĨNH hẳn (≤ 0.7 mặt bằng) — logo đo được 0.20–0.62, cảnh vật
            # ở góc/rìa 0.96–1.34. Ô thấy đủ khung giữ ngưỡng 0.95 như bản gốc.
            overlays = (verify_static([o for o in every if o["seen"] >= strong_need], ok_frames)
                        + verify_static([o for o in every if o["seen"] < strong_need], ok_frames,
                                        ratio_thr=STRICT_STATIC))
            if box:     # không trùng vùng phụ đề
                overlays = [o for o in overlays if _iou(o["box"], box) < 0.3]
            overlays = [{**o, "box": pad_logo(o["box"])} for o in overlays]
        except Exception as e:      # noqa: BLE001
            logger.warning("lọc logo lỗi (bỏ qua): %s", e)
            overlays = []
        return {"found": bool(found), "box": box if found else None, "overlays": overlays,
                "frames_with_text": len(boxes), "total": len(frames)}
    except Exception as e:      # noqa: BLE001
        logger.warning("dò phụ đề gốc lỗi (bỏ qua): %s", e)
        return {"found": False, "overlays": [], "reason": "error"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
