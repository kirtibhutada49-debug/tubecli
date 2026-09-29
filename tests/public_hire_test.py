# -*- coding: utf-8 -*-
"""Việc THUÊ từ Chợ mẫu — phía máy (core/public_hire + cài đặt hire trong public_agents).

  1. normalise: bật/tắt, giá kẹp, kiểu tính job|minute, trần phút, danh sách mẫu;
     agent «chỉ cho thuê» (không skill chat) hợp lệ; bật mà không mẫu → no_hire_presets
  2. public_entries: agent chỉ-cho-thuê vẫn được đẩy lên Town
  3. _profile_row: khối hire {on, price, unit, minutes_max, presets[{n,c}]} — mã Chợ tra từ
     market_links.json; skill 'content.video' chỉ THÊM VÀO HỒ SƠ đẩy, không vào skill chat
  4. receive: từ chối đúng mã (agent lạ / hire tắt / thiếu mẫu / skill lạ); nhận lại cùng
     mã việc không mở việc thứ hai
  5. _run: chuỗi báo cáo running→ready kèm SỐ GIÂY + file; task hỏng → failed; cloud đóng
     (closed) → ngừng theo; file_for phục vụ đúng file
Run:  python tests/public_hire_test.py        (exit 0 = pass)
"""
import asyncio
import json
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tubecli.core import public_agents as pa   # noqa: E402
from tubecli.core import public_hire as ph     # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {str(detail)[:220]}")


# ── 1. normalise ─────────────────────────────────────────────────────────────
pa._profile_names = lambda: {"shared"}
n = pa.normalise({"name": "Tho Video", "enabled": True, "skills": ["capcut.tts"],
                  "hire_on": True, "hire_price": 999999999, "hire_unit": "minute",
                  "hire_minutes_max": 999, "hire_presets": ["Tin Công Nghệ", "Tin Công Nghệ", "  ", "Mẫu B"]})
check("hire: giá kẹp trần, phút kẹp 1–60, mẫu bỏ trùng/rỗng",
      n["hire_price"] == 500000 and n["hire_unit"] == "minute" and n["hire_minutes_max"] == 60
      and n["hire_presets"] == ["Tin Công Nghệ", "Mẫu B"], n)
check("kiểu tính lạ → job", pa.normalise({"name": "Tho Video", "hire_unit": "xu/phut"})["hire_unit"] == "job")
only = pa.normalise({"name": "Tho Video", "enabled": True, "skills": [],
                     "hire_on": True, "hire_price": 100, "hire_presets": ["Mẫu A"]})
check("agent CHỈ CHO THUÊ (không skill chat) vẫn bật được", only["enabled"] and only["skills"] == [])
try:
    pa.normalise({"name": "Tho Video", "enabled": True, "skills": [], "hire_on": True, "hire_presets": []})
    check("bật nhận việc mà không chọn mẫu → lỗi", False)
except ValueError as e:
    check("bật nhận việc mà không chọn mẫu → no_hire_presets", str(e) == "no_hire_presets")
kept = pa.normalise({"name": "Tho Video", "skills": ["capcut.tts"]}, old=n)
check("client cũ không gửi trường hire → giữ nguyên bản đang lưu",
      kept["hire_on"] and kept["hire_presets"] == ["Tin Công Nghệ", "Mẫu B"] and kept["hire_unit"] == "minute")

# ── 2+3. public_entries + _profile_row ───────────────────────────────────────
pa._agents_by_id = lambda: {"A1": object(), "A2": object()}
pa._load_all_real = pa._load_all
pa._load_all = lambda: {"A1": dict(only, name="Chi Cho Thue"),
                        "A2": {"enabled": True, "skills": ["capcut.tts"], "name": "Chat Thuong"}}
