import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from xhs_extractor import ExtractOptions, close_xhs_browser, extract_xhs_note, extract_xhs_note_with_browser, launch_xhs_browser

app = FastAPI(title="XHS URL Cleaner to Feishu Bitable", version="1.1.0")
PWA_CACHE_NAME = "xhs-feishu-pwa-v1"
PWA_APP_NAME = "小红书飞书助手"
PWA_THEME_COLOR = "#e00034"


class SubmitRequest(BaseModel):
    url: str = Field(..., description="Xiaohongshu share URL or text containing URL")


class BatchSubmitRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=100)


class XhsExtractRequest(BaseModel):
    url: str = Field(..., description="Xiaohongshu short/full share URL or text containing URL")
    timeout_ms: int = Field(25000, ge=5000, le=60000)
    settle_ms: int = Field(2500, ge=500, le=10000)
    scroll_times: int = Field(2, ge=0, le=8)


class XhsExtractSubmitRequest(XhsExtractRequest):
    upload_images: bool = Field(True, description="Download extracted images and upload them into Feishu attachment field")
    max_images: Optional[int] = Field(None, ge=0, le=50, description="Optional upload limit; default uploads all extracted note images")


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1)


@dataclass
class ExtractQueueItem:
    task_id: str
    body: XhsExtractSubmitRequest
    submitted_at: float
    status: str = "queued"
    result: Optional[dict[str, Any]] = None
    error: Optional[Any] = None


class FeishuTokenCache:
    token: Optional[str] = None
    expire_at: int = 0


class NotesFieldCache:
    image_field_id: Optional[str] = None


token_cache = FeishuTokenCache()
notes_field_cache = NotesFieldCache()
extract_field_cache = NotesFieldCache()
extract_queue: deque[ExtractQueueItem] = deque()
extract_queue_items: dict[str, ExtractQueueItem] = {}
extract_queue_lock = asyncio.Lock()
extract_queue_worker: Optional[asyncio.Task[Any]] = None


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def extract_first_url(text: str) -> str:
    """Accept either a pure URL or text containing a URL."""
    text = text.strip()
    match = re.search(r"https?://[^\s\"'<>]+", text)
    if match:
        return match.group(0).rstrip("，。,.;；、)")
    return text


XHS_SHORT_LINK_HOSTS = {
    "xhslink.com",
    "www.xhslink.com",
    "xhslink.cn",
    "www.xhslink.cn",
}
XHS_CLEAN_TARGET_HOSTS = {"www.xiaohongshu.com", "xiaohongshu.com"}
XHS_NOTE_PATH_MARKERS = ("/discovery/item/", "/explore/")
XHS_PRODUCT_PATH_MARKERS = ("/goods-detail/",)


def xhs_request_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def is_xhs_note_url(url: str) -> bool:
    parsed = urlparse(url or "")
    path = parsed.path or ""
    if any(marker in path for marker in XHS_NOTE_PATH_MARKERS):
        return True
    if parsed.netloc.lower().split(":", 1)[0] == "oia.xiaohongshu.com":
        query = parse_qs(parsed.query)
        candidates = [query.get("deeplink", [""])[0], query.get("open_url", [""])[0]]
        for candidate in candidates:
            if any(marker in candidate for marker in XHS_NOTE_PATH_MARKERS) or "xhsdiscover://item/" in candidate:
                return True
    return False


def resolve_xhs_url_for_detection(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in XHS_SHORT_LINK_HOSTS:
        return url
    try:
        resp = requests.get(url, allow_redirects=True, timeout=12, headers=xhs_request_headers())
    except requests.RequestException:
        return url
    return resp.url or url


def detect_xhs_input_type(raw: str) -> str:
    url = extract_first_url(raw)
    if is_xhs_note_url(url):
        return "note"
    resolved_url = resolve_xhs_url_for_detection(url)
    if is_xhs_note_url(resolved_url):
        return "note"
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    if host in XHS_SHORT_LINK_HOSTS:
        resolved_path = urlparse(resolved_url).path or ""
        if any(marker in resolved_path for marker in XHS_PRODUCT_PATH_MARKERS):
            return "product"
        return "note"
    return "product"


def normalize_url_without_query(url: str) -> str:
    """Keep only scheme + host + path. Drop query params and fragment."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Invalid URL")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def resolve_xhs_short_url(url: str) -> str:
    """Resolve xhslink.com/xhslink.cn share short URLs to their final Xiaohongshu web URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in XHS_SHORT_LINK_HOSTS:
        return url
    try:
        resp = requests.get(
            url,
            allow_redirects=True,
            timeout=12,
            headers=xhs_request_headers(),
        )
    except requests.RequestException as exc:
        raise ValueError(f"Failed to resolve Xiaohongshu short link: {exc}") from exc
    final_url = resp.url or url
    final_host = urlparse(final_url).netloc.lower().split(":", 1)[0]
    if final_host not in XHS_CLEAN_TARGET_HOSTS:
        raise ValueError(f"Unexpected Xiaohongshu short-link target: {final_host or final_url}")
    return final_url


def clean_url(raw: str) -> str:
    """Extract URL, expand xhslink short links, then drop query params and fragment."""
    url = extract_first_url(raw)
    return normalize_url_without_query(resolve_xhs_short_url(url))


def extract_path_id(cleaned_url: str) -> str:
    parsed = urlparse(cleaned_url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1] if parts else ""


def frontend_password() -> str:
    return os.getenv("FRONTEND_PASSWORD", "").strip() or os.getenv("SERVICE_API_KEY", "").strip()


def frontend_session_secret() -> str:
    return os.getenv("FRONTEND_SESSION_SECRET") or os.getenv("FEISHU_APP_SECRET") or frontend_password()


def frontend_token_ttl() -> int:
    try:
        return int(os.getenv("FRONTEND_TOKEN_TTL_SECONDS", "604800"))
    except ValueError:
        return 604800


def encode_token_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_token_part(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_frontend_payload(payload: str) -> str:
    return hmac.new(frontend_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_frontend_token() -> str:
    payload = encode_token_part(json.dumps({"exp": int(time.time()) + frontend_token_ttl()}, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{sign_frontend_payload(payload)}"


def valid_frontend_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(sign_frontend_payload(payload), signature):
        return False
    try:
        data = json.loads(decode_token_part(payload).decode("utf-8"))
    except Exception:
        return False
    return int(data.get("exp", 0)) > int(time.time())


def verify_api_key(x_api_key: Optional[str] = None, x_frontend_token: Optional[str] = None) -> None:
    password = frontend_password()
    if password:
        expected_api_key = os.getenv("SERVICE_API_KEY")
        if valid_frontend_token(x_frontend_token) or (expected_api_key and x_api_key == expected_api_key):
            return
        raise HTTPException(status_code=401, detail="Please log in first")


def get_feishu_tenant_access_token() -> str:
    now = int(time.time())
    if token_cache.token and token_cache.expire_at > now + 60:
        return token_cache.token

    app_id = get_env("FEISHU_APP_ID")
    app_secret = get_env("FEISHU_APP_SECRET")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feishu token response is not JSON: {resp.text}") from exc
    if resp.status_code != 200 or data.get("code") != 0:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed to get Feishu tenant_access_token", "response": data},
        )
    token_cache.token = data["tenant_access_token"]
    token_cache.expire_at = now + int(data.get("expire", 7200))
    return token_cache.token


def build_url_field_value(value: str) -> Any:
    field_type = os.getenv("FEISHU_FIELD_TYPE", "text").strip().lower()
    if field_type == "url":
        return {"text": value, "link": value}
    return value


def create_feishu_record(cleaned_url: str, original_url: str) -> dict[str, Any]:
    tenant_token = get_feishu_tenant_access_token()
    app_token = get_env("FEISHU_BITABLE_APP_TOKEN")
    table_id = get_env("FEISHU_TABLE_ID")
    link_field = get_env("FEISHU_FIELD_NAME", default="小红书链接")

    fields: dict[str, Any] = {link_field: build_url_field_value(cleaned_url)}

    original_field = os.getenv("FEISHU_ORIGINAL_URL_FIELD")
    if original_field:
        fields[original_field] = original_url

    id_field = os.getenv("FEISHU_XHS_ID_FIELD")
    if id_field:
        fields[id_field] = extract_path_id(cleaned_url)

    submitted_field = os.getenv("FEISHU_SUBMITTED_AT_FIELD")
    if submitted_field:
        # Feishu date field expects milliseconds timestamp.
        fields[submitted_field] = int(time.time() * 1000)

    resp = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        headers={
            "Authorization": f"Bearer {tenant_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"fields": fields},
        timeout=20,
    )
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feishu create-record response is not JSON: {resp.text}") from exc
    if resp.status_code != 200 or data.get("code") != 0:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed to create Feishu bitable record", "response": data, "fields": fields},
        )
    return data


def handle_one(raw_url: str) -> dict[str, Any]:
    try:
        cleaned = clean_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    feishu = create_feishu_record(cleaned, raw_url)
    record = feishu.get("data", {}).get("record", {})
    return {
        "ok": True,
        "original_url": raw_url,
        "clean_url": cleaned,
        "xhs_id": extract_path_id(cleaned),
        "feishu": {"record_id": record.get("record_id"), "raw": feishu},
    }


def get_notes_app_token() -> str:
    return get_env("FEISHU_NOTES_BITABLE_APP_TOKEN") or ""


def get_notes_table_id() -> str:
    return get_env("FEISHU_NOTES_TABLE_ID") or ""


def notes_field(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def notes_title_field() -> str:
    return notes_field("FEISHU_NOTES_TITLE_FIELD", "标题")


def notes_body_field() -> str:
    return notes_field("FEISHU_NOTES_BODY_FIELD", "正文")


def notes_tags_field() -> str:
    return notes_field("FEISHU_NOTES_TAGS_FIELD", "标签")


def notes_image_field() -> str:
    return notes_field("FEISHU_NOTES_IMAGE_FIELD", "图片")


def notes_fetched_field() -> str:
    return notes_field("FEISHU_NOTES_FETCHED_FIELD", "是否拉取")


def notes_category_field() -> str:
    return notes_field("FEISHU_NOTES_CATEGORY_FIELD", "笔记类别")


def notes_fetched_yes_value() -> str:
    return notes_field("FEISHU_NOTES_FETCHED_YES_VALUE", "是")


def get_extract_app_token() -> str:
    return get_env("FEISHU_EXTRACT_BITABLE_APP_TOKEN") or ""


def get_extract_table_id() -> str:
    return get_env("FEISHU_EXTRACT_TABLE_ID") or ""


def extract_field(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def extract_title_field() -> str:
    return extract_field("FEISHU_EXTRACT_TITLE_FIELD", "标题")


def extract_body_field() -> str:
    return extract_field("FEISHU_EXTRACT_BODY_FIELD", "正文")


def extract_tags_field() -> str:
    return extract_field("FEISHU_EXTRACT_TAGS_FIELD", "标签")


def extract_image_field() -> str:
    return extract_field("FEISHU_EXTRACT_IMAGE_FIELD", "图片")


def feishu_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_feishu_tenant_access_token()}"}


def feishu_json(resp: requests.Response, action: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feishu {action} response is not JSON: {resp.text}") from exc
    if resp.status_code != 200 or data.get("code") != 0:
        raise HTTPException(status_code=502, detail={"message": f"Failed to {action}", "response": data})
    return data


def list_notes_fields() -> list[dict[str, Any]]:
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    fields: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=feishu_headers(),
            params=params,
            timeout=20,
        )
        data = feishu_json(resp, "list Feishu note fields")
        payload = data.get("data", {})
        fields.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return fields
        page_token = payload.get("page_token", "")
        if not page_token:
            return fields


def get_notes_image_field_id() -> Optional[str]:
    if notes_field_cache.image_field_id:
        return notes_field_cache.image_field_id
    target_name = notes_image_field()
    for field in list_notes_fields():
        if field.get("field_name") == target_name:
            notes_field_cache.image_field_id = field.get("field_id")
            return notes_field_cache.image_field_id
    return None


def list_extract_fields() -> list[dict[str, Any]]:
    app_token = get_extract_app_token()
    table_id = get_extract_table_id()
    fields: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=feishu_headers(),
            params=params,
            timeout=20,
        )
        data = feishu_json(resp, "list Feishu extract fields")
        payload = data.get("data", {})
        fields.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return fields
        page_token = payload.get("page_token", "")
        if not page_token:
            return fields


def get_extract_image_field_id() -> Optional[str]:
    if extract_field_cache.image_field_id:
        return extract_field_cache.image_field_id
    target_name = extract_image_field()
    for field in list_extract_fields():
        if field.get("field_name") == target_name:
            extract_field_cache.image_field_id = field.get("field_id")
            return extract_field_cache.image_field_id
    return None


def list_notes_records() -> list[dict[str, Any]]:
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=feishu_headers(),
            params=params,
            timeout=20,
        )
        data = feishu_json(resp, "list Feishu note records")
        payload = data.get("data", {})
        records.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return records
        page_token = payload.get("page_token", "")
        if not page_token:
            return records


def get_note_record(record_id: str) -> dict[str, Any]:
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        headers=feishu_headers(),
        timeout=20,
    )
    return feishu_json(resp, "get Feishu note record").get("data", {}).get("record", {})


def update_note_fetched(record_id: str) -> dict[str, Any]:
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    resp = requests.put(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        headers={**feishu_headers(), "Content-Type": "application/json; charset=utf-8"},
        json={"fields": {notes_fetched_field(): notes_fetched_yes_value()}},
        timeout=20,
    )
    return feishu_json(resp, "update Feishu note fetched status")


def create_note_record(fields: dict[str, Any]) -> dict[str, Any]:
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        headers={**feishu_headers(), "Content-Type": "application/json; charset=utf-8"},
        json={"fields": fields},
        timeout=20,
    )
    return feishu_json(resp, "create Feishu note record").get("data", {}).get("record", {})


