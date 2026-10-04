# -*- coding: utf-8 -*-
"""Việc THUÊ «video quảng cáo từ ảnh» (pod.video) — phía máy: cài đặt hire_pod, hồ sơ đẩy, nhận việc kèm ảnh, chạy
pipe Pod Studio, giao file. Mọi HTTP (cloud, Pod Studio, Bảng việc) và Muse đều GIẢ — DATA_DIR tạm.

  1. normalise: giá/clip kẹp, mẫu bỏ trùng, cho gửi ảnh người mẫu mặc định BẬT, bật mà không mẫu → lỗi
  2. public_entries + _profile_row: agent chỉ nhận video quảng cáo vẫn đẩy; khối hire_pod + thẻ mẫu từ kho chung
  3. receive: ảnh hợp lệ lưu vào kho ảnh Pod; cam kết/không cho ảnh người mẫu/clip/ảnh hỏng/ảnh to/mẫu không có
     người mẫu/Muse chưa cài → đúng mã; gọi lại cùng mã không mở việc thứ hai
  4. _run_pod: xếp task Pod (nhãn AI BẬT, origin mã việc), báo «còn sống» khi đứng một bước, giao video + số giây;
     task hỏng / xếp hỏng → failed
Run:  python tests/public_hire_pod_test.py
"""
import asyncio
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMP = tempfile.mkdtemp(prefix="hire_pod_")
import tubecli.config as cfg  # noqa: E402
cfg.DATA_DIR = Path(TMP)
from tubecli.core import public_agents as pa   # noqa: E402
from tubecli.core import public_hire as ph     # noqa: E402
from tubecli.core import templates as T        # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {str(detail)[:260]}")


def jpg_b64(size=(64, 64), color=(200, 30, 30), fmt="JPEG"):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, fmt)
    return base64.b64encode(b.getvalue()).decode()


# kho mẫu chung: một mẫu CÓ người mẫu mặc định, một mẫu không
model_png = os.path.join(TMP, "model.png")
from PIL import Image as _Im  # noqa: E402
_Im.new("RGB", (40, 60), (10, 200, 200)).save(model_png)
T.save_section("Tóc xanh 3D", "ref_video", {"format": "short", "clips": 3, "aspect": "9:16", "style": "3d",
                                            "model_images": [model_png]}, origin="pod_studio")
T.save_section("Chỉ kiểu hình", "ref_video", {"format": "ad", "clips": 2, "aspect": "16:9", "style": "anime"}, origin="pod_studio")

# ── 1. normalise ─────────────────────────────────────────────────────────────
n = pa.normalise({"name": "Quang Cao AI", "enabled": True, "skills": [], "hire_pod_on": True, "hire_pod_price": 10 ** 9,
                  "hire_pod_clips_max": 99, "hire_pod_templates": ["Tóc xanh 3D", "tóc XANH 3d", " ", "Chỉ kiểu hình", "Mất rồi"]})
check("hire_pod: giá kẹp, clip kẹp 1–12, mẫu bỏ trùng (hoa thường), mặc định CHO gửi ảnh người mẫu",
      n["hire_pod_on"] and n["hire_pod_price"] == pa.HIRE_PRICE_MAX and n["hire_pod_clips_max"] == 12
      and n["hire_pod_templates"] == ["Tóc xanh 3D", "Chỉ kiểu hình", "Mất rồi"] and n["hire_pod_models"] is True, n)
check("agent CHỈ nhận video quảng cáo (không skill chat, không hire Content Studio) hợp lệ", n["enabled"] and n["skills"] == [])
try:
    pa.normalise({"name": "Quang Cao AI", "enabled": True, "hire_pod_on": True, "hire_pod_templates": []})
    check("bật mà không mẫu → lỗi", False)
except ValueError as e:
    check("bật mà không mẫu → no_hire_pod_templates", str(e) == "no_hire_pod_templates")
kept = pa.normalise({"name": "Quang Cao AI", "hire_pod_models": False}, old=n)
check("client không gửi trường → giữ bản đang lưu; tắt cho gửi ảnh người mẫu được",
      kept["hire_pod_on"] and kept["hire_pod_templates"] == n["hire_pod_templates"] and kept["hire_pod_models"] is False
      and kept["hire_pod_price"] == pa.HIRE_PRICE_MAX)

# ── 2. public_entries + _profile_row ─────────────────────────────────────────
settings = dict(n, hire_pod_price=50, hire_pod_clips_max=6)
pa._agents_by_id = lambda: {"P1": object()}
pa._load_all = lambda: {"P1": settings}
ents = pa.public_entries()
check("agent chỉ nhận video quảng cáo vẫn được đẩy lên Town", [e["agent_id"] for e in ents] == ["P1"], ents)
row = pa._profile_row(ents[0])
hp = row.get("hire_pod") or {}
check("hồ sơ đẩy: khối hire_pod + skill pod.video (KHÔNG vào skill chat)",
      hp.get("on") and hp.get("price") == 50 and hp.get("clips_max") == 6 and hp.get("models") is True
      and "pod.video" in row["skills"] and "pod.video" not in ents[0]["settings"]["skills"], row)