ents = pa.public_entries()
check("agent chỉ-cho-thuê vẫn nằm trong danh sách đẩy", any(e["agent_id"] == "A1" for e in ents), ents)
pa._market_links = lambda: {"Mẫu A": "tplAAA111", "Tin Công Nghệ": "tplTIN01"}
e1 = next(e for e in ents if e["agent_id"] == "A1")
row = pa._profile_row(e1)
check("hồ sơ đẩy mang khối hire + mã Chợ tra từ market_links",
      row["hire"]["on"] and row["hire"]["presets"] == [{"n": "Mẫu A", "c": "tplAAA111"}]
      and row["hire"]["price"] == 100 and row["hire"]["unit"] == "job", row.get("hire"))
check("skill content.video CHỈ thêm vào hồ sơ đẩy, không vào skill chat",
      "content.video" in row["skills"] and "content.video" not in e1["settings"]["skills"])
e2 = next(e for e in ents if e["agent_id"] == "A2")
check("agent không đụng hire → hồ sơ không mang khối hire", "hire" not in pa._profile_row(e2))

# ── 4. receive ───────────────────────────────────────────────────────────────
reports = []
ph._report_blocking = lambda code, body: (reports.append(dict(body)), {"status": body.get("status")})[1]
ph._studio_preset_exists = lambda name: name != "Mẫu Chưa Cài"
_tmp = tempfile.mkdtemp(prefix="hire_")
ph._dir = lambda: _tmp
_ran = []


async def _fake_run(code):
    _ran.append(code)


_orig_run = ph._run
ph._run = _fake_run
H1 = ents[0]["hash"] if ents[0]["agent_id"] == "A1" else ents[1]["hash"]
pa_public_entries_real = pa.public_entries


def rc(payload):
    try:
        return asyncio.run(ph.receive(payload))
    except Exception as e:      # noqa: BLE001 — PublicSkillError mang .code
        return getattr(e, "code", str(e))


base = {"job": "abc123def456", "agent": H1, "skill": "content.video",
        "brief": "làm video về pin thể rắn", "preset": "Mẫu A", "unit": "minute",
        "minutes": 3, "price": 3000, "caller": "aaaa1111"}
check("nhận việc hợp lệ → ok + chạy nền", rc(base) == {"ok": True, "job": "abc123def456"}
      and _ran == ["abc123def456"], ph._jobs.get("abc123def456"))
check("cùng mã việc gọi lại → ok, KHÔNG mở việc thứ hai",
      rc(base) == {"ok": True, "job": "abc123def456"} and len(_ran) == 1)
check("agent lạ → agent_not_public", rc({**base, "job": "x" * 12, "agent": "f" * 16}) == "agent_not_public")
check("skill lạ → skill_unavailable", rc({**base, "job": "y" * 12, "skill": "layout.image"}) == "skill_unavailable")
check("mẫu agent không khai → template_missing", rc({**base, "job": "z" * 12, "preset": "Mẫu Lạ"}) == "template_missing")
check("mã việc sai dạng → bad_request", rc({**base, "job": "ABC!"}) == "bad_request")
# Giọng + tiêu đề khách chọn (form thuê 28/9): hợp lệ thì lưu vào việc, sai dạng thì
# BỎ LẶNG LẼ (video vẫn dựng bằng mặc định của mẫu, không từ chối việc).
rc({**base, "job": "v" * 12, "voice": "BV075_streaming", "title": "  Pin   thể rắn  2027 ", "ratio": "9:16"})
check("receive: giọng + tiêu đề + tỉ lệ hợp lệ lưu vào việc, tiêu đề nén khoảng trắng",
      ph._jobs["v" * 12]["voice"] == "BV075_streaming" and ph._jobs["v" * 12]["title"] == "Pin thể rắn 2027"
      and ph._jobs["v" * 12]["ratio"] == "9:16",
      ph._jobs.get("v" * 12))
# Độ dài máy nhắm (29/9: «auto thời lượng theo text, không ép»): 0 = tự động; vượt số phút đã
# trả → kẹp; không gửi / rác → như cũ (nhắm đúng minutes).
for _j, _t in (("ta" * 6, 0), ("tb" * 6, 7), ("tc" * 6, None), ("td" * 6, "abc"), ("te" * 6, 2)):
    rc({**base, "job": _j, **({} if _t is None else {"target": _t})})
