"""Mẫu (category='template') trên Chợ: CÀI thẳng vào Content Studio của máy này.

User 29/9/2026: «khi chủ server mua thì tải dữ liệu về máy». Trước đây trang Chợ của máy
liệt kê được mẫu nhưng nút Cài trả «Unknown category: template» — mẫu chỉ cài được bằng tay
trong Content Studio (Chợ › Đã mua › Cài vào Studio › hộp thoại Nhập mẫu). Giờ một chạm:

    Studio POST /market/install {code}   (tải gói đã mua về khu chờ — khoá Chợ của người dùng)
      → chờ việc nền tải xong
      → Studio POST /preset-bundle/import {upload_id, apply}  (nạp kho tranh, bố cục, mẫu)

Nhập theo mặc định AN TOÀN của Studio (mẫu mới → thêm; trùng y hệt → bỏ qua; trùng tên khác
nội dung → giữ cả hai, đổi tên bản nhập). «Cập nhật» (force_update) thì chọn thay thế đúng
những mẫu trùng tên. Sổ `content_studio/market_installed.json` nhớ mã Chợ → tên mẫu đã cài để
trang Chợ biết «Đã cài».
"""
import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

POLL_SEC = 1.5
DOWNLOAD_TIMEOUT_S = 1800        # gói mẫu kèm kho tranh có thể hàng trăm MB
IMPORT_TIMEOUT_S = 900


class TemplateInstallError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _studio_dir() -> str:
    from tubecli.config import DATA_DIR
    return os.path.join(str(DATA_DIR), "content_studio")


def _record_path() -> str:
    return os.path.join(_studio_dir(), "market_installed.json")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _records() -> Dict[str, Any]:
    d = _read_json(_record_path(), {})
    return d if isinstance(d, dict) else {}


def _record(public_id: str, title: str, version: str, saved: List[str]) -> None:
    recs = _records()
    old = recs.get(public_id) if isinstance(recs.get(public_id), dict) else {}
    names = sorted(set((old.get("presets") or []) + list(saved)))
    recs[public_id] = {"title": title, "version": version, "presets": names, "at": int(time.time())}
    os.makedirs(_studio_dir(), exist_ok=True)
    tmp = _record_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _record_path())


def _preset_names() -> set:
    rows = _read_json(os.path.join(_studio_dir(), "presets.json"), [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    return {str(r.get("name") or "") for r in rows if isinstance(r, dict)}


def installed(public_id: str, title: str) -> Dict[str, Any]:
    """Đã cài khi: máy này từng cài mã này từ Chợ, HOẶC chính máy này là nơi đăng mẫu
    (market_links: tên mẫu → mã), HOẶC Content Studio đã có mẫu trùng đúng tên."""
    rec = _records().get(public_id)
    if isinstance(rec, dict):
        return {"installed": True, "path": "content_studio", "local_version": str(rec.get("version") or "0.0.0")}
    links = _read_json(os.path.join(_studio_dir(), "market_links.json"), {})
    if isinstance(links, dict) and public_id in {str(v) for v in links.values()}:
        return {"installed": True, "path": "content_studio", "local_version": "0.0.0"}
    if title and title.strip() in _preset_names():
        return {"installed": True, "path": "content_studio", "local_version": "0.0.0"}
    return {"installed": False, "path": "", "local_version": "0.0.0"}


def _detail(r) -> str:
    try:
        d = r.json()
        return str(d.get("detail") or d.get("message") or d.get("error") or "")[:300]
    except ValueError:
        return (r.text or "")[:300]


async def install(public_id: str, title: str, token: str, version: str = "",
                  force_update: bool = False, base_url: Optional[str] = None,
                  client=None) -> Dict[str, Any]:
    """Tải gói mẫu đã mua về máy và nhập vào Content Studio. Trả {saved, plan counts…}."""
    import httpx

    if not token:
        raise TemplateInstallError("Sign in to the Market first", 401)
    if base_url is None:
        from tubecli.extensions.content_video.pipeline import _base_url
        base_url = _base_url()
    own = client is None
    c = client or httpx.AsyncClient(timeout=60)
    try:
        r = await c.post(f"{base_url}/api/v1/studio/market/install", json={"code": public_id},
                         headers={"X-Market-Token": token})
        if r.status_code == 404:
            raise TemplateInstallError(
                "Content Studio is not installed or turned off on this server — install it first, "
                "then install the template again.", 409)
        if r.status_code >= 400:
            raise TemplateInstallError(_detail(r) or f"Content Studio refused ({r.status_code})",
                                       r.status_code if r.status_code < 500 else 502)
        job_id = str((r.json() or {}).get("job") or "")
        if not job_id:
            raise TemplateInstallError("Content Studio did not start the download")

        deadline = time.monotonic() + DOWNLOAD_TIMEOUT_S
        result = None
        while time.monotonic() < deadline:
            jr = await c.get(f"{base_url}/api/v1/studio/market/jobs/{job_id}")
            job = (jr.json() or {}).get("job") or {} if jr.status_code == 200 else {}
            phase = str(job.get("phase") or "")
            if phase == "ready":
                result = job.get("result") or {}
                break
            if phase == "error":
                st = int(job.get("status") or 502)
                raise TemplateInstallError(str(job.get("error") or "Download failed"), st if 400 <= st < 600 else 502)
            if jr.status_code == 404:
                raise TemplateInstallError("Content Studio lost the download job (it restarted?) — try again")
            await asyncio.sleep(POLL_SEC)
        if result is None:
            raise TemplateInstallError("The template download took too long", 504)

        body: Dict[str, Any] = ({"upload_id": result["upload_id"]} if result.get("upload_id")
                                else {"bundle": result.get("bundle")})
        # Lượt xem trước → biết mẫu nào trùng tên; «Cập nhật» thì thay đúng những mẫu ấy.
        pr = await c.post(f"{base_url}/api/v1/studio/preset-bundle/import", json=dict(body, apply=False),
                          timeout=IMPORT_TIMEOUT_S)
        if pr.status_code >= 400:
            raise TemplateInstallError(_detail(pr) or "Content Studio could not read the package")
        plan = (pr.json() or {}).get("plan") or {}
        choices = {}
        if force_update:
            for it in plan.get("items") or []:
                if it.get("status") in ("conflict", "same"):
                    choices[str(it.get("index"))] = {"action": "replace"}
        ar = await c.post(f"{base_url}/api/v1/studio/preset-bundle/import",
                          json=dict(body, apply=True, choices=choices), timeout=IMPORT_TIMEOUT_S)
        if ar.status_code >= 400:
            raise TemplateInstallError(_detail(ar) or "Content Studio could not import the template")
        done = ar.json() or {}
    finally:
        if own:
            await c.aclose()
    saved = [str(n) for n in (done.get("saved") or [])]
    skipped = [str(it.get("name")) for it in ((done.get("plan") or {}).get("items") or []) if it.get("action") == "skip"]
    _record(public_id, title, version, saved + skipped)
    return {"saved": saved, "skipped": skipped, "layouts_added": done.get("layouts_added") or [],
            "errors": done.get("errors") or []}