check("thẻ mẫu từ kho chung: kiểu/khung/clip/thể loại + có người mẫu mặc định; mẫu đã mất bị bỏ",
      hp.get("templates") == [{"n": "Tóc xanh 3D", "s": "3d", "r": "9:16", "k": 3, "f": "short", "m": True},
                              {"n": "Chỉ kiểu hình", "s": "anime", "r": "16:9", "k": 2, "f": "ad", "m": False}], hp.get("templates"))

# ── 3. receive ───────────────────────────────────────────────────────────────
reports = []
ph._report_blocking = lambda code, body: (reports.append(dict(body)), {"status": body.get("status")})[1]
ph._dir = lambda: os.path.join(TMP, "hire_jobs")
os.makedirs(ph._dir(), exist_ok=True)
_muse = types.SimpleNamespace(settings=lambda: {"profile": "chayagent"})
sys.modules["tubecli.core.muse"] = _muse
import tubecli.core as _core  # noqa: E402
_core.muse = _muse
_ran = []


async def _fake_run(code):
    _ran.append(code)
_orig_run = ph._run
ph._run = _fake_run
H = ents[0]["hash"]


def rc(payload):
    try:
        return asyncio.run(ph.receive(payload))
    except Exception as e:      # noqa: BLE001
        return getattr(e, "code", repr(e))


base = {"job": "pod111111111", "agent": H, "skill": "pod.video", "brief": "Áo khoác mới «Mặc vào là ấm»",
        "preset": "tóc xanh 3d", "unit": "clip", "clips": 3, "price": 150, "products": [{"type": "image/jpeg", "b64": jpg_b64()}]}
check("nhận việc chỉ ảnh sản phẩm (mẫu có người mẫu mặc định) → ok + chạy nền",
      rc(base) == {"ok": True, "job": "pod111111111"} and _ran == ["pod111111111"])
j = ph._jobs["pod111111111"]
check("việc lưu: kind pod, mẫu đúng tên gốc, 3 clip, ảnh sản phẩm nằm trong kho ảnh Pod",
      j["kind"] == "pod" and j["preset"] == "Tóc xanh 3D" and j["clips"] == 3 and j["unit"] == "clip"
      and len(j["products"]) == 1 and os.path.isfile(j["products"][0])
      and os.path.dirname(j["products"][0]) == os.path.join(TMP, "pod_studio", "gallery") and j["models"] == [], j)
check("gọi lại cùng mã → ok, không mở việc thứ hai", rc(base) == {"ok": True, "job": "pod111111111"} and len(_ran) == 1)
withm = {**base, "job": "pod222222222", "models": [{"b64": jpg_b64(fmt="PNG")}]}
check("ảnh người mẫu KHÔNG kèm cam kết → need_consent", rc(withm) == "need_consent")
check("ảnh người mẫu + cam kết → nhận, lưu ảnh người mẫu",
      rc({**withm, "consent": True}) == {"ok": True, "job": "pod222222222"} and ph._jobs["pod222222222"]["consent"] is True
      and ph._jobs["pod222222222"]["models"][0].endswith(".png"))
look = {**base, "job": "podl00000000", "scene": "  rainy   neon\nTokyo ", "character": "x" * 400}
check("tuỳ biến bối cảnh + nhân vật (3/10/2026): gọn dấu cách, cắt 300 ký tự; không gửi → rỗng",
      rc(look) == {"ok": True, "job": "podl00000000"} and ph._jobs["podl00000000"]["scene"] == "rainy neon Tokyo"
      and len(ph._jobs["podl00000000"]["character"]) == 300 and j["scene"] == "" and j["character"] == "")
vj = {**base, "job": "podv00000000", "voice": "ELEGANT", "voice_gender": "female", "voice_custom": " hơi  khàn ",
      "voices": [{"gender": "male", "voice": "nope", "voice_custom": "x" * 300}, {"gender": "?", "voice": "sweet"}]}
check("giọng khách chọn (3/10/2026 tối): kiểu kẹp theo bộ Pod, giới nam/nữ, mô tả gọn ≤200, voices[] theo ảnh",
      rc(vj) == {"ok": True, "job": "podv00000000"} and ph._jobs["podv00000000"]["voice"] == "elegant"
      and ph._jobs["podv00000000"]["voice_gender"] == "female" and ph._jobs["podv00000000"]["voice_custom"] == "hơi khàn"
      and ph._jobs["podv00000000"]["voices"] == [{"gender": "male", "voice_custom": "x" * 200}, {"voice": "sweet"}], ph._jobs["podv00000000"])
