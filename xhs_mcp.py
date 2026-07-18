from mcp.server.fastmcp import FastMCP, Image
from starlette.routing import Route
from starlette.responses import Response
import httpx
import json
import hashlib
import time
import io
import uvicorn

mcp = FastMCP("xiaohongshu")
API_URL = "http://127.0.0.1:8273/xiaohongshu"

_img_cache = {}
PROXY_BASE = "https://xhs.shawbb.cn"
XHS_HEADERS = {
    "referer": "https://www.xiaohongshu.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}

THUMB_WIDTH = 640
THUMB_QUALITY = 55
MAX_THUMBS = 9


async def img_proxy(request):
    key = request.path_params["key"]
    entry = _img_cache.get(key)
    if not entry:
        return Response("not found", status_code=404)
    async with httpx.AsyncClient(timeout=20, headers=XHS_HEADERS) as c:
        r = await c.get(entry["url"])
        if r.status_code != 200:
            return Response("fetch failed", status_code=502)
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(r.content, media_type=ct, headers={
            "cache-control": "public, max-age=3600",
            "access-control-allow-origin": "*",
        })


def cache_images(images):
    proxy_urls = []
    for img in images:
        url = img.get("url") if isinstance(img, dict) else img
        if not url:
            continue
        key = hashlib.md5(url.encode()).hexdigest()[:12]
        _img_cache[key] = {"url": url, "ts": time.time()}
        proxy_urls.append({
            "index": img.get("index", len(proxy_urls)),
            "url": f"{PROXY_BASE}/img/{key}",
            "width": img.get("width"),
            "height": img.get("height"),
        })
    now = time.time()
    for k in list(_img_cache):
        if now - _img_cache[k]["ts"] > 7200:
            del _img_cache[k]
    return proxy_urls


async def fetch_thumbnail(url: str) -> bytes | None:
    from PIL import Image as PILImage
    try:
        async with httpx.AsyncClient(timeout=15, headers=XHS_HEADERS) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return None
        img = PILImage.open(io.BytesIO(r.content))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if w > THUMB_WIDTH:
            ratio = THUMB_WIDTH / w
            img = img.resize((THUMB_WIDTH, int(h * ratio)), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def sort_comments(data, limit=30):
    comments = data.get("comments", [])
    if not isinstance(comments, list):
        return data
    for cm in comments:
        try:
            raw = cm.get("likes", "0")
            if raw is None:
                raw = "0"
            raw = str(raw).replace("万", "0000").replace("+", "").replace("赞", "0")
            cm["_sort"] = int(raw) if raw.isdigit() else 0
        except Exception:
            cm["_sort"] = 0
    data["comments"] = sorted(comments, key=lambda x: x.get("_sort", 0), reverse=True)[:limit]
    for cm in data["comments"]:
        cm.pop("_sort", None)
    return data


@mcp.tool()
async def xhs_feed(n: int = 10) -> str:
    """刷小红书发现页，返回推荐笔记列表。n 控制数量，最多30"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(API_URL, json={"action": "feed", "n": min(n, 30)})
        return r.text


@mcp.tool()
async def xhs_search(query: str, n: int = 10) -> str:
    """搜索小红书笔记。query 是关键词，n 控制数量"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(API_URL, json={"action": "search", "query": query, "n": min(n, 30)})
        return r.text


@mcp.tool()
async def xhs_read(url: str, n: int = 20) -> list:
    """读取笔记详情、图片和评论。url 支持完整链接或 xhslink.com 短链。返回笔记文字内容 + 缩略图（可直接看到图片）+ 全尺寸代理链接"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(API_URL, json={"action": "read", "url": url, "n": min(n, 40)})
        data = json.loads(r.text)

    data = sort_comments(data, limit=30)

    result = []

    if data.get("ok"):
        note = data.get("note", {})
        images = note.get("images", [])
        if images:
            data["image_proxies"] = cache_images(images)

            urls = [img.get("url") for img in images if img.get("url")]
            for i, img_url in enumerate(urls[:MAX_THUMBS]):
                thumb = await fetch_thumbnail(img_url)
                if thumb:
                    result.append(Image(data=thumb, format="jpeg"))

    result.insert(0, json.dumps(data, ensure_ascii=False))
    return result


@mcp.tool()
async def xhs_profile(url: str = "", user: str = "", n: int = 10) -> str:
    """查看用户资料和最近笔记。传 url 或 user(24位id)"""
    body = {"action": "profile", "n": min(n, 30)}
    if url:
        body["url"] = url
    if user:
        body["user"] = user
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(API_URL, json=body)
        return r.text


if __name__ == "__main__":
    mcp_app = mcp.streamable_http_app()
    mcp_app.routes.insert(0, Route("/img/{key}", img_proxy, methods=["GET"]))
    uvicorn.run(mcp_app, host="127.0.0.1", port=8274)
