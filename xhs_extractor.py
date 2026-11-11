from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Browser
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


IPHONE_SAFARI_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Mobile/15E148 Safari/604.1"
)

DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
"""

EXTRACT_PAGE_SCRIPT = r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const meta = {};
  for (const m of document.querySelectorAll('meta')) {
    const key = m.getAttribute('name') || m.getAttribute('property') || m.getAttribute('itemprop');
    if (key) meta[key] = m.getAttribute('content') || '';
  }

  const bodyText = norm(document.body ? document.body.innerText : '');
  const noteImageSelector = [
    '#normal_note_first_img',
    '.onix-carousel-inner img',
    '.onix-carousel-item img',
    '.note-image-container img',
    '.note-image-box img',
    '.image-impression-tracker img',
    '.swiper-slide img',
  ].join(',');
  const recommendationSelector = [
    '.note-card',
    '.dual-col',
    '.dual-row',
    '[class*="recommend"]',
    '[class*="Recommend"]',
    '[class*="feeds"]',
    '[class*="Feeds"]',
  ].join(',');
  const uniqueImages = (nodes) => {
    const seen = new Set();
    const out = [];
    for (const img of nodes) {
      if (!img || seen.has(img)) continue;
      seen.add(img);
      out.push(img);
    }
    return out;
  };
  // Use all loaded images, then let Python-side filters remove recommendation/feed cards.
  // The carousel DOM may only expose currently materialized slides, while document.images
  // can include the full note image set after XHS hydration/lazy loading.
  const images = uniqueImages([...document.images]).map((img) => ({
    src: img.currentSrc || img.src || '',
    width: img.naturalWidth || img.width || 0,
    height: img.naturalHeight || img.height || 0,
    alt: img.alt || '',
    className: String(img.className || ''),
    inNoteCarousel: !!img.closest(noteImageSelector),
    inRecommendation: !!img.closest(recommendationSelector),
  }));

  const noteId = (location.href.match(/(?:discovery\/item|explore)\/([0-9a-fA-F]{12,})/) || [])[1] || '';
  const toStateImage = (item) => {
    if (!item || typeof item !== 'object') return null;
    let src = item.url || item.src || item.originUrl || item.originalUrl || '';
    if (Array.isArray(item.infoList) && item.infoList.length) {
      const preferred = item.infoList.find((x) => x && ['H5_DTL', 'WB_DTL', 'NOTE_DETAIL'].includes(x.imageScene));
      src = (preferred && preferred.url) || (item.infoList.find((x) => x && x.url) || {}).url || src;
    }
    if (!src) return null;
    return {
      src,
      width: Number(item.width || item.w || 0),
      height: Number(item.height || item.h || 0),
      alt: '',
      className: 'state-image',
      inNoteCarousel: true,
      inRecommendation: false,
      source: 'state',
    };
  };
  const extractStateImages = () => {
    const roots = [window.__INITIAL_STATE__, window.__SETUP_SERVER_STATE__].filter(Boolean);
    const seenObjects = new WeakSet();
    const found = [];
    const pushList = (list) => {
      if (!Array.isArray(list)) return;
      for (const item of list) {
        const img = toStateImage(item);
        if (img) found.push(img);
      }
    };
    const visit = (value, depth = 0) => {
      if (!value || depth > 12 || found.length >= 80) return;
      if (Array.isArray(value)) {
        for (const item of value) visit(item, depth + 1);
        return;
      }
      if (typeof value !== 'object') return;
      if (seenObjects.has(value)) return;
      seenObjects.add(value);
      const valueNoteId = String(value.noteId || value.note_id || value.id || '');
      if ((!noteId || valueNoteId === noteId) && Array.isArray(value.imageList)) {
        pushList(value.imageList);
      }
      for (const key of Object.keys(value)) visit(value[key], depth + 1);
    };
    for (const root of roots) visit(root, 0);
    return found;
  };
  const stateImages = extractStateImages();

  const normalizeTag = (tag) => {
    if (!tag) return '';
    if (typeof tag === 'string') return tag;
    if (typeof tag !== 'object') return '';
    return tag.name || tag.tagName || tag.title || tag.id || '';
  };
  const extractStateNote = () => {
    const roots = [window.__INITIAL_STATE__, window.__SETUP_SERVER_STATE__].filter(Boolean);
    const seenObjects = new WeakSet();
    let best = null;
    let bestScore = -1;
    const choose = (value) => {
      if (!value || typeof value !== 'object') return;
      const valueNoteId = String(value.noteId || value.note_id || value.id || '');
      if (noteId && valueNoteId && valueNoteId !== noteId) return;
      const title = value.title || value.displayTitle || value.shareTitle || '';
      const desc = value.desc || value.description || value.content || value.noteDesc || '';
      const imageList = Array.isArray(value.imageList) ? value.imageList : [];
      const tagList = Array.isArray(value.tagList) ? value.tagList : (Array.isArray(value.tags) ? value.tags : []);
      const score = (valueNoteId ? 20 : 0) + (title ? 10 : 0) + (desc ? Math.min(30, String(desc).length / 20) : 0) + Math.min(30, imageList.length * 3);
      if ((title || desc || imageList.length) && score > bestScore) {
        bestScore = score;
        best = {
          noteId: valueNoteId,
          title: String(title || ''),
          desc: String(desc || ''),
          tags: tagList.map(normalizeTag).filter(Boolean),
          imageCount: imageList.length,
        };
      }
    };
    const visit = (value, depth = 0) => {
      if (!value || depth > 12) return;
      if (Array.isArray(value)) {
        for (const item of value) visit(item, depth + 1);
        return;
      }
      if (typeof value !== 'object') return;
      if (seenObjects.has(value)) return;
      seenObjects.add(value);
      choose(value);
      for (const key of Object.keys(value)) visit(value[key], depth + 1);
    };
    for (const root of roots) visit(root, 0);
    return best || {};
  };
  const stateNote = extractStateNote();

  return {
    href: location.href,
    title: document.title || '',
    meta,
    bodyText,
    bodyPreview: bodyText.slice(0, 4000),
    stateNote,
    stateImages,
    images,
  };
}
"""