check("không chọn giọng → không có khoá", "voice" not in j and "voices" not in j)
check("loại nội dung (4/10/2026): drama giữ, rác → rỗng (theo mẫu)",
      rc({**base, "job": "podf00000000", "format": "DRAMA"}) == {"ok": True, "job": "podf00000000"}
      and ph._jobs["podf00000000"]["format"] == "drama" and rc({**base, "job": "podf00000001", "format": "sitcom"}) == {"ok": True, "job": "podf00000001"}
      and ph._jobs["podf00000001"]["format"] == "" and j["format"] == "")
check("quá trần clip của chủ → bad_clips", rc({**base, "job": "pod333333333", "clips": 7}) == "bad_clips")
check("0 clip → bad_clips", rc({**base, "job": "pod333333334", "clips": 0}) == "bad_clips")
check("mẫu agent không phục vụ → template_missing", rc({**base, "job": "pod444444444", "preset": "Edo"}) == "template_missing")
check("mẫu khai nhưng đã xoá khỏi kho → template_missing", rc({**base, "job": "pod444444445", "preset": "Mất rồi"}) == "template_missing")
check("base64 hỏng → bad_images", rc({**base, "job": "pod555555555", "products": [{"b64": "@@@"}]}) == "bad_images")
check("không phải ảnh → bad_images",
      rc({**base, "job": "pod555555556", "products": [{"b64": base64.b64encode(b"%PDF-1.4 hello").decode()}]}) == "bad_images")
ph.POD_IMG_MAX = 200
check("ảnh quá cỡ → image_too_large", rc({**base, "job": "pod555555557"}) == "image_too_large")
ph.POD_IMG_MAX = 3 * 1024 * 1024
check("quá 2 ảnh sản phẩm → bad_images", rc({**base, "job": "pod555555558", "products": [{"b64": jpg_b64()}] * 3}) == "bad_images")
check("mẫu KHÔNG có người mẫu mặc định + không gửi ảnh người → need_model",
      rc({**base, "job": "pod666666666", "preset": "Chỉ kiểu hình"}) == "need_model")
pa._load_all = lambda: {"P1": dict(settings, hire_pod_models=False)}
check("chủ KHÔNG cho gửi ảnh người mẫu → models_not_allowed", rc({**withm, "job": "pod777777777", "consent": True}) == "models_not_allowed")
pa._load_all = lambda: {"P1": dict(settings, hire_pod_on=False, skills=["capcut.tts"])}
check("tắt nhận video quảng cáo → hire_off", rc({**base, "job": "pod888888888"}) == "hire_off")
pa._load_all = lambda: {"P1": settings}
_muse.settings = lambda: {"profile": ""}
check("chưa cài Muse → skill_unavailable (không nhận việc rồi để hỏng)", rc({**base, "job": "pod999999999"}) == "skill_unavailable")
_muse.settings = lambda: {"profile": "chayagent"}
check("việc bị từ chối không để lại ảnh lẻ của nó", not any("pod999999999" in f for f in os.listdir(os.path.join(TMP, "pod_studio", "gallery"))))

# ── 4. _run_pod ──────────────────────────────────────────────────────────────
ph._run = _orig_run
posted = []
state = {"phase": 0, "status": ["running", "running", "running", "review"]}


def fake_post(path, body):
    posted.append((path, dict(body)))
    return {"status": "queued", "task": {"id": "06abfeed-0000-7000-8000-000000000001"}}


def fake_http(path):
    if path.endswith("/events"):
        return {"events": [{"data": {"step": "clips", "status": "running"}}]}
    return {"task": {"status": state["status"][min(state["phase"], len(state["status"]) - 1)], "error": state.get("error", "")}}


final_mp4 = os.path.join(TMP, "final.mp4")
open(final_mp4, "wb").write(b"0" * 4096)
pdir = os.path.join(TMP, "pod_studio", "ref_video", "06abfeed-0000-7000-8000-000000000001")
os.makedirs(pdir, exist_ok=True)
json.dump({"final": {"path": final_mp4}}, open(os.path.join(pdir, "state.json"), "w"))
sys.modules["tubecli.extensions.content_video.pipeline"] = types.SimpleNamespace(
    media_seconds=lambda p: 30.04, _base_url=lambda: "http://127.0.0.1:1")
ph._http_post_json = fake_post
ph._http_json = fake_http


async def run_fast(code):
    ph.POLL_SEC, ph.REPORT_MIN_GAP, ph.HEARTBEAT_SEC = 0.01, 0, 0.03

    async def stepper():
        for _ in range(40):
            await asyncio.sleep(0.02)
            state["phase"] += 1
    await asyncio.gather(ph._run(code), stepper())
    return ph._jobs[code]


