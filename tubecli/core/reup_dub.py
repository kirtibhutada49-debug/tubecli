"""Lồng tiếng video từ phụ đề — bản gọn port từ ReupDouyin core/dubbing/srt_dubber.py
(user 28/9/2026: «tích hợp logic lồng tiếng của ReupDouyin»).

Giữ nguyên xương sống 4 bước + các hằng số đã trả giá thật bên đó:
  1. TTS từng câu bằng Edge-TTS (mp3, retry 1 lần, câu hỏng bỏ qua, cache voice+text).
  2. Ép khớp khung phụ đề: giọng dài hơn khung >2% thì atempo (chuỗi khi hệ số >2,
     trần 2.5x — nhanh hơn nữa khó nghe, chấp nhận tràn).
  3. Ghép track: anullsrc + adelay từng câu + amix normalize=0; quá 50 câu mix theo
     lô 30 (một lệnh amix quá nhiều đầu vào là ffmpeg gãy).
  4. Mux: "replace" thay hẳn âm gốc, "mix" giữ nền gốc hạ xuống bg_volume.
Kèm ghi (burn) phụ đề tuỳ chọn. Các chế độ nặng của bản gốc (duck/tách nhạc Demucs,
co giãn video) không mang sang — skill công khai cần gọn và đoán được.

Không ffprobe cũng chạy: imageio-ffmpeg không kèm ffprobe (bài học lõi .129) — đo thời
lượng bằng ffprobe nếu có, không thì bóc "Duration:" từ stderr của ffmpeg."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("reup_dub")

SAMPLE_RATE = 44100
FIT_TOLERANCE = 1.02
MAX_SPEEDUP = 2.5
BATCH_THRESHOLD = 50
BATCH_SIZE = 30

EDGE_VOICES = {
    "vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural",
    "en-US-JennyNeural", "en-US-GuyNeural",
    "ja-JP-NanamiNeural", "zh-CN-XiaoxiaoNeural",
}


def find_ffmpeg() -> Optional[str]:
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        from tubecli.extensions.video_downloader.routes import _get_ffmpeg_path

        return _get_ffmpeg_path()
    except Exception:      # noqa: BLE001
        return None


def _run(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # Lỗi mềm kiểu lệnh `timeout` — đừng giết cả job vì một bước quá hạn.
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout sau {timeout}s")


async def _run_ff(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_run, cmd, timeout)


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


async def media_duration(path: str, ffmpeg: Optional[str] = None) -> float:
    fp = shutil.which("ffprobe")
    if fp:
        r = await _run_ff([fp, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", path], 60)
        try:
            return float((r.stdout or "").strip())
        except (TypeError, ValueError):
            pass
    ff = ffmpeg or find_ffmpeg()
    if not ff:
        return 0.0
    r = await _run_ff([ff, "-i", path], 60)
    m = _DUR_RE.search((r.stderr or "") + (r.stdout or ""))
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _atempo_chain(factor: float) -> str:
    """atempo chỉ nhận 0.5..2 — hệ số lớn hơn phải xâu chuỗi."""
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


async def _edge_tts(text: str, voice: str, out_mp3: Path) -> bool:
    import edge_tts

    # «No audio was received» là lỗi chập chờn quen của Edge-TTS (đo 28/9: 1/3 câu
    # dính ngay trong test đầu) — 3 lần với backoff mới đáng gọi là retry.
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice).save(str(out_mp3))
            if out_mp3.exists() and out_mp3.stat().st_size > 200:
                return True
        except Exception as e:      # noqa: BLE001
            logger.warning("edge-tts lỗi (lần %d): %s", attempt + 1, str(e)[:120])
        await asyncio.sleep(0.8 * (attempt + 1))
    return False


async def _fit_segment(ffmpeg: str, mp3: Path, wav: Path, slot: float) -> bool:
    dur = await media_duration(str(mp3), ffmpeg)
    af = None
    if slot > 0 and dur > slot * FIT_TOLERANCE:
        af = _atempo_chain(min(dur / slot, MAX_SPEEDUP))
    cmd = [ffmpeg, "-y", "-i", str(mp3)]
    if af:
        cmd += ["-filter:a", af]
    cmd += ["-ar", str(SAMPLE_RATE), "-ac", "1", "-sample_fmt", "s16", str(wav)]
    r = await _run_ff(cmd, 120)
    if r.returncode != 0 and af:
        r = await _run_ff([ffmpeg, "-y", "-i", str(mp3), "-ar", str(SAMPLE_RATE),
                           "-ac", "1", "-sample_fmt", "s16", str(wav)], 120)
    return r.returncode == 0 and wav.exists()


async def _mix_chunk(ffmpeg: str, entries: List[tuple], total: float, out_wav: Path) -> bool:
    """entries: [(start_sec, wav_path)] — anullsrc nền + adelay + amix một lệnh."""
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-t", f"{total:.3f}",
           "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
    flt = []
    for i, (start, wav) in enumerate(entries):
        cmd += ["-i", str(wav)]
        ms = max(0, int(start * 1000))
        flt.append(f"[{i + 1}:a]adelay={ms}|{ms}[d{i}]")
    inputs = "[0:a]" + "".join(f"[d{i}]" for i in range(len(entries)))
    flt.append(f"{inputs}amix=inputs={len(entries) + 1}:normalize=0:duration=first[out]")
    cmd += ["-filter_complex", ";".join(flt), "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", "1", str(out_wav)]
    r = await _run_ff(cmd, int(max(300, total * 2)))
    return r.returncode == 0 and out_wav.exists()


def _srt_time(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(subtitles: List[Dict], path: Path) -> None:
    lines = []
    for i, s in enumerate(subtitles, 1):
        lines += [str(i), f"{_srt_time(float(s['start']))} --> {_srt_time(float(s['end']))}",
                  str(s.get("text") or "").strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _subtitles_filter_path(p: Path) -> str:
    """Đường dẫn cho filter subtitles trên Windows: \\ -> /, thoát dấu hai chấm ổ đĩa."""
    s = str(p).replace("\\", "/").replace(":", "\\:")
    return f"subtitles='{s}'"


async def dub_video(video_path: str, subtitles: List[Dict], voice: str,
                    output_path: str, mode: str = "replace", bg_volume: float = 0.15,
                    burn: bool = False,
                    progress: Optional[Callable[[str, int, int], None]] = None) -> Dict:
    """Trả {"status": "success", "output", "segments", "skipped"} hay {"status": "error", "message"}."""
    say = progress or (lambda phase, done, total: None)
    if voice not in EDGE_VOICES:
        return {"status": "error", "message": "voice_not_allowed"}
    subs = [s for s in (subtitles or [])
            if isinstance(s, dict) and str(s.get("text") or "").strip()
            and float(s.get("end", 0)) > float(s.get("start", 0))]
    if not subs:
        return {"status": "error", "message": "no_subtitles"}
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {"status": "error", "message": "no_ffmpeg"}

    video = Path(video_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f"dubtmp_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        video_dur = await media_duration(str(video), ffmpeg)
        total_dur = max(video_dur, max(float(s["end"]) for s in subs) + 0.5)

        # 1+2. TTS từng câu + ép khớp khung
        entries: List[tuple] = []
        skipped = 0
        for i, s in enumerate(subs):
            text = str(s["text"]).strip()
            key = hashlib.sha256(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
            mp3 = tmp / f"{key}.mp3"
            wav = tmp / f"seg_{i:04d}.wav"
            if not mp3.exists():
                if not await _edge_tts(text, voice, mp3):
                    skipped += 1
                    say("tts", i + 1, len(subs))
                    continue
            slot = float(s["end"]) - float(s["start"])
            if not await _fit_segment(ffmpeg, mp3, wav, slot):
                skipped += 1
            else:
                entries.append((float(s["start"]), wav))
            say("tts", i + 1, len(subs))
        if not entries:
            return {"status": "error", "message": "tts_failed"}

        # 3. Ghép track giọng — quá BATCH_THRESHOLD câu thì mix theo lô rồi mix các lô
        say("mix", 0, 1)
        voice_wav = tmp / "voice.wav"
        if len(entries) <= BATCH_THRESHOLD:
            ok = await _mix_chunk(ffmpeg, entries, total_dur, voice_wav)
        else:
            parts = []
            for bi in range(0, len(entries), BATCH_SIZE):
                pw = tmp / f"part_{bi}.wav"
                if not await _mix_chunk(ffmpeg, entries[bi:bi + BATCH_SIZE], total_dur, pw):
                    return {"status": "error", "message": "mix_failed"}
                parts.append(pw)
            ok = await _mix_chunk(ffmpeg, [(0.0, p) for p in parts], total_dur, voice_wav)
        if not ok:
            return {"status": "error", "message": "mix_failed"}
        say("mix", 1, 1)

        # 4. Mux vào video (replace / mix nền gốc)
        say("mux", 0, 1)
        enc_timeout = int(max(900, min(4 * 3600, total_dur * 3)))
        dubbed = tmp / "dubbed.mp4"
        if mode == "mix":
            cmd = [ffmpeg, "-y", "-i", str(video), "-i", str(voice_wav),
                   "-filter_complex",
                   f"[0:a]volume={max(0.0, min(1.0, bg_volume)):.2f}[bg];"
                   f"[bg][1:a]amix=inputs=2:normalize=0:duration=first[out]",
                   "-map", "0:v", "-map", "[out]", "-c:v", "copy",
                   "-c:a", "aac", "-b:a", "192k", "-shortest", str(dubbed)]
            r = await _run_ff(cmd, enc_timeout)
            if r.returncode != 0:
                # video không có audio gốc → lùi về replace
                mode = "replace"
        if mode != "mix" or not dubbed.exists():
            r = await _run_ff([ffmpeg, "-y", "-i", str(video), "-i", str(voice_wav),
                               "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                               "-c:a", "aac", "-b:a", "192k", "-shortest", str(dubbed)],
                              enc_timeout)
            if r.returncode != 0 or not dubbed.exists():
                return {"status": "error", "message": "mux_failed"}
        say("mux", 1, 1)

        # 5. Ghi phụ đề (tuỳ chọn) — bắt buộc re-encode video
        final = dubbed
        if burn:
            say("burn", 0, 1)
            srt = tmp / "subs.srt"
            write_srt(subs, srt)
            burned = tmp / "burned.mp4"
            r = await _run_ff([ffmpeg, "-y", "-i", str(dubbed),
                               "-vf", _subtitles_filter_path(srt),
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                               "-c:a", "copy", str(burned)], enc_timeout)
            if r.returncode == 0 and burned.exists():
                final = burned
            # burn hỏng thì vẫn giao bản đã lồng tiếng — đừng mất trắng cả job
            say("burn", 1, 1)

        shutil.move(str(final), str(out))
        return {"status": "success", "output": str(out),
                "segments": len(entries), "skipped": skipped}
    except Exception as e:      # noqa: BLE001
        logger.warning("dub_video lỗi: %s", e, exc_info=True)
        return {"status": "error", "message": f"dub_failed: {str(e)[:120]}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