@dataclass(slots=True)
class ExtractOptions:
    timeout_ms: int = 60000
    settle_ms: int = 3000
    scroll_times: int = 4
    headless: bool = True
    debug: bool = False


def extract_first_url(text: str) -> str:
    """Accept a pure URL or a copied XHS share text containing a URL."""
    text = (text or "").strip()
    match = re.search(r"https?://[^\s\"'<>]+", text)
    if match:
        return match.group(0).rstrip("，。,.;；、)）]")
    return text


def extract_note_id(url: str) -> str:
    match = re.search(r"/(?:discovery/item|explore)/([0-9a-fA-F]{12,})", url or "")
    return match.group(1) if match else ""


def resolve_oia_target_url(url: str) -> str:
    """Recover the real web note URL from XHS OIA/app-open intermediate pages."""
    if not url or "oia.xiaohongshu.com/oia" not in url:
        return ""
    try:
        query = parse_qs(urlparse(url).query)
        deeplink = (query.get("deeplink") or [""])[0]
        parsed_deeplink = urlparse(deeplink)
        deeplink_query = parse_qs(parsed_deeplink.query)
        open_url = (deeplink_query.get("open_url") or [""])[0]
        if open_url.startswith("/"):
            return "https://www.xiaohongshu.com" + open_url
        if open_url.startswith(("http://", "https://")):
            return open_url
        note_id = ""
        if parsed_deeplink.scheme.startswith("xhs") and parsed_deeplink.netloc == "item":
            note_id = parsed_deeplink.path.strip("/")
        if note_id:
            return f"https://www.xiaohongshu.com/discovery/item/{note_id}"
    except Exception:
        return ""
    return ""


