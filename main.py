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

app = FastAPI(title="Video Downloader API")

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

# ── TikTok patterns ──────────────────────────────────────────────────────────

TIKTOK_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+/?|"
    r"(?:www\.|m\.)?tiktok\.com/[^\s\"\'<>]+|"
    r"tiktok\.com/[^\s\"\'<>]+"
    r")",
    re.IGNORECASE,
)

SHORT_TIKTOK_RE = re.compile(
    r"^https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+/?$",
    re.IGNORECASE,
)

CANONICAL_TIKTOK_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/video/\d+",
    re.IGNORECASE,
)

HTML_CANONICAL_RE = re.compile(
    r'<(?:meta\s+property="og:url"\s+content="|link\s+rel="canonical"\s+href=")'
    r"(https?://[^\"']+)",
    re.IGNORECASE,
)

TIKTOK_HOSTS = frozenset({
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
})

# ── Instagram patterns ───────────────────────────────────────────────────────

# Matches posts (/p/), reels (/reel/, /reels/), IGTV (/tv/), and stories (/stories/user/)
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv|stories/[^/\s]+)/[A-Za-z0-9_\-]+/?[^\s\"\'<>]*",
    re.IGNORECASE,
)

INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})

# ── Shared constants ─────────────────────────────────────────────────────────

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

# ── TikTok helpers ───────────────────────────────────────────────────────────

def normalize_tiktok_url(raw: str) -> str:
    url = raw.strip().rstrip(".,);]\"'")
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    if SHORT_TIKTOK_RE.match(url) and not url.endswith("/"):
        url += "/"
    return url


def extract_tiktok_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("URL is empty")
    match = TIKTOK_URL_RE.search(raw)
    if match:
        return normalize_tiktok_url(match.group(0))
    parsed = urlparse(normalize_tiktok_url(raw))
    if parsed.netloc.lower() in TIKTOK_HOSTS:
        return normalize_tiktok_url(raw)
    raise ValueError("No valid TikTok URL found")


def is_short_tiktok_link(url: str) -> bool:
    return bool(SHORT_TIKTOK_RE.match(url))


def canonical_tiktok_url(url: str) -> str | None:
    match = CANONICAL_TIKTOK_RE.search(url)
    if match:
        return match.group(0).split("?")[0]
    return None


def find_tiktok_url_in_html(html: str) -> str | None:
    for match in HTML_CANONICAL_RE.finditer(html):
        candidate = match.group(1).replace("&amp;", "&")
        found = canonical_tiktok_url(candidate)
        if found:
            return found
    return canonical_tiktok_url(html)


async def resolve_tiktok_url(url: str) -> str:
    """Follow redirects so vt/vm short links resolve to canonical video URLs."""
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(url, headers=headers)

    final = str(response.url)
    parsed = urlparse(final)

    if parsed.netloc.lower() not in TIKTOK_HOSTS:
        raise ValueError(f"Link did not resolve to TikTok (got: {parsed.netloc})")

    canonical = canonical_tiktok_url(final)
    if canonical:
        return canonical

    canonical = find_tiktok_url_in_html(response.text)
    if canonical:
        return canonical

    if "/photo/" in final:
        return final.split("?")[0]

    if is_short_tiktok_link(url):
        return url

    return final.split("?")[0] if "/video/" in final else final


# ── Instagram helpers ────────────────────────────────────────────────────────

def extract_instagram_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("URL is empty")
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    match = INSTAGRAM_URL_RE.search(raw)
    if match:
        return match.group(0).rstrip(".,);]\"'").split("?")[0]
    parsed = urlparse(raw)
    if parsed.netloc.lower() in INSTAGRAM_HOSTS:
        return raw.split("?")[0]
    raise ValueError(
        "No valid Instagram URL found. Supported formats: "
        "/p/ (posts), /reel/ (reels), /reels/, /tv/ (IGTV), /stories/"
    )


# ── yt-dlp helpers ───────────────────────────────────────────────────────────

def build_ydl_opts(
    format_selector: str, output_template: str, download: bool, platform: str = "tiktok"
) -> dict:
    referer = "https://www.instagram.com/" if platform == "instagram" else "https://www.tiktok.com/"
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
            "Referer": referer,
        },
    }
    if not download:
        opts["skip_download"] = True
    return opts


def run_ytdlp(url: str, ydl_opts: dict) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=ydl_opts.get("skip_download") is not True)


