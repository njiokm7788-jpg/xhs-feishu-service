"""验证飞书 batch_get_tmp_download_url 接口是否可用于让浏览器直连下载附件。

只读测试脚本，自包含飞书调用逻辑（不 import main，避免触发 FastAPI 实例化）。

跑通后会把临时下载 URL 打印出来，再用浏览器控制台实测 CORS：
  1) <img src=URL>               —— 显示图片（不受同源策略限制）
  2) fetch(URL).then(r=>r.blob()) —— 取 blob 触发下载（受 CORS 限制，决定能否绕开服务器）

用法:
    cd xhs_feishu_service
    python verify_tmp_download_url.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

FEISHU_BASE = "https://open.feishu.cn"
BATCH_TMP_URL = f"{FEISHU_BASE}/open-apis/drive/v1/medias/batch_get_tmp_download_url"


# ── 飞书配置 ──────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def get_feishu_tenant_access_token() -> str:
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise SystemExit("❌ 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请检查 .env")
    resp = requests.post(
        f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    data = resp.json()
    if "tenant_access_token" not in data:
        raise SystemExit(f"❌ 获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def feishu_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_feishu_tenant_access_token()}"}


# ── notes 表配置 ──────────────────────────────────────────────────────────
def notes_app_token() -> str:
    return _env("FEISHU_NOTES_BITABLE_APP_TOKEN")


def notes_table_id() -> str:
    return _env("FEISHU_NOTES_TABLE_ID")


def notes_image_field() -> str:
    return _env("FEISHU_NOTES_IMAGE_FIELD", "图片")


# ── 字段 ID（拿一次缓存）──────────────────────────────────────────────────
_image_field_id_cache: Optional[str] = None


def get_notes_image_field_id() -> Optional[str]:
    global _image_field_id_cache
    if _image_field_id_cache:
        return _image_field_id_cache
    app_token = notes_app_token()
    table_id = notes_table_id()
    resp = requests.get(
        f"{FEISHU_BASE}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        headers=feishu_headers(),
        timeout=20,
    )
    data = resp.json()
    target = notes_image_field()
    for field in (data.get("data", {}).get("items") or []):
        if (field.get("field_name") or "") == target:
            _image_field_id_cache = field.get("field_id")
            return _image_field_id_cache
    return None


# ── 记录读取 ──────────────────────────────────────────────────────────────
def list_notes_records() -> list[dict[str, Any]]:
    app_token = notes_app_token()
    table_id = notes_table_id()
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(
            f"{FEISHU_BASE}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=feishu_headers(),
            params=params,
            timeout=20,
        )
        data = resp.json()
        payload = data.get("data", {})
        records.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return records
        page_token = payload.get("page_token", "")
        if not page_token:
            return records


def field_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(field_to_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value)


def normalize_attachments(value: Any) -> list[dict[str, Any]]:
    """从附件字段提取 [{index, name, file_token, size, type}]。"""
    if not isinstance(value, list):
        return []
    images: list[dict[str, Any]] = []
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


def find_first_note_with_images() -> dict[str, Any] | None:
    image_field = notes_image_field()
    title_field = _env("FEISHU_NOTES_TITLE_FIELD", "标题")
    for record in list_notes_records():
        fields = record.get("fields", {}) or {}
        images = normalize_attachments(fields.get(image_field))
        if not images:
            continue
        return {
            "record_id": record.get("record_id"),
            "title": field_to_text(fields.get(title_field)),
            "images": images,
        }
    return None


# ── extra（bitable 高级权限上下文）────────────────────────────────────────
def build_attachment_extra(record_id: str, file_tokens: list[str]) -> Optional[str]:
    field_id = get_notes_image_field_id()
    if not field_id:
        return None
    # extra 里 attachments 形如 {field_id: {record_id: [token, token, ...]}}
    return json.dumps(
        {
            "bitablePerm": {
                "tableId": notes_table_id(),
                "attachments": {field_id: {record_id: file_tokens}},
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ── 主流程 ────────────────────────────────────────────────────────────────
def fetch_tmp_download_urls(file_tokens: list[str], record_id: str) -> dict[str, Any]:
    """调 batch_get_tmp_download_url（单次最多 5 个 token）。返回原始 JSON + 状态码。"""
    file_tokens = file_tokens[:5]
    extra = build_attachment_extra(record_id, file_tokens)
    params: dict[str, Any] = {"file_tokens": file_tokens}
    if extra:
        params["extra"] = extra
    resp = requests.get(
        BATCH_TMP_URL,
        headers=feishu_headers(),
        params=params,
        timeout=20,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}
    return {"status_code": resp.status_code, "data": data}


def main() -> int:
    print("=" * 72)
    print("飞书 batch_get_tmp_download_url 可行性验证（方法 E：浏览器直连飞书 CDN）")
    print("=" * 72)

    app_token = notes_app_token()
    table_id = notes_table_id()
    image_field = notes_image_field()
    if not app_token or not table_id:
        print("❌ 缺少 FEISHU_NOTES_BITABLE_APP_TOKEN / FEISHU_NOTES_TABLE_ID，请检查 .env")
        return 1
    print(f"笔记表: app_token={app_token}  table_id={table_id}  图片字段={image_field}")
    print(f"图片字段 ID: {get_notes_image_field_id() or '(未找到)'}")

    print("\n[1/3] 查找第一条带图片附件的笔记...")
    note = find_first_note_with_images()
    if not note:
        print("❌ 没有找到带图片附件的笔记。请确认飞书表里有数据且图片字段配置正确。")
        return 1
    print(f"✅ 找到: record_id={note['record_id']}  title={note['title'][:40]}")
    print(f"    图片数量: {len(note['images'])}")

    file_tokens = [img["file_token"] for img in note["images"] if img.get("file_token")]
    sample_tokens = file_tokens[:5]
    print(f"\n[2/3] 调用 batch_get_tmp_download_url（取前 5 个 token）...")
    print(f"    file_tokens(前5): {sample_tokens}")
    result = fetch_tmp_download_urls(sample_tokens, note["record_id"])
    print(f"    HTTP 状态: {result['status_code']}")
    print(f"    响应: {json.dumps(result['data'], ensure_ascii=False, indent=2)[:1000]}")

    data = result["data"]
    tmp_urls: list[str] = []
    if isinstance(data, dict) and data.get("code") == 0:
        tmp_urls = [
            item.get("tmp_download_url", "")
            for item in (data.get("data", {}).get("tmp_download_urls") or [])
            if item.get("tmp_download_url")
        ]

    if not tmp_urls:
        print("\n❌ 未拿到 tmp_download_url。请检查应用对该附件的下载权限、extra 参数、file_tokens。")
        return 2

    print(f"\n[3/3] 拿到 {len(tmp_urls)} 个临时下载 URL（官方说明 24h 有效）：")
    for i, url in enumerate(tmp_urls):
        print(f"\n  --- 图片 {i + 1} ---")
        print(f"  {url}")

    # 服务端侧 GET 第一个临时 URL：排除 URL 本身失效，并观察响应头里有无 CORS 标记
    print("\n[附] 服务端侧 GET 第一个临时 URL（验证字节可达 + 观察响应头）...")
    try:
        r = requests.get(tmp_urls[0], timeout=30)
        print(f"    HTTP 状态: {r.status_code}  字节数: {len(r.content)}")
        print(f"    Content-Type: {r.headers.get('content-type', '')}")
        acao = r.headers.get("access-control-allow-origin", "")
        acac = r.headers.get("access-control-allow-credentials", "")
        print(f"    Access-Control-Allow-Origin: {acao or '(无)'}")
        print(f"    Access-Control-Allow-Credentials: {acac or '(无)'}")
        print(f"    响应头全部 key: {list(r.headers.keys())}")
    except Exception as exc:
        print(f"    服务端 GET 失败: {exc}")

    print("\n" + "=" * 72)
    print("下一步：浏览器控制台实测 CORS（决定方法 E 能否落地）")
    print("=" * 72)
    first_url = tmp_urls[0]
    print("\n把下面【图片 1】的 URL 用于浏览器实测：")
    print(f"\n  URL = {first_url}")
    print("\nA. <img> 显示（不受同源策略限制，能显示说明 URL 有效可用）：")
    print("   const i = new Image(); i.src = URL;")
    print("   i.onload = () => console.log('显示 OK', i.naturalWidth, 'x', i.naturalHeight);")
    print("   i.onerror = () => console.log('显示失败');")
    print("\nB. fetch 取 blob（受 CORS 限制；这步成功才能真正绕开服务器下载存盘）：")
    print("   fetch(URL).then(r => r.blob())")
    print("     .then(b => console.log('blob OK', b.size, b.type))")
    print("     .catch(e => console.log('CORS 被拦 / 失败', e));")
    print("\n判读：")
    print("  • A 成功 + B 成功   → 方法 E 完全可行，浏览器直连飞书下载，服务器零流量")
    print("  • A 成功 + B 报 CORS → 方法 E 退化：仅 <img> 显示可直连，下载存盘仍需服务中转/zip")
    print("  • A 失败           → 临时 URL 可能需带特定头或受 referer 限制，方法 E 不可行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