reports.clear()
ph._jobs["pod111111111"].update(scene="rainy neon Tokyo", character="silver hair", voice="warm", voices=[{"gender": "male"}], format="short")
job = asyncio.run(run_fast("pod111111111"))
path, body = posted[0]
check("xếp task Pod: route run, KHÔNG nhãn AI (chủ dự án bỏ 3/10/2026), origin mã việc, mẫu + số clip + ảnh sản phẩm, người mẫu để mẫu lo",
      path == "/api/v1/pod_studio/ref-video/run" and not body.get("watermark") and body["hire"] == "pod111111111"
      and body["template"] == "Tóc xanh 3D" and body["clips"] == 3 and body["model_images"] == []
      and body["product_images"] == j["products"] and body["created_by"] == "hire", body)
check("tuỳ biến của khách xuống Pod là scene_custom / character_custom",
      body["scene_custom"] == "rainy neon Tokyo" and body["character_custom"] == "silver hair", body)
check("giọng khách xuống Pod: voice + voices[]", body["voice"] == "warm" and body["voices"] == [{"gender": "male"}]
      and "voice_gender" not in body, body)
check("loại nội dung xuống Pod: format=short", body["format"] == "short", body)
ready = [r for r in reports if r["status"] == "ready"]
check("giao: báo ready kèm file + 30 s; sổ việc giữ đường dẫn video",
      ready and ready[0]["seconds"] == 30 and ready[0]["files"][0]["name"] == "video-quang-cao-pod111111111.mp4"
      and ready[0]["files"][0]["bytes"] == 4096 and job["paths"] == [final_mp4], reports[-1:])
check("đứng một bước (clips) vẫn báo «còn sống» nhiều lần", len([r for r in reports if r["status"] == "running"]) >= 3,
      [r["status"] for r in reports])
check("file_for phục vụ video đã giao", (ph.file_for("pod111111111", 0) or {}).get("path") == final_mp4)
state.update(phase=0, status=["running", "failed"])
reports.clear()
asyncio.run(run_fast("pod222222222"))
check("task Pod hỏng → báo failed (cloud hoàn tiền)", any(r["status"] == "failed" for r in reports)
      and ph._jobs["pod222222222"]["status"] == "failed")
check("không tuỳ biến → thân không mang khoá scene_custom / character_custom",
      "scene_custom" not in posted[-1][1] and "character_custom" not in posted[-1][1], posted[-1][1])
# Muse trả CHỮ thay cho ảnh (việc thuê #164, 3/10/2026) → mã image_refused + lời AI cho khách, đã lột đường dẫn/link
refusal = ("MuseError: Muse did not draw an image: I couldn't generate that image. If you'd like, I can do Cut 1 in the "
           r"elegant long áo dài instead (C:\tubecreate-vue\tubecli\data\x.jpg, https://muse.ai/c/1) — just say go.")
ph._jobs["podr00000000"] = {**ph._jobs["pod111111111"], "code": "podr00000000", "status": "running"}
state.update(phase=0, status=["running", "failed"], error=refusal)
reports.clear()
asyncio.run(run_fast("podr00000000"))
fr = [r for r in reports if r["status"] == "failed"]
check("Muse không vẽ ảnh → báo image_refused + lời AI (không đường dẫn, không link)",
      len(fr) == 1 and fr[0].get("err") == "image_refused" and "áo dài" in fr[0].get("note", "")
      and "tubecreate-vue" not in fr[0]["note"] and "muse.ai" not in fr[0]["note"] and len(fr[0]["note"]) <= 400, fr)
check("Muse không làm video → video_refused", ph.pod_failure("MuseError: Muse did not make a video: Sorry.") == ("video_refused", "Sorry."))
check("lỗi máy (ffmpeg, đường dẫn) → job_failed, KHÔNG gửi chữ", ph.pod_failure("ffmpeg failed: C:/x/y.mp4") == ("job_failed", ""))
state.update(phase=0, status=["running", "cancelled"], error=refusal)
ph._jobs["podc00000000"] = {**ph._jobs["pod111111111"], "code": "podc00000000", "status": "running"}
reports.clear()
asyncio.run(run_fast("podc00000000"))
check("chủ huỷ task → job_failed, không kèm lời AI", [(r.get("err"), r.get("note")) for r in reports if r["status"] == "failed"] == [("job_failed", None)], reports)
state.pop("error", None)
ph._http_post_json = lambda path, body: {"detail": "Add at least one model/character photo."}
ph._jobs["podq00000000"] = {**ph._jobs["pod111111111"], "code": "podq00000000", "task_id": "", "status": "accepted"}
reports.clear()
asyncio.run(run_fast("podq00000000"))
check("Pod từ chối xếp task → failed queue_failed", [r.get("err") for r in reports if r["status"] == "failed"] == ["queue_failed"], reports)

print(f"\n{passed} pass, {failed} fail")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