async def run_ytdlp_with_fallback(urls: list[str], ydl_opts: dict) -> tuple[dict, str]:
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
    raise last_error or RuntimeError("No URLs to try")


def find_downloaded_file(prefix: str) -> str | None:
    candidates = [f for f in os.listdir(".") if f.startswith(prefix)]
    for ext in VIDEO_EXTENSIONS:
        for name in candidates:
            if name.lower().endswith(ext):
                return name
    return candidates[0] if candidates else None


# ── URL preparation (platform-aware) ─────────────────────────────────────────

async def prepare_urls(raw_url: str) -> tuple[str, list[str], str]:
    """Detect platform, validate & resolve URL. Returns (resolved_url, candidates, platform)."""
    url = raw_url.strip()
    if not url:
        raise ValueError("URL is empty")

    # TikTok — try first because share text may embed a TikTok link
    try:
        extracted = extract_tiktok_url(raw_url)
        resolved = await resolve_tiktok_url(extracted)
        candidates = [resolved]
        if extracted not in candidates:
            candidates.append(extracted)
        return resolved, candidates, "tiktok"
    except ValueError:
        pass

    # Instagram
    try:
        extracted = extract_instagram_url(raw_url)
        return extracted, [extracted], "instagram"
    except ValueError:
        pass

    raise ValueError(
        "No supported URL found. Please paste an Instagram or TikTok video link.\n"
        "Instagram: /p/, /reel/, /reels/, /tv/, /stories/\n"
        "TikTok: www.tiktok.com, vm.tiktok.com, vt.tiktok.com"
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "Video Downloader API — supports Instagram & TikTok",
        "supported_platforms": {
            "instagram": [
                "https://www.instagram.com/p/XXXXXXXX/ (posts)",
                "https://www.instagram.com/reel/XXXXXXXX/ (reels)",
                "https://www.instagram.com/reels/XXXXXXXX/ (reels)",
                "https://www.instagram.com/tv/XXXXXXXX/ (IGTV)",
                "https://www.instagram.com/stories/username/XXXXXXXX/ (stories)",
            ],
            "tiktok": [
                "https://www.tiktok.com/@user/video/1234567890",
                "https://m.tiktok.com/v/1234567890.html",
                "https://vm.tiktok.com/XXXXXXXX/",
                "https://vt.tiktok.com/ZSxg13ny4/ (mobile app share links)",
                "https://www.tiktok.com/t/XXXXXXXX/",
                "Mobile share text (URL extracted automatically)",
            ],
        },
        "endpoints": {
            "/download": {
                "url": "Instagram or TikTok URL",
                "format": "360p | 720p | 1080p | best",
            },
            "/info": {"url": "Instagram or TikTok URL"},
        },
        "notes": {
            "instagram_stories": "Stories require the account to be public.",
            "instagram_private": "Private posts cannot be downloaded without authentication.",
        },
    }


@app.get("/info")
async def video_info(url: str = Query(..., description="Instagram or TikTok URL")):
    try:
        resolved_url, candidates, platform = await prepare_urls(url)
        info, used_url = await run_ytdlp_with_fallback(
            candidates,
            build_ydl_opts("best", "temp.%(ext)s", download=False, platform=platform),
        )
        return {
            "platform": platform,
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
    url: str = Query(..., description="Instagram or TikTok URL"),
    format: str = Query("best"),
):
    actual_file = None
    try:
        resolved_url, candidates, platform = await prepare_urls(url)
        format_selector = FORMAT_MAP.get(format, FORMAT_MAP["best"])
        unique_id = uuid.uuid4().hex[:8]
        output_template = f"temp_{unique_id}.%(ext)s"
        ydl_opts = build_ydl_opts(format_selector, output_template, download=True, platform=platform)

        info, used_url = await run_ytdlp_with_fallback(candidates, ydl_opts)
        actual_file = find_downloaded_file(f"temp_{unique_id}")

        if not actual_file:
            raise HTTPException(status_code=500, detail="File not found after download")

        with open(actual_file, "rb") as f:
            video_data = f.read()

        title = info.get("title") or f"{platform}_video"
        safe_title = re.sub(r"[^\w\s-]", "", title, flags=re.ASCII).strip().replace(" ", "_")[:50]
        timestamp = int(time.time())
        clean_filename = f"{safe_title or f'{platform}_video'}_{timestamp}.mp4"

        return StreamingResponse(
            iter([video_data]),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{clean_filename}"',
                "Content-Length": str(len(video_data)),
                "Cache-Control": "no-cache",
                "X-Platform": platform,
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