check("receive: target 0 = tự động; 7 > 3 phút đã trả → 3; thiếu/rác → minutes; 2 → 2",
      [ph._jobs.get(j, {}).get("target") for j in ("ta" * 6, "tb" * 6, "tc" * 6, "td" * 6, "te" * 6)] == [0, 3, 3, 3, 2],
      [ph._jobs.get(j, {}).get("target") for j in ("ta" * 6, "tb" * 6, "tc" * 6, "td" * 6, "te" * 6)])
rc({**base, "job": "w" * 12, "voice": "bậy bạ có dấu!", "title": "", "ratio": "4:3"})
check("receive: giọng/tỉ lệ sai dạng → bỏ lặng lẽ, việc vẫn nhận",
      ph._jobs["w" * 12]["voice"] == "" and ph._jobs["w" * 12]["ratio"] == "" and ("w" * 12) in _ran)
pa._load_all = lambda: {"A1": dict(only, hire_on=False, skills=["capcut.tts"])}
check("hire tắt → hire_off", rc({**base, "job": "t" * 12}) == "hire_off")
pa._load_all = lambda: {"A1": dict(only, name="Chi Cho Thue"),
                        "A2": {"enabled": True, "skills": ["capcut.tts"], "name": "Chat Thuong"},
                        "A3": {"enabled": True, "skills": ["douyin.resolve"], "name": "Khong Lien Quan"}}
pa._agents_by_id = lambda: {"A1": object(), "A2": object(), "A3": object()}

# ── 4b. catalog giọng mở cho agent CHỈ-CHO-THUÊ (không skill chat capcut.tts) ─
_fake_tts = types.SimpleNamespace(catalog_blocking=lambda: {
    "languages": [{"code": "vi", "count": 1}],
    "voices": [{"id": "BV075_streaming", "lang": "vi", "name": "Ánh Xuân"}]})
_old_tts = sys.modules.get("tubecli.core.public_tts")
sys.modules["tubecli.core.public_tts"] = _fake_tts
H3 = next(e["hash"] for e in pa.public_entries() if e["agent_id"] == "A3")


def rcat(agent_hash):
    try:
        return asyncio.run(pa.catalog({"agent": agent_hash, "skill": "capcut.tts"}))
    except pa.PublicSkillError as e:
        return e.code


got = rcat(H1)
check("catalog: agent chỉ-cho-thuê vẫn xem được danh mục giọng (form thuê chọn giọng)",
      isinstance(got, dict) and got["voices"][0]["id"] == "BV075_streaming", got)
check("catalog: agent KHÔNG hire, không capcut.tts → vẫn skill_not_allowed",
      rcat(H3) == "skill_not_allowed")
if _old_tts is not None:
    sys.modules["tubecli.core.public_tts"] = _old_tts
else:
    sys.modules.pop("tubecli.core.public_tts", None)
pa._load_all = lambda: {"A1": dict(only, name="Chi Cho Thue"),
                        "A2": {"enabled": True, "skills": ["capcut.tts"], "name": "Chat Thuong"}}
pa._agents_by_id = lambda: {"A1": object(), "A2": object()}

# ── 5. _run trọn vòng (mock codex + ffprobe) ─────────────────────────────────
vid = os.path.join(_tmp, "video thue.mp4")
open(vid, "wb").write(b"0" * 2048)
state = {"phase": 0}


def fake_http(path):
    # Route /events THẬT lột sạch event checkpoint (codex/routes._public_events) —
    # fake phải giống thật, không được nhét checkpoint vào đây (bug 28/9: video dựng
    # xong vẫn «no_files» vì _run từng tin rằng /events mang video_path).
    if path.endswith("/events"):
        if state["phase"] >= 2:
            return {"events": [{"data": {"step": "render", "status": "success"}}]}
        return {"events": [{"data": {"step": "script", "status": "running"}}]}
    return {"task": {"status": ["running", "running", "review"][min(state["phase"], 2)]}}


