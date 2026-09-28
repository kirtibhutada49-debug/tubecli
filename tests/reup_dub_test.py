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

# ── Mốc Gemini trôi: cue dồn cục ở đuôi (public_reup) ────────────────────────
from tubecli.core import public_reup as pr  # noqa: E402

drift = [{"start": i * 10.0, "end": i * 10.0 + 9, "text": "x" * 90} for i in range(14)]  # tới 139 s
drift[-1]["end"] = 148.33
drift += [{"start": 148.33, "end": 148.33, "text": "y" * 45} for _ in range(3)]
check("đếm cue dồn cục", pr.collapsed_count(drift) == 3)
fixed = pr.rescue_collapsed(drift, 148.33)
check("cứu xong: không còn cue dài 0, đủ số câu, đúng thứ tự chữ",
      pr.collapsed_count(fixed) == 0 and len(fixed) == len(drift)
      and [s["text"] for s in fixed] == [s["text"] for s in drift])
check("mọi mốc nằm trong video, tăng dần, không chồng",
      all(0 <= s["start"] < s["end"] <= 148.33 for s in fixed)
      and all(a["end"] <= b["start"] + 1e-6 for a, b in zip(fixed, fixed[1:])))
mid = [{"start": 0, "end": 5, "text": "a"}, {"start": 5, "end": 5, "text": "b"},
       {"start": 6, "end": 9, "text": "c"}]
check("dồn cục ở GIỮA thì để nguyên (không đoán)", pr.rescue_collapsed(mid, 10) == mid)
check("không có cue dồn cục thì trả nguyên", pr.rescue_collapsed(drift[:5], 148.33) == drift[:5])

print(f"\n{passed} pass, {failed} fail")
sys.exit(1 if failed else 0)
