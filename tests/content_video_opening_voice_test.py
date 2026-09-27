# -*- coding: utf-8 -*-
"""Giọng đọc đoạn MỞ ĐẦU + nhãn lời thoại «VOZ (Tên):» (27/9/2026).

User: «hiện tại làm video [thuỷ mặc] vào khán giả chỉ xem vài giây rồi thoát». Đo #81: giọng ngừng 0,7 s ngay sau hai
chữ đầu và cứ vài giây lại lặng ở mỗi «…». Và kịch bản thuỷ mặc mới có dòng «VOZ (Lao Tsé): …» — bộ lọc nhãn cũ
để nguyên, giọng đọc to cả «VOZ Lao Tsé». Cam kết:
  1. Nhãn «VOZ/VOICE/GIỌNG (Tên):» bị gỡ; «Voz:» trần (có thể là câu thật) và «la voz (del río)» giữa câu thì giữ.
  2. Nhịp mở đầu (scene.type «hook», hay «title» + cover) gửi cho giọng «…» thành dấu phẩy; nhịp khác giữ nguyên.
  3. Không đụng dấu mở câu tiếng Tây Ban Nha (¿ ¡), không để dấu phẩy thừa ở cuối.
Run: python tests/content_video_opening_voice_test.py   (exit 0 = pass; không gọi mạng)
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tubecli.extensions.content_video import pipeline as P      # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, "—", str(detail)[:200])


print("── 1. nhãn lời thoại ──")
cases = {
    'VOZ (Funcionario): "Maestro, los archivos ya no están."': '"Maestro, los archivos ya no están."',
    'VOZ (Lao Tsé): "Lo que hacía el archivero..."': '"Lo que hacía el archivero..."',
    "VOICE (Lao Tzu): Water does not argue.": "Water does not argue.",
    "GIỌNG (Lão Tử): Nước không tranh.": "Nước không tranh.",
    "Narrador: Hola": "Hola",
    "VO: hi": "hi",
    "Voz: esto es una frase real": "Voz: esto es una frase real",
    "La voz (del río) suena": "La voz (del río) suena",
}
for src, want in cases.items():
    got = P._strip_label(src)
    ok(got == want, f"{src[:36]!r} → {want[:30]!r}", got)

print("── 2. khoảng lặng ở nhịp mở đầu ──")
hook = {"narration_text": "Y a los lados... nada. Papel.", "metadata": {"scene": {"type": "hook"}}}
cover = {"narration_text": "Un camino… Una línea.", "metadata": json.dumps({"scene": {"type": "title", "cover": True}})}
plain = {"narration_text": "Y a los lados... nada.", "metadata": {"scene": {"type": "painting"}}}
title = {"narration_text": "A... b", "metadata": {"scene": {"type": "title"}}}
ok(P._spoken(hook) == "Y a los lados, nada. Papel.", "nhịp hook: «…» thành dấu phẩy", P._spoken(hook))
ok(P._spoken(cover) == "Un camino, Una línea.", "thẻ xem trước (title + cover, metadata dạng chuỗi JSON)", P._spoken(cover))
ok(P._spoken(plain) == "Y a los lados... nada." and P._spoken(title) == "A... b", "nhịp thường giữ nguyên nhịp ngừng của kịch bản")
ok(P._shot_narration(hook) == "Y a los lados... nada. Papel.", "lời của shot (đo độ phủ, phụ đề) KHÔNG đổi")
ok(P._spoken({"narration_text": 'VOZ (Lao Tsé): "Lo que sabe…"', "metadata": {"scene": {"type": "hook"}}}) == '"Lo que sabe."',
   "nhãn + «…» cuối cùng lúc", P._spoken({"narration_text": 'VOZ (Lao Tsé): "Lo que sabe…"', "metadata": {"scene": {"type": "hook"}}}))

print("── 3. dấu câu ──")
for src, want in {"Hola… ¿qué?": "Hola, ¿qué?", "¡Mira... esto!": "¡Mira, esto!", "x…": "x.", "…y entonces": "y entonces",
                  "A... . B": "A. B"}.items():
    ok(P.soften_pauses(src) == want, f"{src!r} → {want!r}", P.soften_pauses(src))

print()
print("=" * 62)
print(f"{PASS}/{PASS + FAIL} PASS" if not FAIL else f"{PASS}/{PASS + FAIL} PASS — {FAIL} HỎNG")
sys.exit(1 if FAIL else 0)