def page_data_score(page_data: dict[str, Any]) -> int:
    """Prefer data that already contains real note content over app-open/OIA pages."""
    if not page_data:
        return 0
    score = 0
    href = page_data.get("href", "") or ""
    if extract_note_id(href):
        score += 10
    title = normalize_title(page_data)
    description = normalize_description(page_data)
    if title and title not in {"打开小红书", "小红书"}:
        score += 10
    if description and description != "在小红书，和最会生活的人做朋友":
        score += min(20, len(description) // 20)
    score += min(30, len(normalize_images(page_data)) * 2)
    if "oia.xiaohongshu.com/oia" in href:
        score -= 20
    return score


def has_enough_note_content(page_data: dict[str, Any]) -> bool:
    return page_data_score(page_data) >= 25


def looks_like_login_or_blocked(page_data: dict[str, Any]) -> tuple[bool, str]:
    href = page_data.get("href", "")
    title = page_data.get("title", "")
    text = page_data.get("bodyText", "") or page_data.get("bodyPreview", "")
    if "/login" in href or "登录后推荐更懂你的笔记" in text or "手机号登录" in text:
        return True, "XHS_LOGIN_REQUIRED"
    if "/404/sec_" in href or "source=xhs_sec_server" in href:
        return True, "XHS_SECURITY_BLOCKED"
    if title == "小红书" and len(text) < 80 and not page_data.get("meta", {}).get("description"):
        return True, "XHS_EMPTY_OR_BLOCKED"
    return False, ""


def normalize_title(page_data: dict[str, Any]) -> str:
    meta = page_data.get("meta", {}) or {}
    state_note = page_data.get("stateNote", {}) or {}
    title = (
        state_note.get("title")
        or meta.get("og:title")
        or meta.get("title")
        or meta.get("twitter:title")
        or page_data.get("title")
        or ""
    )
    title = re.sub(r"\s*-\s*小红书\s*$", "", title).strip()
    return title


def normalize_description(page_data: dict[str, Any]) -> str:
    meta = page_data.get("meta", {}) or {}
    state_note = page_data.get("stateNote", {}) or {}
    desc = state_note.get("desc") or meta.get("description") or meta.get("og:description") or ""
    desc = desc.strip()
    if desc:
        return desc

    body = page_data.get("bodyText", "") or ""
    title = normalize_title(page_data)
    # Mobile share pages usually place title/body before recommendation area.
    stop_markers = ["打开小红书查看", "热门推荐", "查看更多", "说点什么", "App内打开"]
    end = len(body)
    for marker in stop_markers:
        idx = body.find(marker)
        if idx > 0:
            end = min(end, idx)
    text = body[:end].strip()
    if title and text.startswith(title):
        text = text[len(title) :].strip()
    return text


def normalize_tags(page_data: dict[str, Any]) -> list[str]:
    meta = page_data.get("meta", {}) or {}
    tags: list[str] = []
    state_tags = (page_data.get("stateNote", {}) or {}).get("tags") or []
    for tag in state_tags:
        tag = str(tag).strip().strip("#")
        if tag and tag not in tags:
            tags.append(tag)
    raw = meta.get("keywords") or ""
    for part in re.split(r"[,，#\n\t]+", raw):
        tag = part.strip().strip("#")
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def canonical_image_key(src: str) -> str:
    src = (src or "").strip()
    src = src.split("?")[0]
    # XHS CDN suffix after ! often only changes rendition. Deduplicate by original object id
    # when possible because signed desktop webpic URLs contain changing date/hash prefixes.
    src = src.split("!")[0]
    match = re.search(r"/(notes_pre_post/[^/?!]+|[0-9a-z]{20,})$", src)
    if match:
        return match.group(1)
    return src


def is_probable_note_image(img: dict[str, Any]) -> bool:
    if img.get("inRecommendation"):
        return False
    src = img.get("src", "") or ""
    width = int(img.get("width") or 0)
    height = int(img.get("height") or 0)
    if not src.startswith(("http://", "https://", "//")):
        return False
    if src.startswith("//"):
        src = "https:" + src
    parsed = urlparse(src)
    host = parsed.netloc.lower()
    if not any(k in host for k in ("xhscdn.com", "xhscdn.net", "xiaohongshu.com")):
        return False
    if any(k in src for k in ("avatar", "icon", "logo", "picasso-static", "fe-platform")):
        return False
    if width and height and (width < 450 or height < 450):
        return False
    # Note images commonly use these XHS CDN variants/paths. Network-captured desktop
    # image URLs may not have decoded dimensions yet, so allow known note variants first.
    if any(k in src for k in ("notes_pre_post", "spectrum", "h5_", "nd_dft", "sns-webpic")):
        return True
    if width < 450 or height < 450:
        return False
    # Fallback: large portrait/square images from XHS CDN.
    return width >= 700 and height >= 700


def normalize_images(page_data: dict[str, Any], max_images: int = 30) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Prefer SSR/hydration state imageList for the current note. DOM carousel may only
    # materialize visible slides; full-page DOM may include recommendation cards.
    source_images = (page_data.get("networkImages") or []) + (page_data.get("stateImages") or []) + (page_data.get("images", []) or [])
    for img in source_images:
        src = img.get("src", "") or ""
        if src.startswith("//"):
            src = "https:" + src
        candidate = {**img, "src": src}
        if not is_probable_note_image(candidate):
            continue
        key = canonical_image_key(src)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "src": src,
                "width": int(candidate.get("width") or 0),
                "height": int(candidate.get("height") or 0),
                "alt": candidate.get("alt", ""),
            }
        )
        if len(result) >= max_images:
            break
    return result


