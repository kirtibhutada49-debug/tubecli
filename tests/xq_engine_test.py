# -*- coding: utf-8 -*-
"""core/xq_engine.py + mức Vừa/Khó của public_xiangqi. Phần thuần luôn chạy; phần gọi
engine thật chỉ chạy khi máy đã tải Fairy-Stockfish (data/engines/fairy_stockfish) — không
tự tải trong test. Chạy: python tests/xq_engine_test.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tubecli.core import xq_engine as E  # noqa: E402
from tubecli.core.public_xiangqi import describe_move  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


START = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
SHOT = "rnbakabnr/9/2c4c1/p1p1C1p1p/9/9/P1P1P1P1P/1C7/9/RNBAKABNR b - - 0 2"   # ảnh user 28/9

# ── Toạ độ: TubeCLI hàng 0–9, Fairy-Stockfish 1–10 ────────────────────────────
check("h2e2 → h3e3 (pháo đầu)", E.to_fsf("h2e2") == "h3e3")
check("hàng 10 của engine → hàng 9", E.from_fsf("h10g8") == "h9g7" and E.from_fsf("a1a10") == "a0a9")
check("đi-về không lệch", all(E.from_fsf(E.to_fsf(m)) == m for m in ("a0a9", "i9i0", "e4e5", "b7c7")))
check("rác → rỗng", E.to_fsf("z9z9") == "" and E.from_fsf("a11a1") == "" and E.from_fsf("") == "")

# ── Bảng tải: đủ 3 bản cho mỗi HĐH, mã băm đúng dạng ─────────────────────────
check("mỗi HĐH có bmi2 → modern → x86-64",
      all([n.split("x86-64")[1].split(".")[0] for n, _ in E._ASSETS[k]] == ["-bmi2", "-modern", ""]
          for k in ("windows", "linux")))
check("SHA-256 ghim đủ 64 hex", all(len(h) == 64 and int(h, 16) >= 0 for v in E._ASSETS.values() for _, h in v))

# ── Tả nước cho lời bình (model từng tả nhầm quân) ───────────────────────────
check("pháo đi", describe_move(SHOT, "h7h5") == "your cannon h7→h5")
check("pháo ăn tốt", describe_move(SHOT, "c7c3") == "your cannon c7→c3, capturing a soldier")
check("mã", describe_move(SHOT, "h9g7") == "your horse h9→g7")
check("ô lạ không làm sập", describe_move(SHOT, "zz") == "a move")

# ── Engine thật (chỉ khi đã có trên máy) ─────────────────────────────────────
installed = E._platform_key() and (E._dir() / "engine.txt").exists()
if installed:
    p = E.ensure(wait=30)
    check("engine đã cài dùng được ngay", p is not None, str(p))
    hard = E.analyse(SHOT, 1, movetime_ms=800)
    check("Khó: một nước, đúng kiểu toạ độ TubeCLI", len(hard) == 1 and len(hard[0][0]) == 4 and hard[0][0][1].isdigit(),
          hard)
    check("Khó KHÔNG đi nước tham tốt c7c3 của engine Python cũ", hard and hard[0][0] != "c7c3", hard)
    med = E.analyse(SHOT, 5, depth=4)
    check("Vừa: 5 nước kèm điểm THẬT (không phải chặn trên như engine Python)",
          len(med) == 5 and len({m for m, _ in med}) == 5, med)
    check("khai cuộc: engine ra nước hợp lý (pháo đầu / pháo trái / mã)",
          E.analyse(START, 1, movetime_ms=500)[0][0] in ("h2e2", "b2e2", "h0g2", "b0c2", "c3c4", "g3g4"))
else:
    print("(bỏ qua phần engine thật — máy chưa tải Fairy-Stockfish)")

print(f"\n{passed} pass, {failed} fail")
sys.exit(1 if failed else 0)