class _FakePipeline(types.ModuleType):
    pass


seen_opts = {}
create_calls = {"n": 0}


def setup_run(monkey_status=None):
    reports.clear()
    ph._http_json = fake_http

    def _fake_create(aid, options=None, **k):
        create_calls["n"] += 1
        seen_opts.clear()
        seen_opts.update(options or {})
        return {"id": "task-123"}

    fake = types.SimpleNamespace(
        create_auto_task=_fake_create,
        media_seconds=lambda p: 95.0,
        # Checkpoint đọc TRONG TIẾN TRÌNH (không phải /events) — chỉ có khi task đã xong.
        _read_checkpoint=lambda tid: ({"video_path": vid} if state["phase"] >= 2 else {}),
        _base_url=lambda: "http://x")
    sys.modules["tubecli.extensions.content_video.pipeline"] = fake      # _run import từ đây
    return fake


async def run_fast(code, **extra):
    ph.POLL_SEC = 0.01
    ph.REPORT_MIN_GAP = 0
    # "_task" = vật KHÔNG json-hoá được, y như receive() treo asyncio.Task vào job.
    # Bug 28/9: _save nổ TypeError vì nó → mọi việc «failed» oan ngay sau khi tạo task.
    job = {"code": code, "agent_id": "A1", "preset": "Mẫu A", "brief": "x", "unit": "minute",
           "minutes": 3, "price": 3000, "status": "accepted", "task_id": "", "files": [],
           "paths": [], "seconds": 0, "at": 0, "_task": object(), **extra}
    ph._jobs[code] = job

    async def stepper():
        for _ in range(30):
            await asyncio.sleep(0.02)
            state["phase"] += 1
    await asyncio.gather(ph._run(code), stepper())
    return job


ph._run = _orig_run
_real_run_import = sys.modules.get("tubecli.extensions.content_video.pipeline")
setup_run()
state["phase"] = 0
job = asyncio.run(run_fast("job1ok123456", voice="BV075_streaming", title="Pin thể rắn", ratio="9:16"))
check("_run: giọng khách chọn → tts_engine capcut + capcut_speaker; tiêu đề + tỉ lệ vào options",
      seen_opts.get("tts_engine") == "capcut" and seen_opts.get("capcut_speaker") == "BV075_streaming"
      and seen_opts.get("title") == "Pin thể rắn"
      and seen_opts.get("aspect_ratio") == "9:16" and seen_opts.get("aspect_ratio_explicit") is True, seen_opts)
ready = [r for r in reports if r["status"] == "ready"]
check("_run: báo ready kèm file + SỐ GIÂY đo bằng ffprobe",
      job["status"] in ("reported", "delivered", "ready") and ready and ready[0]["seconds"] == 95
      and ready[0]["files"][0]["name"].endswith(".mp4") and ready[0]["files"][0]["bytes"] == 2048,
      reports[-1] if reports else "không có báo cáo")
check("_run: có báo tiến độ running trước khi giao",
      any(r["status"] == "running" for r in reports))
disk = json.load(open(os.path.join(_tmp, "job1ok123456.json"), encoding="utf-8"))
check("_save lọc khoá «_»: sổ trên đĩa ghi ĐƯỢC dù job mang _task, không còn báo hỏng oan",
      disk["status"] in ("reported", "delivered", "ready") and "_task" not in disk
      and disk["seconds"] == 95, disk.get("status"))

