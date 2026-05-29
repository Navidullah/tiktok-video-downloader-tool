import asyncio
import os
import re
import time
import uuid
from urllib.parse import urlparse

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="TikTok Video Downloader API")

load_dotenv()

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGIN",
    "https://www.shopyor.com,https://shopyor.com,http://localhost:3000",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Desktop, mobile web, vm/vt app share links, and /t/ share links
TIKTOK_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+/?|"
    r"(?:www\.|m\.)?tiktok\.com/[^\s\"\'<>]+|"
    r"tiktok\.com/[^\s\"\'<>]+"
    r")",
    re.IGNORECASE,
)

# vt.tiktok.com/ABC123/ and vm.tiktok.com/ABC123/ (mobile app shares)
SHORT_TIKTOK_RE = re.compile(
    r"^https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+/?$",
    re.IGNORECASE,
)

CANONICAL_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/video/\d+",
    re.IGNORECASE,
)

HTML_CANONICAL_RE = re.compile(
    r'<(?:meta\s+property="og:url"\s+content="|link\s+rel="canonical"\s+href=")'
    r"(https?://[^\"']+)",
    re.IGNORECASE,
)

TIKTOK_HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }
)

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FORMAT_MAP = {
    "360p": "best[height<=360]/best",
    "720p": "best[height<=720]/best",
    "1080p": "best[height<=1080]/best",
    "best": "bestvideo*+bestaudio/best",
}

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov", ".m4a")


def normalize_input_url(raw: str) -> str:
    """Ensure scheme and trailing slash on vt/vm short links."""
    url = raw.strip().rstrip(".,);]\"'")
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    if SHORT_TIKTOK_RE.match(url) and not url.endswith("/"):
        url += "/"
    return url


def extract_tiktok_url(raw: str) -> str:
    """Pull a TikTok URL from pasted share text or a plain link."""
    raw = raw.strip()
    if not raw:
        raise ValueError("URL is empty")

    match = TIKTOK_URL_RE.search(raw)
    if match:
        return normalize_input_url(match.group(0))

    parsed = urlparse(normalize_input_url(raw))
    if parsed.netloc and is_tiktok_host(parsed.netloc):
        return normalize_input_url(raw)

    raise ValueError(
        "No valid TikTok URL found. Supported: www.tiktok.com, m.tiktok.com, "
        "vm.tiktok.com, vt.tiktok.com (e.g. https://vt.tiktok.com/ZSxg13ny4/), "
        "and mobile/desktop share links."
    )


def is_tiktok_host(hostname: str) -> bool:
    host = hostname.lower().removeprefix("www.")
    return host in {h.removeprefix("www.") for h in TIKTOK_HOSTS}


def is_short_tiktok_link(url: str) -> bool:
    return bool(SHORT_TIKTOK_RE.match(url))


def canonical_video_url(url: str) -> str | None:
    match = CANONICAL_VIDEO_RE.search(url)
    if match:
        return match.group(0).split("?")[0]
    return None


def find_video_url_in_html(html: str) -> str | None:
    for match in HTML_CANONICAL_RE.finditer(html):
        candidate = match.group(1).replace("&amp;", "&")
        found = canonical_video_url(candidate)
        if found:
            return found
    return canonical_video_url(html)


async def resolve_tiktok_url(url: str) -> str:
    """Follow redirects so vt/vm and other short links become canonical video URLs."""
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
    ) as client:
        response = await client.get(url, headers=headers)

    final = str(response.url)
    parsed = urlparse(final)

    if not is_tiktok_host(parsed.netloc):
        raise ValueError(f"Link did not resolve to TikTok (got: {parsed.netloc})")

    canonical = canonical_video_url(final)
    if canonical:
        return canonical

    canonical = find_video_url_in_html(response.text)
    if canonical:
        return canonical

    if "/photo/" in final:
        return final.split("?")[0]

    # yt-dlp can often resolve vt/vm links directly when HTTP redirect is incomplete
    if is_short_tiktok_link(url):
        return url

    return final.split("?")[0] if "/video/" in final else final