def build_result(input_text_or_url: str, input_url: str, page_data: dict[str, Any]) -> dict[str, Any]:
    blocked, reason = looks_like_login_or_blocked(page_data)
    title = normalize_title(page_data)
    description = normalize_description(page_data)
    tags = normalize_tags(page_data)
    images = normalize_images(page_data)
    final_url = page_data.get("href", "") or ""
    note_id = extract_note_id(final_url) or extract_note_id(input_url)

    has_real_content = bool(description or images or page_data.get("stateNote"))
    # Desktop XHS may briefly expose SSR state and then redirect to /login. If we captured
    # real note content before the redirect, treat it as successful instead of letting the
    # final login URL overwrite the useful snapshot.
    if blocked and reason == "XHS_LOGIN_REQUIRED" and has_real_content:
        blocked = False
        reason = ""

    success = not blocked and bool(title or description or images)
    if success and not images and not description:
        success = False
        reason = "XHS_NO_CONTENT_EXTRACTED"

    return {
        "success": success,
        "reason": "" if success else (reason or "XHS_NO_CONTENT_EXTRACTED"),
        "input": input_text_or_url,
        "input_url": input_url,
        "final_url": final_url,
        "note_id": note_id,
        "title": title,
        "description": description,
        "tags": tags,
        "tags_text": " ".join(f"#{tag}" for tag in tags),
        "images": images,
        "image_urls": [img["src"] for img in images],
        "meta": page_data.get("meta", {}) or {},
        "state_note": page_data.get("stateNote", {}) or {},
        "body_preview": page_data.get("bodyPreview", "") or "",
    }


async def launch_xhs_browser(options: ExtractOptions | None = None) -> tuple[Any, Browser]:
    options = options or ExtractOptions()
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=options.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
    except Exception:
        await playwright.stop()
        raise
    return playwright, browser


async def close_xhs_browser(playwright: Any, browser: Browser | None) -> None:
    try:
        if browser:
            await browser.close()
    finally:
        await playwright.stop()


