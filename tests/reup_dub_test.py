# -*- coding: utf-8 -*-
"""core/reup_dub.py — phần thuần (không ffmpeg, không mạng): tách dòng dài, dựng .ass,
chuỗi atempo, đọc khổ video. Chạy: python tests/reup_dub_test.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tubecli.core import reup_dub as rd  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


# ── Tách dòng dài ───────────────────────────────────────────────────────────
long_vi = ("Zhongke Shuguang đã thông báo qua tài khoản công khai chính thức vào ngày 10 tháng 7 "
           "rằng cụm siêu máy tính AI đã chính thức hoàn thành và được kết nối với mạng lưới quốc gia.")
out = rd.split_long_subtitles([{"start": 7.6, "end": 16.7, "text": long_vi}])
check("dòng dài bị cắt thành nhiều cue", len(out) >= 3, len(out))
check("mỗi mảnh ≤ ~56 ký tự (mồ côi gộp được tới 1.3x)", all(len(s["text"]) <= 73 for s in out),
      [len(s["text"]) for s in out])
check("không mất chữ nào", " ".join(s["text"] for s in out) == long_vi)
check("mốc liên tục: đầu = start gốc, cuối = end gốc, không chồng",
      out[0]["start"] == 7.6 and out[-1]["end"] == 16.7
      and all(a["end"] == b["start"] for a, b in zip(out, out[1:])))
short = [{"start": 0, "end": 2, "text": "Xin chào."}]
check("dòng ngắn giữ nguyên", rd.split_long_subtitles(short) == [{"start": 0.0, "end": 2.0, "text": "Xin chào."}])
cjk = rd.split_long_subtitles([{"start": 0, "end": 10,
                                "text": "今天我们来聊一聊人工智能的发展，以及它对我们生活的影响，还有未来的趋势和挑战。"}],
                              max_chars=20)
check("CJK cắt theo dấu câu", [s["text"][-1] for s in cjk] == ["，", "，", "。"], [s["text"] for s in cjk])
check("dòng hỏng (start sai kiểu) bị bỏ, không làm sập", rd.split_long_subtitles([{"start": "x", "text": "a"}]) == [])

# ── .ass theo khổ video ─────────────────────────────────────────────────────
ass = rd.build_ass([{"start": 0, "end": 2, "text": "Xin {\\b1}chào\ndòng hai"},
                    {"start": 2.5, "end": 9, "text": "x" * 200}], 576, 768)
check("PlayRes = đúng khổ video", "PlayResX: 576" in ass and "PlayResY: 768" in ass)
check("cỡ chữ gốc 5.5% cạnh ngắn (576 → 32)", ",Arial,32," in ass)
check("ngoặc nhọn của chữ không thành lệnh ASS", "{\\b1}" not in ass and "Xin (\\b1)chào" in ass, ass.splitlines()[-2])
check("xuống dòng → \\N", "chào\\Ndòng hai" in ass)
check("cue quá dài tự thu cỡ, sàn 14", "{\\fs14}xxxx" in ass)
land = rd.build_ass([{"start": 0, "end": 1, "text": "a"}], 1920, 1080)
check("video ngang: chữ theo cạnh ngắn (1080 → 59), lề 12%", ",Arial,59," in land and ",230,230," in land)
check("mốc giờ ASS", rd._ass_time(3725.456) == "1:02:05.46")

# ── atempo + khổ video ──────────────────────────────────────────────────────
check("atempo ≤2 một khâu", rd._atempo_chain(1.5) == "atempo=1.5000")
check("atempo >2 xâu chuỗi", rd._atempo_chain(2.5) == "atempo=2.0,atempo=1.2500")
m = rd._SIZE_RE.search("Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, "
                       "bt470bg/unknown/unknown, progressive), 576x768 [SAR 1:1 DAR 3:4], 161 kb/s")
check("đọc khổ video từ stderr ffmpeg (không nhầm mã codec 0x3163…)", m and m.groups() == ("576", "768"))

print(f"\n{passed} pass, {failed} fail")
sys.exit(1 if failed else 0)