def build_ydl_opts(format_selector: str, output_template: str, download: bool) -> dict:
    opts = {
        "format": format_selector,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": DESKTOP_USER_AGENT,
            "Referer": "https://www.tiktok.com/",
        },
    }
    if not download:
        opts["skip_download"] = True
    return opts


def run_ytdlp(url: str, ydl_opts: dict) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=ydl_opts.get("skip_download") is not True)


async def run_ytdlp_with_fallback(urls: list[str], ydl_opts: dict) -> tuple[dict, str]:
    """Try resolved URL first, then original vt/vm short link if needed."""
    seen: set[str] = set()
    last_error: Exception | None = None

    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            info = await asyncio.to_thread(run_ytdlp, url, ydl_opts)
            return info, url
        except Exception as e:
            last_error = e

    raise last_error or RuntimeError("No URLs to download")


def find_downloaded_file(prefix: str) -> str | None:
    candidates = [f for f in os.listdir(".") if f.startswith(prefix)]
    for ext in VIDEO_EXTENSIONS:
        for name in candidates:
            if name.lower().endswith(ext):
                return name
    return candidates[0] if candidates else None


async def prepare_urls(raw_url: str) -> tuple[str, list[str]]:
    """Return resolved URL and ordered list of URLs to try with yt-dlp."""
    extracted = extract_tiktok_url(raw_url)
    resolved = await resolve_tiktok_url(extracted)
    candidates = [resolved]
    if extracted not in candidates:
        candidates.append(extracted)
    return resolved, candidates


@app.get("/")
async def root():
    return {
        "message": "TikTok Video Downloader API",
        "supported_urls": [
            "https://www.tiktok.com/@user/video/1234567890",
            "https://m.tiktok.com/v/1234567890.html",
            "https://vm.tiktok.com/XXXXXXXX/",
            "https://vt.tiktok.com/ZSxg13ny4/ (mobile app share links)",
            "https://www.tiktok.com/t/XXXXXXXX/",
            "Mobile app share text (URL is extracted automatically)",
        ],
        "endpoints": {
            "/download": {"url": "TikTok URL or share text", "format": "360p | 720p | 1080p | best"},
            "/info": {"url": "TikTok URL or share text"},
        },
    }


@app.get("/info")
async def video_info(url: str = Query(..., description="TikTok URL or pasted share text")):
    try:
        resolved_url, candidates = await prepare_urls(url)
        info, used_url = await run_ytdlp_with_fallback(
            candidates,
            build_ydl_opts("best", "temp.%(ext)s", download=False),
        )
        return {
            "resolved_url": resolved_url,
            "download_url": used_url,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "description": info.get("description"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch video info: {e}") from e


@app.get("/download")
async def download_video(
    url: str = Query(..., description="TikTok URL or pasted share text from app/desktop"),
    format: str = Query("best"),
):
    actual_file = None
    try:
        resolved_url, candidates = await prepare_urls(url)
        format_selector = FORMAT_MAP.get(format, FORMAT_MAP["best"])
        unique_id = uuid.uuid4().hex[:8]
        output_template = f"temp_{unique_id}.%(ext)s"
        ydl_opts = build_ydl_opts(format_selector, output_template, download=True)

        info, used_url = await run_ytdlp_with_fallback(candidates, ydl_opts)
        actual_file = find_downloaded_file(f"temp_{unique_id}")

        if not actual_file:
            raise HTTPException(status_code=500, detail="File not found after download")

        with open(actual_file, "rb") as f:
            video_data = f.read()

        title = info.get("title") or "tiktok_video"
       safe_title = re.sub(r"[^\w\s-]", "", title, flags=re.ASCII).strip().replace(" ", "_")[:50]
        timestamp = int(time.time())
        clean_filename = f"{safe_title or 'tiktok_video'}_{timestamp}.mp4"

        return StreamingResponse(
            iter([video_data]),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{clean_filename}"',
                "Content-Length": str(len(video_data)),
                "Cache-Control": "no-cache",
                "X-Resolved-Url": resolved_url,
                "X-Download-Url": used_url,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download error: {e}") from e
    finally:
        if actual_file and os.path.exists(actual_file):
            os.unlink(actual_file)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
