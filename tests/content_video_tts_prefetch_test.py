# Đọc giọng SONG SONG với bước vẽ ảnh: luồng nền bắt đầu ở _step_images, bước tts chờ nó rồi chỉ đọc nốt phần thiếu.
# Mọi HTTP đều giả (_get/_post/_put/_poll_studio/_storyboards/_post_audio_marks) — test không chạm Studio thật.
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tubecli.config as CFG
from tubecli.extensions.content_video import pipeline as P

CFG.DATA_DIR = tempfile.mkdtemp(prefix="cv-prefetch-")
P._preset_meta = lambda st: {}
P._agent_voice_default = lambda st: ("", "")
P._task_meta = lambda *a, **k: None
P.media_seconds = lambda p: 1.0
P._get = lambda path, timeout=60: (_ for _ in ()).throw(AssertionError(f"unexpected GET {path}"))
P._put = lambda path, payload, timeout=60: (_ for _ in ()).throw(AssertionError(f"unexpected PUT {path}"))


def fresh_state(ep, says):
    return {"episode_id": ep, "language": "de", "warnings": [], "_cancelled": lambda: False,
            "_say": lambda *a: says.append(a)}


# 1. edge: _step_images khởi động batch-tts ở nền; thẻ tts KHÔNG nhúc nhích khi đang vẽ; bước tts chờ rồi chạy lại batch
P.installed_extensions = lambda: {"tts_vibevoice": True}
P._canvas_kit_meta = lambda st: {}
P._fill_missing_prompts = lambda st: 0
shots = [{"id": 1, "narration_text": "Eins"}, {"id": 2, "narration_text": "Zwei"}]
P._storyboards = lambda ep: shots
posts, polls = [], []
gate = threading.Event()


def post(path, payload, timeout=60):
    posts.append((path, payload))
    if path.endswith("/batch-tts"):
        return {"task_id": f"tts{len(posts)}"}
    return {}       # gen-images không khởi động → _step_images dừng ngay sau khi đã bật luồng đọc trước


def poll(path, timeout, state, step, done_statuses=()):
    polls.append(path)
    state["_say"](step, "running", "1/2", 50)
    if len(polls) == 1:
        gate.wait(5)        # luồng nền «đang đọc» cho tới khi test mở cổng
        for s in shots:
            s["tts_audio_url"] = f"/api/v1/tts/audio/{s['id']}.mp3"
    return {"status": "done", "success": 2, "failed": 0}


P._post, P._poll_studio = post, poll
says = []
st = fresh_state(501, says)
try:
    P._step_images(st, {"tts_voice": "de-DE-FlorianMultilingualNeural"})
    raise SystemExit("gen-images returned no task_id — must raise")
except RuntimeError as e:
    assert "gen-images did not start" in str(e), e
box = st["_tts_prefetch"]
assert box["thread"].is_alive(), "voice must already be running while the pictures draw"
time.sleep(0.2)
assert not [a for a in says if a[0] == "tts"], f"background voice must not drive the tts card: {says}"
assert posts[0][0] == "/api/v1/studio/episodes/501/batch-tts", posts
assert posts[0][1] == {"voice_id": "de-DE-FlorianMultilingualNeural", "engine": "edge"}, posts[0]
threading.Timer(0.3, gate.set).start()
P._step_tts(st, {"tts_voice": "de-DE-FlorianMultilingualNeural"})
waits = [a for a in says if a[0] == "tts" and "voice started while drawing" in a[2]]
assert waits and waits[0][2] == "voice started while drawing · 1/2" and waits[0][3] == 50, waits
assert [p for p, _ in posts].count("/api/v1/studio/episodes/501/batch-tts") == 2, "main run re-asks (Studio skips voiced shots)"
assert st["tts_summary"] == "2 voiced (edge)" and st["tts_engine"] == "edge", st
assert "_tts_prefetch" not in st and 501 not in P._TTS_PREFETCH
print("1 edge       : voice starts with the images step, tts card quiet until its turn, then waits + tops up")

# 2. dự án CANVAS (bộ cảnh có thể gộp/chèn nhịp) và lượt tắt giọng → không đọc trước
started = []
real_start = P._start_tts_prefetch
P._start_tts_prefetch = lambda st, op: started.append(st["episode_id"])
P._canvas_kit_meta = lambda st: {"scene_kit": "chalk"}
P._post = lambda path, payload, timeout=60: {"skipped": "kit_draws_itself", "task_id": "x"} if "gen-images" in path else {}
try:
    P._step_images(fresh_state(502, []), {})
except Exception:
    pass
P._canvas_kit_meta = lambda st: {}
P._post = post
try:
    P._step_images(fresh_state(503, []), {"tts": False})
except RuntimeError:
    pass