async def extract_xhs_note_with_browser(input_text_or_url: str, browser: Browser, options: ExtractOptions | None = None) -> dict[str, Any]:
    """Extract public XHS note data using a caller-managed Playwright browser."""
    options = options or ExtractOptions()
    input_url = extract_first_url(input_text_or_url)
    if not input_url.startswith(("http://", "https://")):
        raise ValueError("No http(s) URL found in input")

    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        is_mobile=False,
        has_touch=False,
        device_scale_factor=1,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=DESKTOP_CHROME_UA,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    page = await context.new_page()
    network_images: list[dict[str, Any]] = []
    seen_network_images: set[str] = set()

    def remember_network_image(url: str) -> None:
        if not url or "xhscdn" not in url:
            return
        if not any(token in url for token in ("!nd_dft", "!nd_prv", "!nd_wm", "!h5_")):
            return
        key = canonical_image_key(url)
        if not key or key in seen_network_images:
            return
        seen_network_images.add(key)
        network_images.append({
            "src": url,
            "width": 0,
            "height": 0,
            "alt": "",
            "className": "network-image",
            "inNoteCarousel": True,
            "inRecommendation": False,
            "source": "network",
        })

    page.on("request", lambda request: remember_network_image(request.url))
    page.on("response", lambda response: remember_network_image(response.url))

    async def block_login_route(route):
        request = route.request
        url = request.url
        if request.resource_type == "document" and "xiaohongshu.com/login" in url:
            await route.abort()
            return
        await route.continue_()

    async def capture_page_data() -> dict[str, Any]:
        data = await page.evaluate(EXTRACT_PAGE_SCRIPT)
        data["networkImages"] = list(network_images)
        return data

    async def sample_best_page_data(sample_count: int) -> tuple[dict[str, Any], int]:
        best: dict[str, Any] = {}
        best_score_local = -1
        for _ in range(max(1, sample_count)):
            try:
                candidate = await capture_page_data()
                score = page_data_score(candidate)
                if score > best_score_local:
                    best = candidate
                    best_score_local = score
            except Exception:
                pass
            await page.wait_for_timeout(300)
        return best, best_score_local

    try:
        await page.route("**/*", block_login_route)
        best_page_data: dict[str, Any] = {}
        best_score = -1
        try:
            await page.goto(input_url, wait_until="commit", timeout=options.timeout_ms)
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        sample_count = max(4, min(12, (options.settle_ms // 300) + 2))
        best_page_data, best_score = await sample_best_page_data(sample_count)
        page_data = best_page_data or await capture_page_data()

        oia_target_url = resolve_oia_target_url(page_data.get("href", "") or page.url)
        if oia_target_url:
            try:
                await page.goto(oia_target_url, wait_until="commit", timeout=options.timeout_ms)
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass
            page_data, score = await sample_best_page_data(sample_count)
            if score > best_score:
                best_page_data = page_data
                best_score = score

        if not has_enough_note_content(best_page_data):
            for _ in range(max(0, options.scroll_times)):
                await page.mouse.wheel(0, 700)
                await page.wait_for_timeout(500)
                page_data = await page.evaluate(EXTRACT_PAGE_SCRIPT)
                page_data["networkImages"] = list(network_images)
                score = page_data_score(page_data)
                if score > best_score:
                    best_page_data = page_data
                    best_score = score
                if has_enough_note_content(best_page_data):
                    break
                oia_target_url = resolve_oia_target_url(page_data.get("href", "") or page.url)
                if oia_target_url:
                    try:
                        await page.goto(oia_target_url, wait_until="domcontentloaded", timeout=options.timeout_ms)
                        await page.wait_for_timeout(options.settle_ms)
                        page_data = await page.evaluate(EXTRACT_PAGE_SCRIPT)
                        score = page_data_score(page_data)
                        if score > best_score:
                            best_page_data = page_data
                            best_score = score
                    except PlaywrightTimeoutError:
                        pass
        best_page_data["networkImages"] = list(network_images)
        page_data = best_page_data
    finally:
        await context.close()

    return build_result(input_text_or_url, input_url, page_data)


async def extract_xhs_note(input_text_or_url: str, options: ExtractOptions | None = None) -> dict[str, Any]:
    """Extract public XHS note data with no-login desktop Playwright rendering."""
    options = options or ExtractOptions()
    playwright, browser = await launch_xhs_browser(options)
    try:
        return await extract_xhs_note_with_browser(input_text_or_url, browser, options)
    finally:
        await close_xhs_browser(playwright, browser)