def create_extract_record(fields: dict[str, Any]) -> dict[str, Any]:
    app_token = get_extract_app_token()
    table_id = get_extract_table_id()
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        headers={**feishu_headers(), "Content-Type": "application/json; charset=utf-8"},
        json={"fields": fields},
        timeout=20,
    )
    return feishu_json(resp, "create Feishu extract record").get("data", {}).get("record", {})


def xhs_tags_to_text(tags: list[str]) -> str:
    cleaned = []
    seen = set()
    for tag in tags:
        item = str(tag).strip().lstrip("#")
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(f"#{item}")
    return " ".join(cleaned)


def strip_xhs_topic_tags(text: str) -> str:
    """Remove Xiaohongshu topic markers like #tag[话题]# from body text."""
    text = str(text or "")
    text = re.sub(r"\s*#[^#\r\n]{1,120}\[话题\]#", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def xhs_note_body(note: dict[str, Any]) -> str:
    return strip_xhs_topic_tags(note.get("description") or note.get("desc") or note.get("body") or "")


def build_xhs_note_fields(note: dict[str, Any], category: Optional[str] = None, fetched: Optional[str] = "否") -> dict[str, Any]:
    fields: dict[str, Any] = {
        notes_title_field(): note.get("title") or "",
        notes_body_field(): xhs_note_body(note),
        notes_tags_field(): xhs_tags_to_text(note.get("tags") or []),
    }
    category_value = (category or "").strip()
    if category_value:
        fields[notes_category_field()] = category_value
    fetched_value = "" if fetched is None else str(fetched).strip()
    if fetched_value:
        fields[notes_fetched_field()] = fetched_value
    return fields


def xhs_image_urls_to_text(image_urls: list[Any]) -> str:
    return "\n".join(str(url).strip() for url in image_urls if str(url).strip())


def build_xhs_extract_fields(note: dict[str, Any]) -> dict[str, Any]:
    return {
        extract_title_field(): note.get("title") or "",
        extract_body_field(): xhs_note_body(note),
        extract_tags_field(): xhs_tags_to_text(note.get("tags") or []),
        extract_image_field(): xhs_image_urls_to_text(note.get("image_urls") or []),
    }


def build_extract_submit_result(note: dict[str, Any]) -> dict[str, Any]:
    if not note.get("success"):
        raise HTTPException(status_code=502, detail={"message": "XHS note extraction did not return usable content", "reason": note.get("reason"), "note": note})
    record = create_extract_record(build_xhs_extract_fields(note))
    record_id = record.get("record_id")
    image_urls = [str(url).strip() for url in note.get("image_urls") or [] if str(url).strip()]
    return {
        "ok": True,
        "note": note,
        "feishu": {"record_id": record_id, "raw": record},
        "images": {"urls": image_urls},
    }


async def run_extract_submit(body: XhsExtractSubmitRequest, browser: Any = None) -> dict[str, Any]:
    options = ExtractOptions(timeout_ms=body.timeout_ms, settle_ms=body.settle_ms, scroll_times=body.scroll_times)
    if browser is None:
        note = await extract_xhs_note(body.url, options)
    else:
        note = await extract_xhs_note_with_browser(body.url, browser, options)
    return build_extract_submit_result(note)


def mark_smart_result(result: dict[str, Any], action: str, detected_type: str, message: str) -> dict[str, Any]:
    result["action"] = action
    result["detected_type"] = detected_type
    result["message"] = message
    return result


def handle_smart_submit_sync(raw_url: str) -> dict[str, Any]:
    detected_type = detect_xhs_input_type(raw_url)
    if detected_type == "note":
        result = asyncio.run(run_extract_submit(XhsExtractSubmitRequest(url=raw_url)))
        return mark_smart_result(result, "extract_note", "note", "识别为笔记内容，已抓取并写入抓取笔记内容表格。")
    return mark_smart_result(handle_one(raw_url), "clean", "product", "识别为商品链接，已清洗并写入链接表格。")


async def handle_smart_extract_submit(body: XhsExtractSubmitRequest) -> dict[str, Any]:
    detected_type = detect_xhs_input_type(body.url)
    if detected_type == "product":
        return mark_smart_result(handle_one(body.url), "clean", "product", "识别为商品链接，已清洗并写入链接表格。")
    result = await enqueue_extract_submit(body)
    return mark_smart_result(result, "extract_note", "note", result.get("message") or "识别为笔记内容，已加入抓取队列。")


def serialize_queue_error(exc: Exception) -> Any:
    if isinstance(exc, HTTPException):
        return exc.detail
    return str(exc)


async def process_extract_queue() -> None:
    global extract_queue_worker
    playwright = None
    browser = None
    try:
        while True:
            async with extract_queue_lock:
                if not extract_queue:
                    extract_queue_worker = None
                    break
                item = extract_queue[0]
                item.status = "running"
            try:
                if browser is None:
                    playwright, browser = await launch_xhs_browser(ExtractOptions())
                item.result = await run_extract_submit(item.body, browser)
                item.status = "completed"
            except Exception as exc:
                item.status = "failed"
                item.error = serialize_queue_error(exc)
                if browser is not None and playwright is not None:
                    await close_xhs_browser(playwright, browser)
                    playwright = None
                    browser = None
            finally:
                async with extract_queue_lock:
                    if extract_queue and extract_queue[0].task_id == item.task_id:
                        extract_queue.popleft()
                    else:
                        for queued in list(extract_queue):
                            if queued.task_id == item.task_id:
                                extract_queue.remove(queued)
                                break
    finally:
        if browser is not None and playwright is not None:
            await close_xhs_browser(playwright, browser)
        async with extract_queue_lock:
            if not extract_queue:
                extract_queue_worker = None


async def enqueue_extract_submit(body: XhsExtractSubmitRequest) -> dict[str, Any]:
    global extract_queue_worker
    input_url = extract_first_url(body.url)
    if not input_url.startswith(("http://", "https://")):
        raise ValueError("No http(s) URL found in input")
    task_id = uuid.uuid4().hex
    item = ExtractQueueItem(task_id=task_id, body=body, submitted_at=time.time())
    async with extract_queue_lock:
        extract_queue.append(item)
        extract_queue_items[task_id] = item
        position = len(extract_queue)
        if extract_queue_worker is None or extract_queue_worker.done():
            extract_queue_worker = asyncio.create_task(process_extract_queue())
    ahead = max(0, position - 1)
    message = "已加入队列，马上开始处理。" if ahead == 0 else f"已加入队列，当前排第 {position} 位，前方还有 {ahead} 个任务。"
    return {"ok": True, "queued": True, "task_id": task_id, "status": "queued", "position": position, "ahead": ahead, "message": message}


def guess_image_filename(url: str, index: int, content_type: str = "") -> str:
    suffix = "jpg"
    content_type = content_type.lower()
    if "png" in content_type:
        suffix = "png"
    elif "webp" in content_type:
        suffix = "webp"
    elif "gif" in content_type:
        suffix = "gif"
    else:
        path = urlparse(url).path.lower()
        for ext in ("jpg", "jpeg", "png", "webp", "gif"):
            if path.endswith(f".{ext}"):
                suffix = ext
                break
    return f"xhs-image-{index + 1}.{suffix}"


def download_xhs_image(url: str, index: int) -> tuple[str, bytes]:
    resp = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            "Referer": "https://www.xiaohongshu.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
        timeout=30,
    )
    if resp.status_code != 200 or not resp.content:
        raise HTTPException(status_code=502, detail={"message": "Failed to download XHS image", "url": url, "status_code": resp.status_code})
    return guess_image_filename(url, index, resp.headers.get("content-type", "")), resp.content


def extract_field_name_from_definition(data: dict[str, Any], fallback: str) -> str:
    field = data.get("data", {}).get("field") or data.get("data", {}).get("items") or data.get("field") or {}
    if isinstance(field, list):
        field = field[0] if field else {}
    if isinstance(field, dict):
        return str(field.get("field_name") or field.get("name") or fallback)
    return fallback