# Độ dài TỰ ĐỘNG (target 0): KHÔNG đặt target_words — pipeline đo chính bài dán; bài dài hơn
# số phút khách đã trả (3 phút = 450 chữ) thì kẹp về trần. Mốc cụ thể → target × 150.
for _code, _extra, _want in (
        ("jobauto00001", {"target": 0, "brief": "ý tưởng ngắn về số pi"}, None),
        ("jobauto00002", {"target": 0, "brief": " ".join(["chữ"] * 500)}, 450),
        ("jobauto00003", {"target": 2}, 300),
        ("jobauto00004", {}, 450)):                  # sổ việc trước .186: không có khoá target → như cũ
    setup_run()
    state["phase"] = 0
    asyncio.run(run_fast(_code, **_extra))
    check(f"_run {_code}: target_words = {_want}", seen_opts.get("target_words") == _want, seen_opts)

# task hỏng → failed
def fake_http_fail(path):
    return {"events": []} if path.endswith("/events") else {"task": {"status": "failed"}}


ph._http_json = fake_http_fail
state["phase"] = 0
job2 = asyncio.run(run_fast("job2fail0000"))
check("_run: task hỏng → báo failed để cloud hoàn tiền khách",
      job2["status"] == "failed" and any(r["status"] == "failed" for r in reports))

# cloud đóng việc giữa chừng → ngừng theo, không báo thêm
ph._http_json = fake_http


def closer(code, body):
    reports.append(dict(body))
    return {"closed": True, "status": "cancelled"}


ph._report_blocking = closer
reports.clear()
state["phase"] = 0
job3 = asyncio.run(run_fast("job3closed00"))
check("cloud đóng việc (khách huỷ) → máy ngừng theo, đóng sổ",
      job3["status"] == "closed" and len([r for r in reports if r["status"] == "ready"]) == 0)

# ── resume: restart giữa lượt dựng — bám tiếp task cũ, KHÔNG tạo task thứ hai ─
# Sổ trên đĩa còn cả việc «accepted» cũ của mục 4 (đời thật: đã bị cloud hoàn từ lâu)
# — cloud trả closed ở lượt báo đầu là chúng phải đóng sổ êm, không tạo task nào.
def _resume_report(code, body):
    reports.append(dict(body))
    if body["job"] != "jobresume001":
        return {"closed": True, "status": "cancelled"}
    return {"status": body.get("status")}


ph._report_blocking = _resume_report
ph._http_json = fake_http


async def _test_resume():
    state["phase"] = 5                     # codex «xong» ngay lượt poll đầu
    reports.clear()
    job = {"code": "jobresume001", "agent_id": "A1", "preset": "Mẫu A", "brief": "x",
           "unit": "job", "minutes": 3, "price": 0, "status": "running",
           "task_id": "task-123", "files": [], "paths": [], "seconds": 0, "at": 0}
    ph._save(job)
    ph._jobs.clear()
    n0 = create_calls["n"]
    ph.resume()
    await asyncio.gather(*[j["_task"] for j in ph._jobs.values() if j.get("_task")])
    return n0


_n0 = asyncio.run(_test_resume())
check("resume: nối việc running sau restart, KHÔNG tạo task thứ hai, việc cũ đóng êm",
      create_calls["n"] == _n0
      and ph._jobs["jobresume001"]["status"] in ("reported", "delivered", "ready")
      and any(r["status"] == "ready" for r in reports)
      and all(ph._jobs[c]["status"] == "closed" for c in ("abc123def456", "v" * 12, "w" * 12)),
      {c: j.get("status") for c, j in ph._jobs.items()})

# ── file_for ─────────────────────────────────────────────────────────────────
f = ph.file_for("job1ok123456", 0)
check("file_for trả đúng file đã giao", f and f["path"] == vid and f["name"].endswith(".mp4"), f)
check("file_for: số ngoài phạm vi / mã lạ → None",
      ph.file_for("job1ok123456", 5) is None and ph.file_for("khongcodau1", 0) is None
      and ph.file_for("job1ok123456", "x") is None)

if _real_run_import is not None:
    sys.modules["tubecli.extensions.content_video.pipeline"] = _real_run_import

print(f"\n{passed} pass, {failed} fail")
sys.exit(1 if failed else 0)