assert started == [], started
P._start_tts_prefetch = real_start
# canvas trực tiếp ở hàm khởi động cũng bị chặn
P._canvas_kit_meta = lambda st: {"scene_kit": "chalk"}
s2 = fresh_state(504, [])
P._start_tts_prefetch(s2, {})
assert "_tts_prefetch" not in s2
P._canvas_kit_meta = lambda st: {}
print("2 skip       : canvas kits and voice-off runs never prefetch")

# 3. CapCut: luồng nền đọc hết → lượt chính không còn gì, mà tóm tắt vẫn đếm đủ (không phải «0 voiced»)
P.installed_extensions = lambda: {"capcut_tts": True}
P._get = lambda path, timeout=60: {"accounts": [{"email": "a@x.com", "enabled": True}]}
cshots = [{"id": 11, "storyboard_number": 1, "narration_text": "Hola uno"},
          {"id": 12, "storyboard_number": 2, "narration_text": "Hola dos"}]
P._storyboards = lambda ep: cshots
reads = []


def put(path, payload, timeout=60):
    sid = int(path.rsplit("/", 1)[1])
    for s in cshots:
        if s["id"] == sid:
            s.update(payload)
    return {}


P._put = put
P._post_audio_marks = lambda path, body, timeout=180: (reads.append(body["text"]) or (b"ID3" + b"\x00" * 2000), [])
P._capcut_voice_platform = lambda email, spk: "sami"
P._wants_word_marks = lambda st: True
st3 = fresh_state(505, [])
opts3 = {"tts_engine": "capcut", "capcut_speaker": "es_male"}
P._start_tts_prefetch(st3, opts3)
st3["_tts_prefetch"]["thread"].join(5)
assert reads == ["Hola uno", "Hola dos"], reads
P._step_tts(st3, opts3)
assert reads == ["Hola uno", "Hola dos"], f"main run must not read twice: {reads}"
assert st3["tts_summary"] == "2 voiced (CapCut)", st3["tts_summary"]
assert all(os.path.isfile(s["tts_audio_url"]) for s in cshots)
print("3 capcut     : background voices every shot; main run reads nothing and still reports 2 voiced")

# 4. CapCut: nhịp luồng nền đọc hỏng — lượt chính đọc lại nó; nhịp ấy hỏng lần nữa KHÔNG làm cả bước hỏng
for s in cshots:
    s.pop("tts_audio_url", None)
reads.clear()
P.TTS_RETRY_DELAY = 0
P._post_audio_marks = lambda path, body, timeout=180: (
    reads.append(body["text"]) or (_ for _ in ()).throw(RuntimeError("CapCut 502")) if body["text"] == "Hola dos"
    else (b"ID3" + b"\x00" * 2000, []))
st4 = fresh_state(506, [])
P._start_tts_prefetch(st4, opts3)
st4["_tts_prefetch"]["thread"].join(5)
P._step_tts(st4, opts3)
assert st4["tts_summary"] == "1 voiced (CapCut), 1 failed", st4["tts_summary"]
assert any("5-second still" in w for w in st4["warnings"]), st4["warnings"]
print("4 leftovers  : a shot the background run lost is retried by the main run; still failing ⇒ warning, not a crash")

# 5. luồng nền hỏng (thiếu extension) → lượt chính tự báo đúng lỗi của nó
P.installed_extensions = lambda: {}
st5 = fresh_state(507, [])
P._start_tts_prefetch(st5, {})
st5["_tts_prefetch"]["thread"].join(5)
assert "No TTS extension" in st5["_tts_prefetch"]["error"]
try:
    P._step_tts(st5, {})
    raise SystemExit("main run must raise its own error")
except RuntimeError as e:
    assert "No TTS extension" in str(e), e
print("5 failure    : background error is swallowed, the tts step reports it itself")

# 6. chạy lại khi luồng cũ còn sống → bám vào luồng cũ, không mở luồng thứ hai; huỷ khi đang chờ → dừng
P.installed_extensions = lambda: {"tts_vibevoice": True}
P._storyboards = lambda ep: shots
hold = threading.Event()
P._poll_studio = lambda *a, **k: hold.wait(5) and {"status": "done", "success": 2, "failed": 0}
first = fresh_state(508, [])
P._start_tts_prefetch(first, {})
again = fresh_state(508, [])
P._start_tts_prefetch(again, {})
assert again["_tts_prefetch"] is first["_tts_prefetch"], "a retry must attach to the running prefetch"
again["_cancelled"] = lambda: True
try:
    P._step_tts(again, {})
    raise SystemExit("cancel while waiting must raise")
except Exception as e:
    assert P._is_cancel(e), e
hold.set()
first["_tts_prefetch"]["thread"].join(5)
print("6 retry      : a second run attaches to the live prefetch; cancelling while waiting stops the step")
print("ALL OK")