def get_base_field_definition(app_token: str, table_id: str, field_id: str, context: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/base/v3/bases/{app_token}/tables/{table_id}/fields/{field_id}",
        headers=feishu_headers(),
        timeout=20,
    )
    return feishu_json(resp, context)


def get_base_record(app_token: str, table_id: str, record_id: str, context: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records/{record_id}",
        headers=feishu_headers(),
        timeout=20,
    )
    return feishu_json(resp, context).get("data", {}).get("record", {})


def existing_attachment_items(record: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    fields = record.get("fields") if isinstance(record, dict) else {}
    value = fields.get(field_name) if isinstance(fields, dict) else []
    items: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return items
    for item in value:
        if not isinstance(item, dict):
            continue
        file_token = item.get("file_token") or item.get("token") or item.get("fileToken")
        if not file_token:
            continue
        name = item.get("name") or item.get("file_name") or item.get("filename") or "attachment"
        items.append({"deprecated_set_attachment": True, "file_token": file_token, "name": name})
    return items


def upload_bitable_attachment_media(app_token: str, filename: str, content: bytes, context: str) -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        headers=feishu_headers(),
        data={
            "file_name": filename,
            "parent_type": "bitable_file",
            "parent_node": app_token,
            "size": str(len(content)),
        },
        files={"file": (filename, content, "application/octet-stream")},
        timeout=60,
    )
    data = feishu_json(resp, context)
    return data.get("data", {}).get("file_token") or data.get("data", {}).get("token") or data.get("file_token") or ""


def patch_base_attachment_cell(app_token: str, table_id: str, record_id: str, field_name: str, attachments: list[dict[str, Any]], context: str) -> dict[str, Any]:
    resp = requests.patch(
        f"https://open.feishu.cn/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records/{record_id}",
        headers={**feishu_headers(), "Content-Type": "application/json; charset=utf-8"},
        json={field_name: attachments},
        timeout=30,
    )
    return feishu_json(resp, context)


def prepare_bitable_attachment_context(app_token: str, table_id: str, record_id: str, field_id: str, fallback_field_name: str, context: str) -> tuple[str, list[dict[str, Any]]]:
    field_data = get_base_field_definition(app_token, table_id, field_id, f"read Feishu {context} attachment field")
    field_name = extract_field_name_from_definition(field_data, fallback_field_name)
    record = get_base_record(app_token, table_id, record_id, f"read Feishu {context} record attachments")
    return field_name, existing_attachment_items(record, field_name)


def upload_note_attachment(record_id: str, field_id: str, filename: str, content: bytes) -> str:
    return upload_bitable_attachment_media(get_notes_app_token(), filename, content, "upload Feishu note attachment media")


def upload_xhs_images_to_note(record_id: str, image_urls: list[str], max_images: int) -> list[dict[str, Any]]:
    if max_images <= 0 or not image_urls:
        return []
    field_id = get_notes_image_field_id()
    if not field_id:
        raise HTTPException(status_code=400, detail=f"Feishu attachment field not found: {notes_image_field()}")
    app_token = get_notes_app_token()
    table_id = get_notes_table_id()
    field_name, merged_attachments = prepare_bitable_attachment_context(app_token, table_id, record_id, field_id, notes_image_field(), "note")
    uploaded = []
    cell_dirty = False
    for index, image_url in enumerate(image_urls[:max_images]):
        try:
            filename, content = download_xhs_image(image_url, index)
            file_token = upload_note_attachment(record_id, field_id, filename, content)
            if not file_token:
                raise HTTPException(status_code=502, detail="Feishu note attachment media upload returned empty file_token")
            merged_attachments.append({"deprecated_set_attachment": True, "file_token": file_token, "name": filename})
            cell_dirty = True
            uploaded.append({"index": index, "url": image_url, "name": filename, "file_token": file_token, "ok": True})
        except HTTPException as exc:
            uploaded.append({"index": index, "url": image_url, "ok": False, "error": exc.detail})
    if cell_dirty:
        patch_base_attachment_cell(app_token, table_id, record_id, field_name, merged_attachments, "patch Feishu note attachment cell")
    return uploaded


def upload_extract_attachment(record_id: str, field_id: str, filename: str, content: bytes) -> str:
    return upload_bitable_attachment_media(get_extract_app_token(), filename, content, "upload Feishu extract attachment media")


def upload_xhs_images_to_extract(record_id: str, image_urls: list[str], max_images: int) -> list[dict[str, Any]]:
    if max_images <= 0 or not image_urls:
        return []
    field_id = get_extract_image_field_id()
    if not field_id:
        raise HTTPException(status_code=400, detail=f"Feishu attachment field not found: {extract_image_field()}")
    app_token = get_extract_app_token()
    table_id = get_extract_table_id()
    field_name, merged_attachments = prepare_bitable_attachment_context(app_token, table_id, record_id, field_id, extract_image_field(), "extract")
    uploaded = []
    cell_dirty = False
    for index, image_url in enumerate(image_urls[:max_images]):
        try:
            filename, content = download_xhs_image(image_url, index)
            file_token = upload_extract_attachment(record_id, field_id, filename, content)
            if not file_token:
                raise HTTPException(status_code=502, detail="Feishu extract attachment media upload returned empty file_token")
            merged_attachments.append({"deprecated_set_attachment": True, "file_token": file_token, "name": filename})
            cell_dirty = True
            uploaded.append({"index": index, "url": image_url, "name": filename, "file_token": file_token, "ok": True})
        except HTTPException as exc:
            uploaded.append({"index": index, "url": image_url, "ok": False, "error": exc.detail})
    if cell_dirty:
        patch_base_attachment_cell(app_token, table_id, record_id, field_name, merged_attachments, "patch Feishu extract attachment cell")
    return uploaded


def field_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link"):
            item = value.get(key)
            if isinstance(item, (str, int, float)):
                return str(item)
        return ""
    if isinstance(value, list):
        return "".join(field_to_text(item) for item in value)
    return str(value)


def build_note_copy_body(body: str, tags: str) -> str:
    body = body.rstrip()
    tags = tags.strip()
    if body and tags:
        return f"{body}\n\n{tags}"
    return body or tags


def normalize_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    images = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        file_token = item.get("file_token") or item.get("token") or item.get("fileToken")
        if not file_token:
            continue
        images.append(
            {
                "index": index,
                "name": field_to_text(item.get("name")) or f"image-{index + 1}.jpg",
                "file_token": file_token,
                "size": item.get("size"),
                "type": item.get("type") or item.get("mime_type") or item.get("mimeType"),
            }
        )
    return images


def normalize_note_record(record: dict[str, Any], include_detail: bool = False) -> dict[str, Any]:
    fields = record.get("fields", {})
    body = field_to_text(fields.get(notes_body_field()))
    tags = field_to_text(fields.get(notes_tags_field()))
    fetched = field_to_text(fields.get(notes_fetched_field())).strip() or "否"
    note = {
        "record_id": record.get("record_id"),
        "title": field_to_text(fields.get(notes_title_field())),
        "category": field_to_text(fields.get(notes_category_field())),
        "fetched": fetched,
    }
    if include_detail:
        note.update(
            {
                "body": body,
                "tags": tags,
                "copy_body": build_note_copy_body(body, tags),
                "images": normalize_attachments(fields.get(notes_image_field())),
            }
        )
    return note


def filter_note_summaries(category: Optional[str], fetched: Optional[str]) -> list[dict[str, Any]]:
    notes = [normalize_note_record(record) for record in list_notes_records()]
    if category:
        notes = [note for note in notes if note["category"] == category]
    if fetched:
        notes = [note for note in notes if note["fetched"] == fetched]
    return notes


def build_bitable_attachment_extra(record_id: str, file_token: str) -> Optional[str]:
    field_id = get_notes_image_field_id()
    if not field_id:
        return None
    return json.dumps(
        {
            "bitablePerm": {
                "tableId": get_notes_table_id(),
                "attachments": {field_id: {record_id: [file_token]}},
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", binascii.crc32(chunk_type + data) & 0xFFFFFFFF)


def build_png_icon(size: int) -> bytes:
    raw = bytearray()
    red = bytes.fromhex("e00034")
    white = bytes.fromhex("fffaf8")
    radius = size * 0.23
    center = (size - 1) / 2
    for y in range(size):
        raw.append(0)
        for x in range(size):
            dx = abs(x - center)
            dy = abs(y - center)
            if dx + dy < radius or (size * 0.22 < x < size * 0.78 and size * 0.45 < y < size * 0.55):
                raw.extend(white)
            else:
                raw.extend(red)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + png_chunk(b"IEND", b"")


def design_system_css(extra: str = "") -> str:
    return """  <style>
    :root { color-scheme: light; --red:#e00034; --red-soft:#fff0f2; --ink:#151a24; --muted:#6f5f61; --line:#ead9d7; --bg:#fdf8f6; --surface:#fffaf8; --surface-soft:#fff4f1; --success:#0f7a4f; --success-soft:#e8f7ef; --error:#b4233c; --error-soft:#fff0f2; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif; background:var(--bg); color:var(--ink); }
    main { max-width:920px; margin:34px auto; padding:0 16px; }
    .wrap { width:min(980px,100%); margin:0 auto; padding:16px; }
    .wrap.narrow { max-width:860px; margin:42px auto; padding:0 18px; }
    .hero { padding:8px 2px 16px; margin-bottom:16px; }
    .card { background:var(--surface); border:1px solid rgba(224,0,52,.08); border-radius:18px; padding:24px; box-shadow:0 10px 30px rgba(93,39,48,.08); }
    h1 { margin:0 0 8px; font-size:26px; line-height:1.25; }
    h2 { margin:0 0 14px; font-size:20px; }
    h3 { margin:0; font-size:18px; }
    .desc,.tip { color:var(--muted); line-height:1.65; }
    .desc { margin:0 0 22px; }
    .tip { font-size:13px; margin:8px 0 0; }
    label,.field-label { display:block; margin:16px 0 8px; font-weight:700; }
    textarea,input,select { width:100%; border:1px solid var(--line); border-radius:13px; padding:13px 14px; font:inherit; font-size:16px; outline:none; background:#fffdfc; color:var(--ink); }
    textarea:focus,input:focus,select:focus { border-color:var(--red); box-shadow:0 0 0 3px rgba(224,0,52,.14); }
    textarea { min-height:150px; resize:vertical; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:16px; }
    button,a.button,button.ghost,a.ghost { border:0; border-radius:13px; padding:12px 16px; min-height:44px; font-size:15px; font-weight:800; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; transition:background-color .16s ease, border-color .16s ease, transform .16s ease, box-shadow .16s ease; }
    button,a.button { background:var(--red); color:#fffaf8; }
    button.secondary,a.secondary,.secondary { background:var(--surface-soft); color:var(--ink); border:1px solid var(--line); }
    button.ghost,a.ghost { background:var(--surface); color:var(--ink); border:1px solid var(--line); }
    button:hover,a.button:hover { box-shadow:0 8px 18px rgba(93,39,48,.12); transform:translateY(-1px); }
    button:active,a.button:active,button.ghost:active,a.ghost:active { transform:translateY(0); box-shadow:none; }
    button:disabled { opacity:.55; cursor:not-allowed; transform:none; box-shadow:none; }
    button:focus-visible,a:focus-visible,textarea:focus-visible,input:focus-visible,select:focus-visible { outline:2px solid var(--red); outline-offset:3px; }
    .status { margin-top:14px; background:#151a24; color:#e8f7ef; padding:14px; border-radius:14px; overflow:auto; white-space:pre-wrap; word-break:break-word; min-height:80px; }
    pre { margin-top:14px; background:#151a24; color:#e8f7ef; padding:14px; border-radius:14px; overflow:auto; white-space:pre-wrap; word-break:break-word; min-height:80px; }
    .grid,.split,.result { display:grid; gap:16px; }
    .hidden { display:none; }
    .box,.copy-box { border:1px solid var(--line); border-radius:14px; padding:13px; background:#fffdfc; white-space:pre-wrap; word-break:break-word; line-height:1.65; }
    .copy-box { margin-top:10px; }
    .navbar { position:sticky; top:0; z-index:30; background:rgba(253,248,246,.92); backdrop-filter:saturate(180%) blur(10px); border-bottom:1px solid var(--line); }
    .navbar .nav-inner { width:min(980px,100%); margin:0 auto; padding:10px 16px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .navbar .brand { font-weight:900; font-size:17px; color:var(--red); text-decoration:none; margin-right:6px; letter-spacing:.5px; }
    .navbar .nav-tabs { display:flex; gap:4px; flex:1; flex-wrap:wrap; }
    .navbar .nav-tab { padding:8px 14px; border-radius:11px; font-size:14px; font-weight:800; color:var(--muted); text-decoration:none; background:transparent; border:1px solid transparent; transition:color .14s ease, background .14s ease, border-color .14s ease; white-space:nowrap; }
    .navbar .nav-tab:hover { color:var(--ink); background:var(--surface-soft); }
    .navbar .nav-tab.active { color:var(--red); background:var(--red-soft); border-color:rgba(224,0,52,.18); }
    .navbar .nav-spacer { flex:1; }
    .navbar .nav-logout { padding:8px 14px; border-radius:11px; font-size:13px; font-weight:800; color:var(--muted); background:transparent; border:1px solid var(--line); cursor:pointer; white-space:nowrap; }
    .navbar .nav-logout:hover { color:var(--ink); background:var(--surface-soft); }
    main { margin-top:0; padding-top:24px; }
    @media (max-width:560px) { .navbar .nav-inner { flex-wrap:nowrap; gap:6px; padding:8px 12px; } .navbar .brand { display:none; } .navbar .nav-tabs { flex:1; gap:4px; } .navbar .nav-tab { padding:7px 10px; font-size:13px; } .navbar .nav-logout { padding:7px 10px; font-size:12px; } }
    @media (min-width:760px) { .wrap { padding:28px; } .wrap.narrow { padding:0 18px; } }
    @media (prefers-reduced-motion: reduce) { *,*::before,*::after { transition:none !important; animation:none !important; scroll-behavior:auto !important; } }
""" + extra + """
  </style>"""


def nav_bar(active: str = "") -> str:
    tabs = [
        ("clean", "/", "清洗链接"),
        ("notes", "/notes-page", "笔记素材库"),
        ("extract", "/extract-note-page", "抓取笔记"),
    ]
    tabs_html = "".join(
        f'<a class="nav-tab{" active" if active == key else ""}" href="{href}">{label}</a>'
        for key, href, label in tabs
    )
    return f"""  <nav class="navbar">
    <div class="nav-inner">
      <a class="brand" href="/">小红书飞书助手</a>
      <div class="nav-tabs">{tabs_html}</div>
      <span class="nav-spacer"></span>
      <button class="nav-logout" onclick="logoutFrontend()">退出</button>
    </div>
  </nav>"""


def pwa_head(title: str) -> str:
    return f"""<meta name=\"theme-color\" content=\"{PWA_THEME_COLOR}\" />
  <meta name=\"description\" content=\"小红书链接清洗、笔记内容抓取并写入飞书多维表格。\" />
  <meta name=\"mobile-web-app-capable\" content=\"yes\" />
  <meta name=\"apple-mobile-web-app-capable\" content=\"yes\" />
  <meta name=\"apple-mobile-web-app-status-bar-style\" content=\"default\" />
  <meta name=\"apple-mobile-web-app-title\" content=\"{PWA_APP_NAME}\" />
  <link rel=\"manifest\" href=\"/manifest.webmanifest\" />
  <link rel=\"icon\" href=\"/icons/icon-192.png\" />
  <link rel=\"apple-touch-icon\" href=\"/icons/icon-192.png\" />
  <title>{title}</title>"""


def pwa_register_script() -> str:
    return """
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}
</script>"""


def download_feishu_media(file_token: str, record_id: Optional[str] = None) -> requests.Response:
    params = None
    if record_id:
        extra = build_bitable_attachment_extra(record_id, file_token)
        if extra:
            params = {"extra": extra}
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download",
        headers=feishu_headers(),
        params=params,
        timeout=30,
    )
    if resp.status_code != 200:
        try:
            detail: Any = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=502, detail={"message": "Failed to download Feishu image", "response": detail})
    return resp


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": PWA_APP_NAME,
            "short_name": "小红书助手",
            "description": "小红书链接清洗、笔记内容抓取并写入飞书多维表格。",
            "start_url": "/notes-page",
            "scope": "/",
            "display": "standalone",
            "background_color": "#fdf8f6",
            "theme_color": PWA_THEME_COLOR,
            "icons": [
                {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker() -> Response:
    body = f"""
const CACHE_NAME = '{PWA_CACHE_NAME}';
const APP_SHELL = ['/', '/notes-page', '/extract-note-page', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png'];
self.addEventListener('install', event => {{
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
}});
self.addEventListener('activate', event => {{
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))));
  self.clients.claim();
}});
self.addEventListener('fetch', event => {{
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;
  if (!APP_SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(request).then(response => {{
    const copy = response.clone();
    caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
    return response;
  }}).catch(() => caches.match(request)));
}});
""".strip()
    return Response(body, media_type="application/javascript; charset=utf-8")


@app.get("/icons/icon-{size}.png")
def pwa_icon(size: int) -> Response:
    if size not in {192, 512}:
        raise HTTPException(status_code=404, detail="Icon not found")
    return Response(build_png_icon(size), media_type="image/png")


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    return {"ok": True, "password_required": bool(frontend_password())}


@app.post("/auth/login")
def login(body: LoginRequest) -> dict[str, Any]:
    expected = frontend_password()
    if expected and not hmac.compare_digest(body.password, expected):
        raise HTTPException(status_code=401, detail="Invalid password")
    if not expected:
        return {"ok": True, "token": "", "password_required": False}
    return {"ok": True, "token": create_frontend_token(), "password_required": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  """ + pwa_head("小红书链接写入飞书") + design_system_css("""
    .wrap.narrow .card { padding:28px; }
    .wrap.narrow .row { gap:12px; }
    .wrap.narrow pre { margin-top:18px; padding:16px; }
    .wrap.narrow button.secondary,.wrap.narrow a.secondary { background:var(--ink); color:#fffaf8; border:0; }
""") + r"""
</head>
<body>
  """ + nav_bar("clean") + r"""
  <main class="wrap narrow">
    <form id="loginCard" class="card hidden" onsubmit="loginFrontend(event)">
      <h1>进入小红书飞书助手</h1>
      <p class="desc">输入访问密码后，就能清洗链接、抓取笔记并写入飞书。</p>
      <label for="loginPassword">访问密码</label>
      <input id="loginPassword" type="password" autocomplete="current-password" placeholder="输入访问密码" />
      <div class="row">
        <button id="loginBtn" type="submit">进入工具</button>
      </div>
      <div id="loginStatus" class="status" role="status" aria-live="polite">请输入访问密码。</div>
    </form>

    <div id="appContent" class="card hidden">
      <h1>清洗链接并写入飞书</h1>
      <p class="desc">粘贴小红书分享链接，工具会去掉跟踪参数，只保留可打开的干净链接，再写入飞书多维表格。</p>

      <label for="url">小红书分享链接</label>
      <textarea id="url" placeholder="粘贴小红书分享链接或分享文本"></textarea>
      <div class="row">
        <button class="secondary" onclick="clearAndPasteUrl()">粘贴剪贴板</button>
      </div>

      <div class="row">
        <button id="submitBtn" onclick="submitUrl()">写入飞书</button>
        <button class="secondary" onclick="previewClean()">先看清洗结果</button>
      </div>

      <div id="result" class="status" role="status" aria-live="polite">粘贴链接后即可写入飞书。</div>
    </div>
  </main>
<script>
const TOKEN_KEY = 'xhs_feishu_frontend_token';

function showLogin() {
  document.getElementById('loginCard').classList.remove('hidden');
  document.getElementById('appContent').classList.add('hidden');
}

function showApp() {
  document.getElementById('loginCard').classList.add('hidden');
  document.getElementById('appContent').classList.remove('hidden');
}

function authHeaders(json=false) {
  const headers = {};
  if (json) headers['Content-Type'] = 'application/json';
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers['X-Frontend-Token'] = token;
  return headers;
}

async function initAuth() {
  try {
    const resp = await fetch('/auth/status');
    const data = await resp.json();
    if (!data.password_required || localStorage.getItem(TOKEN_KEY)) showApp();
    else showLogin();
  } catch(e) {
    showLogin();
  }
}

async function loginFrontend(event) {
  if (event) event.preventDefault();
  const password = document.getElementById('loginPassword').value;
  const status = document.getElementById('loginStatus');
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;
  status.textContent = '正在验证密码...';
  try {
    const resp = await fetch('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})});
    const data = await resp.json();
    if (!resp.ok || data.ok === false) throw new Error(data.detail || '密码不正确');
    if (data.token) localStorage.setItem(TOKEN_KEY, data.token);
    status.textContent = '已进入工具。';
    showApp();
  } catch(e) {
    status.textContent = '无法登录：' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function logoutFrontend() {
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
}

async function clearAndPasteUrl() {
  const input = document.getElementById('url');
  const result = document.getElementById('result');
  input.value = '';
  result.textContent = '正在读取剪贴板...';
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error('当前浏览器不支持读取剪贴板');
    const text = await navigator.clipboard.readText();
    input.value = text;
    result.textContent = text ? '已粘贴剪贴板内容，可以直接写入飞书。' : '剪贴板里没有可粘贴的内容。';
  } catch(e) {
    result.textContent = '没有读到剪贴板，请手动粘贴链接：' + e.message;
  }
  input.focus();
}

async function previewClean() {
  const result = document.getElementById('result');
  const input = document.getElementById('url').value.trim();
  if (!input) { result.textContent = '先粘贴一条小红书分享链接。'; return; }
  result.textContent = '正在整理链接，短链会自动展开...';
  try {
    const resp = await fetch('/clean', {method:'POST', headers:authHeaders(true), body:JSON.stringify({url: input})});
    const data = await resp.json();
    result.textContent = (resp.ok ? '清洗后的链接：\n' : '链接清洗失败：\n') + JSON.stringify(data, null, 2);
  } catch(e) {
    result.textContent = '清洗链接时出错，请稍后再试：' + e.message;
  }
}
async function submitUrl() {
  const url = document.getElementById('url').value.trim();
  const result = document.getElementById('result');
  const btn = document.getElementById('submitBtn');
  if (!url) { result.textContent = '先粘贴一条小红书分享链接。'; return; }
  btn.disabled = true; result.textContent = '正在写入飞书...';
  try {
    const resp = await fetch('/submit', {method:'POST', headers:authHeaders(true), body:JSON.stringify({url})});
    const data = await resp.json();
    result.textContent = (resp.ok ? '已写入飞书：\n' : '写入飞书失败：\n') + JSON.stringify(data, null, 2);
  } catch(e) { result.textContent = '写入飞书时出错，请稍后再试：' + e.message; }
  finally { btn.disabled = false; }
}

document.addEventListener('DOMContentLoaded', initAuth);
</script>
""" + pwa_register_script() + r"""
</body>
</html>
"""


@app.get("/notes-page", response_class=HTMLResponse)
def notes_page() -> str:
    return r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  """ + pwa_head("小红书笔记内容表") + design_system_css("""
    .card { padding:18px; box-shadow:0 10px 28px rgba(93,39,48,.07); }
    .hero .desc { margin:0; }
    .row { margin-top:14px; }
    label,.field-label { margin:12px 0 7px; }
    textarea { min-height:136px; }
    .filters { display:grid; grid-template-columns:1fr; gap:10px; }
    .note-list { display:grid; gap:10px; margin-top:14px; }
    .note-item { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:start; padding:13px; border:1px solid var(--line); border-radius:14px; background:#fffdfc; cursor:pointer; transition:border-color .14s ease, box-shadow .14s ease; }
    .note-item:hover { border-color:rgba(224,0,52,.3); box-shadow:0 6px 16px rgba(93,39,48,.08); }
    .note-item.active { border-color:var(--red); box-shadow:0 0 0 3px rgba(224,0,52,.14); }
    .note-title { font-weight:800; line-height:1.45; word-break:break-word; font-size:15px; }
    .note-preview { color:var(--muted); font-size:13px; line-height:1.5; margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .pill { display:inline-flex; align-items:center; width:max-content; margin-top:8px; padding:3px 9px; border-radius:999px; background:var(--red-soft); color:#9f1239; font-size:12px; font-weight:700; }
    .pill.fetched-yes { background:var(--success-soft); color:var(--success); }
    .note-item .view-btn { align-self:center; }
    .skeleton { background:linear-gradient(90deg,#f6ebe8 25%,#fff4f1 37%,#f6ebe8 63%); background-size:400% 100%; animation:sk 1.2s ease infinite; border-radius:12px; }
    @keyframes sk { 0%{background-position:100% 0;} 100%{background-position:-100% 0;} }
    .skeleton-item { height:62px; }
    .detail { display:none; }
    .detail.show { display:block; }
    .detail-header { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:6px; }
    .detail-header h2 { margin:0; font-size:18px; }
    .detail-back { display:none; }
    .section-title { font-size:13px; font-weight:800; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin:18px 0 8px; display:flex; align-items:center; gap:8px; }
    .section-title .count { background:var(--surface-soft); color:var(--ink); padding:2px 9px; border-radius:999px; font-size:12px; }
    .detail-title-box { font-size:20px; font-weight:900; line-height:1.35; word-break:break-word; padding:4px 0; }
    .detail-body-box { white-space:pre-wrap; word-break:break-word; line-height:1.7; font-size:14px; color:var(--ink); }
    .tag-row { display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }
    .tag-chip { padding:3px 10px; border-radius:999px; background:var(--red-soft); color:#9f1239; font-size:12px; font-weight:700; }
    .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    .toolbar button.copied { background:var(--success); color:#f7fff9; }
    .images { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:10px; }
    .image-card { position:relative; border:2px solid var(--line); border-radius:14px; overflow:hidden; background:var(--surface-soft); cursor:pointer; aspect-ratio:1/1; touch-action:manipulation; user-select:none; -webkit-user-select:none; -webkit-touch-callout:none; }
    .image-card:hover { border-color:rgba(224,0,52,.4); }
    .image-card.downloaded { border-color:var(--success); }
    .image-card.downloaded::after { content:'✓'; position:absolute; top:6px; right:6px; z-index:2; width:22px; height:22px; border-radius:50%; background:var(--success); color:#f7fff9; font-size:13px; font-weight:900; display:flex; align-items:center; justify-content:center; }
    .image-card.downloading::after { content:''; position:absolute; top:6px; right:6px; z-index:2; width:18px; height:18px; border:2px solid rgba(255,255,255,.4); border-top-color:#fff; border-radius:50%; animation:spin .7s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .image-card img { display:block; width:100%; height:100%; object-fit:cover; background:#f6ebe8; }
    .image-card .img-placeholder { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:12px; }
    .progress-wrap { margin-top:12px; display:none; }
    .progress-wrap.show { display:block; }
    .progress-bar { height:8px; border-radius:999px; background:var(--surface-soft); overflow:hidden; border:1px solid var(--line); }
    .progress-fill { height:100%; background:var(--red); width:0; transition:width .2s ease; border-radius:999px; }
    .progress-text { font-size:13px; color:var(--muted); margin-top:6px; font-weight:700; }
    .lightbox { position:fixed; inset:0; z-index:60; background:rgba(21,26,36,.92); display:none; align-items:center; justify-content:center; padding:20px; }
    .lightbox.show { display:flex; }
    .lightbox img { max-width:100%; max-height:80vh; border-radius:12px; box-shadow:0 20px 60px rgba(0,0,0,.5); }
    .lightbox-nav { position:absolute; top:50%; transform:translateY(-50%); width:44px; height:44px; border-radius:50%; background:rgba(255,255,255,.16); color:#fff; border:0; font-size:22px; cursor:pointer; display:flex; align-items:center; justify-content:center; }
    .lightbox-nav:hover { background:rgba(255,255,255,.28); }
    .lightbox-nav.prev { left:16px; }
    .lightbox-nav.next { right:16px; }
    .lightbox-toolbar { position:absolute; top:16px; right:16px; display:flex; gap:8px; }
    .lightbox-toolbar button { padding:8px 14px; border-radius:10px; border:0; font-weight:800; cursor:pointer; font-size:13px; }
    .lightbox-toolbar .lb-download { background:var(--red); color:#fff; }
    .lightbox-toolbar .lb-close { background:rgba(255,255,255,.16); color:#fff; }
    .lightbox-counter { position:absolute; bottom:18px; left:50%; transform:translateX(-50%); color:#fff; font-size:13px; font-weight:700; background:rgba(0,0,0,.4); padding:4px 12px; border-radius:999px; }
    .toast { position:fixed; left:50%; bottom:24px; z-index:70; transform:translate(-50%,16px); padding:10px 14px; border-radius:999px; background:rgba(21,26,36,.94); color:#fffaf8; font-size:14px; font-weight:800; opacity:0; pointer-events:none; transition:opacity .18s ease, transform .18s ease; box-shadow:0 10px 28px rgba(93,39,48,.18); }
    .toast.show { opacity:1; transform:translate(-50%,0); }
    @media (min-width:760px) { .grid,.split { grid-template-columns:1fr 1.2fr; } .filters { grid-template-columns:1fr 1fr; align-items:end; } .images { grid-template-columns:repeat(3,minmax(0,1fr)); } }
    @media (max-width:759px) { .detail { position:fixed; inset:0; z-index:40; background:var(--bg); overflow:auto; padding:16px; } .detail.show { display:block; } .detail-back { display:inline-flex; } .note-detail-card { max-width:680px; margin:0 auto; } }
""") + r"""
</head>
<body>
  """ + nav_bar("notes") + r"""
  <main class="wrap">
    <section class="hero">
      <h1>笔记素材库</h1>
      <div class="desc">查看已经写入飞书的笔记素材，快速复制标题正文，逐张下载图片。</div>
    </section>

    <form id="loginCard" class="card hidden" onsubmit="loginFrontend(event)">
      <h2>进入素材库</h2>
      <p class="desc">输入访问密码后，可以查看和下载已拉取的笔记素材。</p>
      <label for="loginPassword">访问密码</label>
      <input id="loginPassword" type="password" autocomplete="current-password" placeholder="输入访问密码" />
      <div class="row">
        <button id="loginBtn" type="submit">进入素材库</button>
      </div>
      <div id="loginStatus" class="status" role="status" aria-live="polite">请输入访问密码。</div>
    </form>

    <div id="appContent" class="grid hidden" style="margin-top:16px;">
      <section class="card">
        <h2 style="margin:0 0 6px;">待整理笔记</h2>
        <div class="filters">
          <div>
            <label for="noteCategory">笔记类别</label>
            <select id="noteCategory"><option value="">全部类别</option></select>
          </div>
          <div>
            <label for="noteFetched">拉取状态</label>
            <select id="noteFetched">
              <option value="">全部</option>
              <option value="否" selected>未拉取</option>
              <option value="是">已拉取</option>
            </select>
          </div>
        </div>
        <div id="noteList" class="note-list"></div>
      </section>

      <section id="noteDetail" class="card detail">
        <div class="note-detail-card">
          <div class="detail-header">
            <h2>笔记详情</h2>
            <button class="ghost detail-back" onclick="clearDetail()">返回列表</button>
          </div>
          <div class="section-title">标题</div>
          <div id="detailTitle" class="detail-title-box"></div>
          <div class="toolbar">
            <button id="copyTitleBtn" onclick="copyCurrentTitle()">复制标题</button>
          </div>

          <div class="section-title">正文</div>
          <div id="detailBody" class="detail-body-box"></div>
          <div id="detailTags" class="tag-row"></div>
          <div class="toolbar">
            <button id="copyBodyBtn" onclick="copyCurrentBody()">复制正文+标签</button>
          </div>

          <div class="section-title">图片 <span id="imageCount" class="count">0</span></div>
          <div class="toolbar">
            <button id="downloadAllBtn" onclick="downloadAllImages()">下载所有图片</button>
            <button id="shareReadyBtn" class="ghost" style="display:none" onclick="confirmPendingShare()">打开系统分享</button>
          </div>
          <div class="progress-wrap" id="progressWrap">
            <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
            <div class="progress-text" id="progressText">准备下载...</div>
          </div>
          <div id="imageList" class="images"></div>
          <div id="noteStatus" class="status" role="status" aria-live="polite">选择一条笔记查看详情。</div>
        </div>
      </section>
    </div>
    <div id="lightbox" class="lightbox">
      <div class="lightbox-toolbar">
        <button class="lb-download" id="lbDownload">下载</button>
        <button class="lb-close" id="lbClose">关闭</button>
      </div>
      <button class="lightbox-nav prev" id="lbPrev">‹</button>
      <img id="lbImg" alt="预览" />
      <button class="lightbox-nav next" id="lbNext">›</button>
      <div class="lightbox-counter" id="lbCounter"></div>
    </div>
    <div id="toast" class="toast" role="status" aria-live="polite"></div>
  </main>
<script>
let currentNote = null;
let activeRecordId = null;
const downloadedImageIndexes = new Set();
const imageCache = new Map();
let pendingShareFiles = null;
const TOKEN_KEY = 'xhs_feishu_frontend_token';
let toastTimer = null;
let filterDebounce = null;
let lightboxIndex = -1;

function showLogin() {
  document.getElementById('loginCard').classList.remove('hidden');
  document.getElementById('appContent').classList.add('hidden');
}

function showApp() {
  document.getElementById('loginCard').classList.add('hidden');
  document.getElementById('appContent').classList.remove('hidden');
}

function apiHeaders(json=false) {
  const headers = {};
  if (json) headers['Content-Type'] = 'application/json';
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers['X-Frontend-Token'] = token;
  return headers;
}

function initHistoryState() {
  if (!history.state) history.replaceState({page:'notes'}, '', location.pathname);
}

function detailVisible() {
  return document.getElementById('noteDetail').classList.contains('show');
}

async function initAuth() {
  initHistoryState();
  try {
    const resp = await fetch('/auth/status');
    const data = await resp.json();
    if (!data.password_required || localStorage.getItem(TOKEN_KEY)) {
      showApp();
      await loadCategories();
      await loadNotes();
      bindFilters();
    } else {
      showLogin();
    }
  } catch(e) {
    showLogin();
  }
}

async function loginFrontend(event) {
  if (event) event.preventDefault();
  const password = document.getElementById('loginPassword').value;
  const status = document.getElementById('loginStatus');
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;
  status.textContent = '正在验证密码...';
  try {
    const resp = await fetch('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})});
    const data = await resp.json();
    if (!resp.ok || data.ok === false) throw new Error(data.detail || '密码不正确');
    if (data.token) localStorage.setItem(TOKEN_KEY, data.token);
    status.textContent = '已进入素材库。';
    showApp();
    await loadCategories();
    await loadNotes();
    bindFilters();
  } catch(e) {
    status.textContent = '无法登录：' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function logoutFrontend() {
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
}

async function fetchJson(url, options={}) {
  const resp = await fetch(url, options);
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch(e) {
    throw new Error(resp.ok ? '服务返回了无法读取的内容。' : '服务暂时不可用，请检查飞书配置或稍后再试。');
  }
  if (!resp.ok || (data && data.ok === false)) throw new Error(JSON.stringify((data && data.detail) || data, null, 2));
  return data;
}

function setNoteStatus(text) {
  document.getElementById('noteStatus').textContent = text;
}

function bindFilters() {
  const cat = document.getElementById('noteCategory');
  const fet = document.getElementById('noteFetched');
  const handler = () => {
    clearTimeout(filterDebounce);
    filterDebounce = setTimeout(loadNotes, 300);
  };
  cat.onchange = handler;
  fet.onchange = handler;
}

async function loadCategories() {
  try {
    const data = await fetchJson('/notes/categories', {headers:apiHeaders()});
    const select = document.getElementById('noteCategory');
    select.innerHTML = '<option value="">全部类别</option>';
    for (const category of data.categories) {
      const option = document.createElement('option');
      option.value = category;
      option.textContent = category;
      select.appendChild(option);
    }
  } catch(e) {}
}

function renderSkeleton(list, n=4) {
  list.innerHTML = '';
  for (let i=0;i<n;i++) {
    const s = document.createElement('div');
    s.className = 'skeleton skeleton-item';
    list.appendChild(s);
  }
}

async function loadNotes() {
  const list = document.getElementById('noteList');
  renderSkeleton(list);
  try {
    const params = new URLSearchParams();
    const category = document.getElementById('noteCategory').value;
    const fetched = document.getElementById('noteFetched').value;
    if (category) params.set('category', category);
    if (fetched) params.set('fetched', fetched);
    const data = await fetchJson('/notes' + (params.toString() ? '?' + params.toString() : ''), {headers:apiHeaders()});
    renderNoteList(data.notes);
  } catch(e) {
    list.innerHTML = '<div class="tip">没有读到笔记：' + escapeHtml(e.message) + '</div>';
  }
}

function renderNoteList(notes) {
  const list = document.getElementById('noteList');
  if (!notes.length) {
    list.innerHTML = '<div class="tip">没有符合条件的笔记。换个类别或状态再试。</div>';
    return;
  }
  list.innerHTML = '';
  for (const note of notes) {
    const item = document.createElement('div');
    item.className = 'note-item' + (note.record_id === activeRecordId ? ' active' : '');
    item.dataset.recordId = note.record_id;
    const fetchedYes = note.fetched === '是';
    item.innerHTML = '<div><div class="note-title"></div><div class="note-preview"></div><div class="pill' + (fetchedYes ? ' fetched-yes' : '') + '"></div></div><button class="secondary view-btn">查看</button>';
    item.querySelector('.note-title').textContent = note.title || '未命名笔记';
    item.querySelector('.note-preview').textContent = (note.category || '未分类') + (fetchedYes ? ' · 已拉取' : ' · 未拉取');
    item.querySelector('.pill').textContent = fetchedYes ? '✓ 已拉取' : '待拉取';
    const open = () => loadNoteDetail(note.record_id);
    item.querySelector('button').onclick = (e) => { e.stopPropagation(); open(); };
    item.onclick = open;
    list.appendChild(item);
  }
}

async function loadNoteDetail(recordId) {
  activeRecordId = recordId;
  document.querySelectorAll('.note-item').forEach(el => el.classList.toggle('active', el.dataset.recordId === recordId));
  const panel = document.getElementById('noteDetail');
  panel.classList.add('show');
  setNoteStatus('正在打开笔记详情...');
  resetDownloadState();
  imageCache.clear();
  hideProgress();
  try {
    const data = await fetchJson('/notes/' + encodeURIComponent(recordId), {headers:apiHeaders()});
    currentNote = data.note;
    document.getElementById('detailTitle').textContent = currentNote.title || '';
    document.getElementById('detailBody').textContent = (currentNote.body || '').trim();
    renderTags(currentNote.tags || '');
    const images = currentNote.images || [];
    document.getElementById('imageCount').textContent = images.length;
    renderImages(images);
    if (!history.state || !history.state.detail) history.pushState({page:'notes', detail:true}, '', '#detail');
    setNoteStatus(images.length ? '已打开笔记。点击图片预览，下载全部后会标记为已拉取。' : '已打开笔记。这条笔记没有图片。');
  } catch(e) {
    setNoteStatus('没有打开这条笔记，请稍后再试：' + e.message);
  }
}

function renderTags(tagsText) {
  const row = document.getElementById('detailTags');
  const tags = String(tagsText || '').split(/\s+/).map(s => s.replace(/^#/, '').trim()).filter(Boolean);
  if (!tags.length) { row.innerHTML = ''; return; }
  row.innerHTML = tags.map(t => '<span class="tag-chip">#' + escapeHtml(t) + '</span>').join('');
}

function renderImages(images) {
  const list = document.getElementById('imageList');
  list.innerHTML = '';
  if (!images.length) {
    list.innerHTML = '<div class="tip">这条笔记没有图片。</div>';
    return;
  }
  for (const image of images) {
    const card = document.createElement('div');
    card.className = 'image-card';
    card.dataset.index = image.index;
    card.innerHTML = '<div class="img-placeholder">加载中</div><img alt="图片" style="display:none">';
    bindImageCardClick(card, image);
    list.appendChild(card);
    loadImageBlob(image).then(item => {
      const img = card.querySelector('img');
      img.src = item.url;
      img.style.display = 'block';
      card.querySelector('.img-placeholder').remove();
    }).catch(() => {
      card.querySelector('.img-placeholder').textContent = '加载失败';
    });
  }
}

function bindImageCardClick(card, image) {
  let clickTimer = null;
  const CLICK_DELAY = 250;
  card.addEventListener('click', () => {
    if (clickTimer) {
      clearTimeout(clickTimer);
      clickTimer = null;
      return;
    }
    clickTimer = setTimeout(async () => {
      clickTimer = null;
      await downloadSingleImage(image, card);
    }, CLICK_DELAY);
  });
  card.addEventListener('dblclick', () => {
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    openLightbox(image.index);
  });
}

async function downloadSingleImage(image, card) {
  if (!currentNote || card.classList.contains('downloaded') || card.classList.contains('downloading')) return;
  card.classList.add('downloading');
  try {
    setNoteStatus('正在下载图片：' + (image.name || ('第 ' + (image.index + 1) + ' 张')));
    const item = await loadImageBlob(image);
    triggerImageDownload(item);
    downloadedImageIndexes.add(image.index);
    card.classList.remove('downloading');
    card.classList.add('downloaded');
    const total = (currentNote.images || []).length;
    if (downloadedImageIndexes.size >= total) {
      await markDownloaded();
      setNoteStatus('已下载全部图片，并标记为已拉取。');
      await loadNotes();
    } else {
      setNoteStatus('已下载 ' + downloadedImageIndexes.size + ' / ' + total + ' 张。');
    }
  } catch(e) {
    card.classList.remove('downloading');
    setNoteStatus('图片下载失败，请再试：' + e.message);
  }
}

async function loadImageBlob(image) {
  const key = image.index;
  if (imageCache.has(key)) return imageCache.get(key);
  const params = new URLSearchParams();
  if (image.file_token) params.set('file_token', image.file_token);
  if (image.name) params.set('name', image.name);
  const query = params.toString() ? '?' + params.toString() : '';
  const resp = await fetch('/notes/' + encodeURIComponent(currentNote.record_id) + '/images/' + image.index + '/download' + query, {headers:apiHeaders()});
  if (!resp.ok) {
    let message = '图片下载失败';
    try {
      const data = await resp.json();
      message = data.detail ? JSON.stringify(data.detail, null, 2) : JSON.stringify(data, null, 2);
    } catch(e) {
      const text = await resp.text();
      if (text) message = text;
    }
    throw new Error(message);
  }
  const blob = await resp.blob();
  const item = {blob, url:URL.createObjectURL(blob), name:image.name || ('image-' + (image.index + 1) + '.jpg'), image};
  imageCache.set(key, item);
  return item;
}

function showToast(message) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1600);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.left = '-9999px';
  document.body.appendChild(area);
  area.focus();
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  return ok;
}

function flashCopied(btnId, text) {
  const btn = document.getElementById(btnId);
  const original = btn.textContent;
  btn.classList.add('copied');
  btn.textContent = '已复制 ✓';
  setTimeout(() => { btn.classList.remove('copied'); btn.textContent = original; }, 1600);
  showToast(text);
}

async function copyCurrentTitle() {
  if (!currentNote) return;
  if (await copyText(currentNote.title || '')) flashCopied('copyTitleBtn', '标题已复制');
}

async function copyCurrentBody() {
  if (!currentNote) return;
  if (await copyText(currentNote.copy_body || '')) flashCopied('copyBodyBtn', '正文已复制');
}

function resetDownloadState() {
  downloadedImageIndexes.clear();
  clearPendingShare();
}

function triggerImageDownload(item) {
  const link = document.createElement('a');
  link.href = item.url;
  link.download = item.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function showProgress(done, total, label) {
  const wrap = document.getElementById('progressWrap');
  const fill = document.getElementById('progressFill');
  const text = document.getElementById('progressText');
  wrap.classList.add('show');
  fill.style.width = (total ? Math.round(done/total*100) : 0) + '%';
  const prefix = label || '下载中';
  text.textContent = prefix + ' ' + done + ' / ' + total + (done >= total && total ? ' · 完成' : '');
}

function hideProgress() {
  document.getElementById('progressWrap').classList.remove('show');
}

function setShareReadyVisible(show) {
  const btn = document.getElementById('shareReadyBtn');
  if (!btn) return;
  btn.style.display = show ? '' : 'none';
  btn.disabled = !show;
}

function clearPendingShare() {
  pendingShareFiles = null;
  setShareReadyVisible(false);
}

function guessImageMime(name, blob) {
  if (blob && blob.type && blob.type !== 'application/octet-stream') return blob.type;
  const lower = String(name || '').toLowerCase();
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.webp')) return 'image/webp';
  if (lower.endsWith('.gif')) return 'image/gif';
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  return 'image/jpeg';
}

function itemToFile(item) {
  const type = guessImageMime(item.name, item.blob);
  const blob = (item.blob && item.blob.type) ? item.blob : new Blob([item.blob], {type: type});
  return new File([blob], item.name || 'image.jpg', {type: type});
}

function canShareFiles(files) {
  if (!navigator.share || !files || !files.length) return false;
  if (typeof navigator.canShare !== 'function') {
    // 部分内核有 share 但无 canShare：交给 share 时再兜底
    return true;
  }
  try {
    return navigator.canShare({files: files});
  } catch (e) {
    return false;
  }
}

async function sharePreparedFiles(files, title) {
  const payload = {files: files};
  if (title) {
    payload.title = title;
    payload.text = title;
  }
  await navigator.share(payload);
}

function markLoadedItemsDownloaded(loadedItems, cards) {
  for (const item of loadedItems) {
    const index = item.image ? item.image.index : null;
    if (index === null || index === undefined) continue;
    downloadedImageIndexes.add(index);
    const card = cards && cards.get(index);
    if (card) {
      card.classList.remove('downloading');
      card.classList.add('downloaded');
    }
  }
}

async function finalizeBatchDownload(loadedItems, images, cards, modeLabel) {
  markLoadedItemsDownloaded(loadedItems, cards);
  if (downloadedImageIndexes.size >= images.length) {
    await markDownloaded();
    setNoteStatus((modeLabel || '已处理全部图片') + '，并标记为已拉取。');
    await loadNotes();
  } else {
    setNoteStatus('已处理 ' + downloadedImageIndexes.size + ' / ' + images.length + ' 张。失败的可点单张重试。');
  }
}

async function fallbackDownloadLoadedItems(loadedItems, images, cards) {
  let done = 0;
  showProgress(0, loadedItems.length, '逐张保存');
  for (const item of loadedItems) {
    const index = item.image ? item.image.index : null;
    const card = cards && index !== null ? cards.get(index) : null;
    try {
      triggerImageDownload(item);
      if (index !== null && index !== undefined) downloadedImageIndexes.add(index);
      if (card) {
        card.classList.remove('downloading');
        card.classList.add('downloaded');
      }
    } catch (e) {
      if (card) card.classList.remove('downloading');
    }
    done++;
    showProgress(done, loadedItems.length, '逐张保存');
    await new Promise(r => setTimeout(r, 280));
  }
  if (downloadedImageIndexes.size >= images.length) {
    await markDownloaded();
    setNoteStatus('已触发逐张下载全部图片，并标记为已拉取。若浏览器拦截，请允许下载后重试。');
    await loadNotes();
  } else {
    setNoteStatus('已触发下载 ' + downloadedImageIndexes.size + ' / ' + images.length + ' 张。若未出现保存，请允许浏览器下载或改用系统分享。');
  }
}

async function confirmPendingShare() {
  if (!pendingShareFiles || !pendingShareFiles.length || !currentNote) {
    showToast('没有待分享的图片');
    return;
  }
  const btn = document.getElementById('shareReadyBtn');
  const downloadBtn = document.getElementById('downloadAllBtn');
  if (btn) btn.disabled = true;
  if (downloadBtn) downloadBtn.disabled = true;
  const images = currentNote.images || [];
  const cards = new Map([...document.querySelectorAll('.image-card')].map(card => [Number(card.dataset.index), card]));
  try {
    setNoteStatus('正在打开系统分享…');
    await sharePreparedFiles(pendingShareFiles, currentNote.title || '笔记图片');
    const loadedItems = (images || []).map(image => imageCache.get(image.index)).filter(Boolean);
    await finalizeBatchDownload(loadedItems, images, cards, '已通过系统分享全部图片');
    clearPendingShare();
    showToast('分享完成');
  } catch (e) {
    if (e && e.name === 'AbortError') {
      setNoteStatus('已取消系统分享。可再次点击「打开系统分享」，或点「下载所有图片」改用逐张保存。');
      setShareReadyVisible(true);
    } else {
      setNoteStatus('系统分享失败：' + (e && e.message ? e.message : e) + '。改为逐张下载…');
      const loadedItems = (images || []).map(image => imageCache.get(image.index)).filter(Boolean);
      clearPendingShare();
      await fallbackDownloadLoadedItems(loadedItems, images, cards);
    }
  } finally {
    if (btn && pendingShareFiles) btn.disabled = false;
    if (downloadBtn) downloadBtn.disabled = false;
    setTimeout(hideProgress, 1200);
  }
}

async function downloadAllImages() {
  if (!currentNote) return;
  const images = currentNote.images || [];
  if (!images.length) {
    setNoteStatus('这条笔记没有图片可下载。');
    return;
  }
  clearPendingShare();
  const btn = document.getElementById('downloadAllBtn');
  btn.disabled = true;
  const cards = new Map([...document.querySelectorAll('.image-card')].map(card => [Number(card.dataset.index), card]));
  showProgress(0, images.length, '拉取图片');
  setNoteStatus('正在拉取图片 ' + images.length + ' 张…');
  const concurrency = 4;
  let cursor = 0;
  let done = 0;
  const loadedItems = [];
  async function worker() {
    while (cursor < images.length) {
      const idx = cursor++;
      const image = images[idx];
      const card = cards.get(image.index);
      if (card && !card.classList.contains('downloaded')) card.classList.add('downloading');
      try {
        const item = await loadImageBlob(image);
        loadedItems.push(item);
        if (card) card.classList.remove('downloading');
      } catch (e) {
        if (card) card.classList.remove('downloading');
      }
      done++;
      showProgress(done, images.length, '拉取图片');
    }
  }
  try {
    await Promise.all(Array.from({length: concurrency}, worker));
    loadedItems.sort((a, b) => (a.image.index || 0) - (b.image.index || 0));
    if (!loadedItems.length) {
      setNoteStatus('图片拉取失败，请检查网络或登录后重试。');
      return;
    }
    if (loadedItems.length < images.length) {
      setNoteStatus('已拉取 ' + loadedItems.length + ' / ' + images.length + ' 张，将处理已成功的图片。');
    }
    const files = loadedItems.map(itemToFile);
    if (canShareFiles(files)) {
      showProgress(images.length, images.length, '打开分享');
      setNoteStatus('图片已就绪，正在打开系统分享（可选相册/文件）…');
      try {
        await sharePreparedFiles(files, currentNote.title || '笔记图片');
        await finalizeBatchDownload(loadedItems, images, cards, '已通过系统分享全部图片');
        showToast('分享完成');
      } catch (e) {
        if (e && e.name === 'AbortError') {
          pendingShareFiles = files;
          setShareReadyVisible(true);
          setNoteStatus('已取消系统分享。可点「打开系统分享」重试，或再次点「下载所有图片」。');
          showToast('已取消分享');
        } else if (e && (e.name === 'NotAllowedError' || e.name === 'InvalidStateError')) {
          pendingShareFiles = files;
          setShareReadyVisible(true);
          setNoteStatus('图片已就绪。请再点一次「打开系统分享」以保存到相册/文件。');
          showToast('请再点一次分享');
        } else {
          setNoteStatus('系统分享不可用（' + (e && e.message ? e.message : e) + '），改为逐张下载…');
          await fallbackDownloadLoadedItems(loadedItems, images, cards);
        }
      }
    } else {
      setNoteStatus('当前浏览器不支持多图分享，改为逐张下载…');
      await fallbackDownloadLoadedItems(loadedItems, images, cards);
    }
  } finally {
    btn.disabled = false;
    setTimeout(hideProgress, 1500);
  }
}

function openLightbox(index) {
  const images = (currentNote && currentNote.images) || [];
  if (!images.length) return;
  lightboxIndex = images.findIndex(i => i.index === index);
  if (lightboxIndex < 0) lightboxIndex = 0;
  renderLightbox();
  document.getElementById('lightbox').classList.add('show');
}

function closeLightbox() {
  document.getElementById('lightbox').classList.remove('show');
  lightboxIndex = -1;
}

function renderLightbox() {
  const images = (currentNote && currentNote.images) || [];
  if (!images.length || lightboxIndex < 0) return;
  const image = images[lightboxIndex];
  const img = document.getElementById('lbImg');
  loadImageBlob(image).then(item => { img.src = item.url; }).catch(() => { img.alt = '加载失败'; });
  document.getElementById('lbCounter').textContent = (lightboxIndex + 1) + ' / ' + images.length;
}

document.getElementById('lbPrev').onclick = () => {
  const images = (currentNote && currentNote.images) || [];
  if (!images.length) return;
  lightboxIndex = (lightboxIndex - 1 + images.length) % images.length;
  renderLightbox();
};
document.getElementById('lbNext').onclick = () => {
  const images = (currentNote && currentNote.images) || [];
  if (!images.length) return;
  lightboxIndex = (lightboxIndex + 1) % images.length;
  renderLightbox();
};
document.getElementById('lbClose').onclick = closeLightbox;
document.getElementById('lbDownload').onclick = async () => {
  const images = (currentNote && currentNote.images) || [];
  if (!images.length || lightboxIndex < 0) return;
  const image = images[lightboxIndex];
  const card = document.querySelector('.image-card[data-index="' + image.index + '"]');
  try {
    const item = await loadImageBlob(image);
    triggerImageDownload(item);
    downloadedImageIndexes.add(image.index);
    if (card) card.classList.add('downloaded');
    showToast('已下载：' + item.name);
  } catch(e) {
    showToast('下载失败');
  }
};
document.getElementById('lightbox').addEventListener('click', (e) => {
  if (e.target.id === 'lightbox') closeLightbox();
});
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox').classList.contains('show')) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft') document.getElementById('lbPrev').click();
  else if (e.key === 'ArrowRight') document.getElementById('lbNext').click();
});

async function markDownloaded() {
  await fetchJson('/notes/' + encodeURIComponent(currentNote.record_id) + '/mark-downloaded', {method:'POST', headers:apiHeaders()});
  currentNote.fetched = '是';
}

function clearDetail(fromHistory=false) {
  if (!fromHistory && history.state && history.state.detail) {
    history.back();
    return;
  }
  currentNote = null;
  activeRecordId = null;
  resetDownloadState();
  imageCache.clear();
  hideProgress();
  document.getElementById('noteDetail').classList.remove('show');
  document.querySelectorAll('.note-item').forEach(el => el.classList.remove('active'));
  if (!fromHistory && location.hash) history.replaceState({page:'notes'}, '', location.pathname);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

window.addEventListener('popstate', event => {
  if (!event.state || !event.state.detail) clearDetail(true);
});
document.addEventListener('DOMContentLoaded', initAuth);
</script>
""" + pwa_register_script() + r"""
</body>
</html>
"""


@app.get("/extract-note-page", response_class=HTMLResponse)
def extract_note_page() -> str:
    return r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  """ + pwa_head("笔记内容抓取") + design_system_css("""
    .hero .desc { margin:0; }
    .result { margin-top:16px; }
""") + r"""
</head>
<body>
  """ + nav_bar("extract") + r"""
  <main>
    <section class="hero card">
      <h1>抓取笔记内容</h1>
      <p class="desc">粘贴小红书分享链接或分享文本，提交后会排队抓取标题、正文、标签和图片链接，并写入飞书。</p>
    </section>

    <form id="loginCard" class="card hidden" onsubmit="loginFrontend(event)">
      <h2>进入抓取工具</h2>
      <p class="desc">输入访问密码后，就能把新的小红书笔记加入抓取队列。</p>
      <label for="loginPassword">访问密码</label>
      <input id="loginPassword" type="password" autocomplete="current-password" placeholder="输入访问密码" />
      <div class="row">
        <button id="loginBtn" type="submit">进入抓取工具</button>
      </div>
      <div id="loginStatus" class="status" role="status" aria-live="polite">请输入访问密码。</div>
    </form>

    <section id="appContent" class="card hidden">
      <h2>加入抓取队列</h2>
      <p class="desc">提交后可以继续粘贴下一条，后台会按顺序处理。</p>
      <label for="noteUrl">小红书分享链接或分享文本</label>
      <textarea id="noteUrl" placeholder="粘贴小红书分享文本，例如：标题 http://xhslink.com/..."></textarea>
      <div class="row">
        <button class="secondary" onclick="clearAndPasteNoteUrl()">粘贴剪贴板</button>
      </div>
      <div class="row">
        <button id="extractBtn" onclick="submitExtract()">开始抓取</button>
      </div>
      <div id="status" class="status" role="status" aria-live="polite">粘贴链接后即可加入队列。</div>
      <div id="result" class="result hidden">
        <div><strong>飞书记录 ID</strong><div id="recordId" class="box"></div></div>
        <div><strong>标题</strong><div id="noteTitle" class="box"></div></div>
        <div><strong>正文</strong><div id="noteBody" class="box"></div></div>
        <div><strong>标签</strong><div id="noteTags" class="box"></div></div>
        <div><strong>图片结果</strong><div id="imageSummary" class="box"></div></div>
      </div>
    </section>
  </main>
<script>
const TOKEN_KEY = 'xhs_feishu_frontend_token';

function showLogin() {
  document.getElementById('loginCard').classList.remove('hidden');
  document.getElementById('appContent').classList.add('hidden');
}

function showApp() {
  document.getElementById('loginCard').classList.add('hidden');
  document.getElementById('appContent').classList.remove('hidden');
}

function apiHeaders(json=false) {
  const headers = {};
  if (json) headers['Content-Type'] = 'application/json';
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers['X-Frontend-Token'] = token;
  return headers;
}

async function initAuth() {
  try {
    const resp = await fetch('/auth/status');
    const data = await resp.json();
    if (!data.password_required || localStorage.getItem(TOKEN_KEY)) showApp();
    else showLogin();
  } catch(e) {
    showLogin();
  }
}

async function loginFrontend(event) {
  if (event) event.preventDefault();
  const password = document.getElementById('loginPassword').value;
  const status = document.getElementById('loginStatus');
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;
  status.textContent = '正在验证密码...';
  try {
    const resp = await fetch('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})});
    const data = await resp.json();
    if (!resp.ok || data.ok === false) throw new Error(data.detail || '密码不正确');
    if (data.token) localStorage.setItem(TOKEN_KEY, data.token);
    status.textContent = '已进入工具。';
    showApp();
  } catch(e) {
    status.textContent = '无法登录：' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function logoutFrontend() {
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
}

async function clearAndPasteNoteUrl() {
  const input = document.getElementById('noteUrl');
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  input.value = '';
  result.classList.add('hidden');
  status.textContent = '正在读取剪贴板...';
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error('当前浏览器不支持读取剪贴板');
    const text = await navigator.clipboard.readText();
    input.value = text;
    status.textContent = text ? '已粘贴剪贴板内容，可以开始抓取。' : '剪贴板里没有可粘贴的内容。';
  } catch(e) {
    status.textContent = '没有读到剪贴板，请手动粘贴链接：' + e.message;
  }
  input.focus();
}

async function submitExtract() {
  const url = document.getElementById('noteUrl').value.trim();
  const status = document.getElementById('status');
  const btn = document.getElementById('extractBtn');
  const result = document.getElementById('result');
  if (!url) {
    status.textContent = '先粘贴一条小红书分享链接或分享文本。';
    return;
  }
  btn.disabled = true;
  result.classList.add('hidden');
  status.textContent = '正在加入抓取队列...';
  try {
    const resp = await fetch('/extract-note-submit', {method:'POST', headers:apiHeaders(true), body:JSON.stringify({url})});
    const data = await resp.json();
    if (!resp.ok || data.ok === false) throw new Error(JSON.stringify(data.detail || data, null, 2));
    document.getElementById('noteUrl').value = '';
    status.textContent = data.message || '已加入抓取队列，可以继续粘贴下一条。';
  } catch(e) {
    status.textContent = '加入队列失败，请稍后再试：' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderResult(data) {
  const note = data.note || {};
  const images = data.images || {};
  const urls = images.urls || note.image_urls || [];
  document.getElementById('recordId').textContent = (data.feishu && data.feishu.record_id) || '';
  document.getElementById('noteTitle').textContent = note.title || '';
  document.getElementById('noteBody').textContent = note.description || '';
  document.getElementById('noteTags').textContent = note.tags_text || (note.tags || []).join(' ');
  document.getElementById('imageSummary').textContent = '网页图片：' + ((note.image_urls || []).length) + ' 张\n已写入图片链接：' + urls.length + ' 条';
  document.getElementById('result').classList.remove('hidden');
}

document.addEventListener('DOMContentLoaded', initAuth);
</script>
""" + pwa_register_script() + r"""
</body>
</html>
"""


@app.post("/extract-note")
async def extract_note(
    body: XhsExtractRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    try:
        result = await extract_xhs_note(
            body.url,
            ExtractOptions(timeout_ms=body.timeout_ms, settle_ms=body.settle_ms, scroll_times=body.scroll_times),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to extract XHS note: {exc}") from exc
    return {"ok": True, "note": result}


@app.post("/extract-note-submit")
async def extract_note_submit(
    body: XhsExtractSubmitRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    try:
        return await handle_smart_extract_submit(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/submit")
def submit(
    body: SubmitRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    return handle_smart_submit_sync(body.url)


@app.post("/submit-batch")
def submit_batch(
    body: BatchSubmitRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    results = []
    for raw in body.urls:
        try:
            results.append(handle_smart_submit_sync(raw))
        except HTTPException as exc:
            results.append({"ok": False, "original_url": raw, "error": exc.detail})
    return {"ok": all(item.get("ok") for item in results), "results": results}


@app.post("/clean")
def clean_only(
    body: SubmitRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    try:
        cleaned = clean_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "original_url": body.url, "clean_url": cleaned, "xhs_id": extract_path_id(cleaned)}


@app.get("/notes/categories")
def note_categories(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    notes = [normalize_note_record(record) for record in list_notes_records()]
    categories = sorted({note["category"] for note in notes if note["category"]})
    return {"ok": True, "categories": categories}


@app.get("/notes")
def notes(
    category: Optional[str] = None,
    fetched: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    return {"ok": True, "notes": filter_note_summaries(category, fetched)}


@app.get("/notes/{record_id}")
def note_detail(
    record_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    return {"ok": True, "note": normalize_note_record(get_note_record(record_id), include_detail=True)}


@app.get("/notes/{record_id}/images/{image_index}/download")
def note_image_download(
    record_id: str,
    image_index: int,
    file_token: Optional[str] = None,
    name: Optional[str] = None,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> Response:
    verify_api_key(x_api_key, x_frontend_token)

    if not file_token:
        record = get_note_record(record_id)
        note = normalize_note_record(record, include_detail=True)
        images = note.get("images", [])
        image = next((item for item in images if item["index"] == image_index), None)
        if image is None:
            raise HTTPException(status_code=404, detail="Image not found")
        file_token = image.get("file_token")
        name = image.get("name")

    if not file_token:
        raise HTTPException(status_code=404, detail="Image file token not found")

    resp = download_feishu_media(file_token, record_id)
    filename = str(name or f"image-{image_index + 1}.jpg").replace("\r", "").replace("\n", "")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/notes/{record_id}/mark-downloaded")
def note_mark_downloaded(
    record_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_frontend_token: Optional[str] = Header(default=None, alias="X-Frontend-Token"),
) -> dict[str, Any]:
    verify_api_key(x_api_key, x_frontend_token)
    update_note_fetched(record_id)
    return {"ok": True, "record_id": record_id, "fetched": notes_fetched_yes_value()}
