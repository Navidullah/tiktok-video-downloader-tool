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
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

load_dotenv()

app = FastAPI(
    title="TikTok & Instagram Video Downloader API",
    description="Download videos, audio, and thumbnails from TikTok and Instagram.",
    version="2.0.0",
)

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

# ── TikTok patterns ───────────────────────────────────────────────────────────

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

CANONICAL_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/video/\d+",
    re.IGNORECASE,
)

HTML_CANONICAL_RE = re.compile(
    r'<(?:meta\s+property="og:url"\s+content="|link\s+rel="canonical"\s+href=")'
    r"(https?://[^\"']+)",
    re.IGNORECASE,
)

TIKTOK_HOSTS = frozenset({
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
})

# ── Instagram patterns ────────────────────────────────────────────────────────

# Matches posts (/p/), reels (/reel/, /reels/), IGTV (/tv/), and stories (/stories/user/)
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv|stories/[^/\s]+)/[A-Za-z0-9_\-]+/?[^\s\"\'<>]*",
    re.IGNORECASE,
)

INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})

# ── Shared constants ──────────────────────────────────────────────────────────

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

VIDEO_FORMAT_MAP = {
    "360p": "best[height<=360]/best",
    "480p": "best[height<=480]/best",
    "720p": "best[height<=720]/best",
    "1080p": "best[height<=1080]/best",
    "best": "bestvideo*+bestaudio/best",
}

AUDIO_FORMATS = frozenset({"mp3", "m4a"})
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov", ".m4a")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".ogg", ".opus", ".webm", ".aac")

# TTL-based in-memory cache for /info responses
_info_cache: dict[str, tuple[dict, float]] = {}
INFO_CACHE_TTL = 300  # seconds

# Limit concurrent platform requests in batch operations
_batch_semaphore = asyncio.Semaphore(3)


# ── TikTok helpers ────────────────────────────────────────────────────────────

def normalize_input_url(raw: str) -> str:
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
        return normalize_input_url(match.group(0))
    parsed = urlparse(normalize_input_url(raw))
    if parsed.netloc and is_tiktok_host(parsed.netloc):
        return normalize_input_url(raw)
    raise ValueError("No valid TikTok URL found")


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
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
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
    if is_short_tiktok_link(url):
        return url
    return final.split("?")[0] if "/video/" in final else final


# ── Instagram helpers ─────────────────────────────────────────────────────────

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
        "No valid Instagram URL found. Supported: /p/ (posts), /reel/ (reels), "
        "/reels/, /tv/ (IGTV), /stories/"
    )


# ── Shared helpers ────────────────────────────────────────────────────────────

def safe_filename(title: str, fallback: str = "video") -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title or "", flags=re.ASCII).strip().replace(" ", "_")
    return cleaned[:50] if cleaned else fallback


def build_ydl_opts(
    format_selector: str,
    output_template: str,
    *,
    download: bool = True,
    no_watermark: bool = False,
    audio_only: bool = False,
    audio_format: str = "mp3",
    platform: str = "tiktok",
) -> dict:
    referer = "https://www.instagram.com/" if platform == "instagram" else "https://www.tiktok.com/"
    opts: dict = {
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

    if no_watermark and platform == "tiktok":
        # Prefer download_addr format IDs which are typically watermark-free on TikTok
        height_match = re.search(r"height<=(\d+)", format_selector)
        if height_match:
            h = height_match.group(1)
            opts["format"] = (
                f"bestvideo[format_id*=download][height<={h}]+bestaudio"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]/best"
            )
        else:
            opts["format"] = "bestvideo[format_id*=download]+bestaudio/bestvideo+bestaudio/best"

    if audio_only:
        opts["format"] = "bestaudio/best"
        opts.pop("merge_output_format", None)
        opts["keepvideo"] = False
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": "192",
        }]

    return opts


def run_ytdlp(url: str, ydl_opts: dict) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=not ydl_opts.get("skip_download", False))


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


def find_downloaded_file(prefix: str, extensions: tuple[str, ...] = VIDEO_EXTENSIONS) -> str | None:
    candidates = [f for f in os.listdir(".") if f.startswith(prefix)]
    for ext in extensions:
        for name in candidates:
            if name.lower().endswith(ext):
                return name
    return candidates[0] if candidates else None


async def prepare_urls(raw_url: str) -> tuple[str, list[str], str]:
    """Detect platform, validate and resolve URL. Returns (resolved_url, candidates, platform)."""
    url = raw_url.strip()
    if not url:
        raise ValueError("URL is empty")

    # TikTok first — share text may embed a TikTok link
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
        "No supported URL found. Please paste a TikTok or Instagram video link.\n"
        "TikTok: www.tiktok.com, vm.tiktok.com, vt.tiktok.com\n"
        "Instagram: /p/, /reel/, /reels/, /tv/, /stories/"
    )


def extract_video_metadata(info: dict) -> dict:
    formats = [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "width": f.get("width"),
            "fps": f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
        }
        for f in (info.get("formats") or [])
        if f.get("vcodec") not in (None, "none")
    ]
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "repost_count": info.get("repost_count"),
        "thumbnail": info.get("thumbnail"),
        "thumbnails": [t.get("url") for t in (info.get("thumbnails") or []) if t.get("url")],
        "tags": info.get("tags") or [],
        "age_limit": info.get("age_limit"),
        "webpage_url": info.get("webpage_url"),
        "is_photo_slideshow": "/photo/" in (info.get("webpage_url") or ""),
        "formats": formats,
    }


def _maybe_prune_cache() -> None:
    if len(_info_cache) > 100:
        oldest = sorted(_info_cache, key=lambda k: _info_cache[k][1])[:50]
        for key in oldest:
            del _info_cache[key]


def file_stream_and_cleanup(path: str, chunk_size: int = 1024 * 1024):
    """Yield file chunks then delete the file."""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "name": "TikTok & Instagram Video Downloader API",
        "version": "2.0.0",
        "supported_platforms": {
            "tiktok": [
                "https://www.tiktok.com/@user/video/1234567890",
                "https://m.tiktok.com/v/1234567890.html",
                "https://vm.tiktok.com/XXXXXXXX/",
                "https://vt.tiktok.com/ZSxg13ny4/",
                "https://www.tiktok.com/t/XXXXXXXX/",
                "Mobile app share text (URL extracted automatically)",
            ],
            "instagram": [
                "https://www.instagram.com/p/XXXXXXXX/ (posts)",
                "https://www.instagram.com/reel/XXXXXXXX/ (reels)",
                "https://www.instagram.com/reels/XXXXXXXX/",
                "https://www.instagram.com/tv/XXXXXXXX/ (IGTV)",
                "https://www.instagram.com/stories/username/XXXXXXXX/ (public only)",
            ],
        },
        "endpoints": {
            "GET /health": "Service health check",
            "GET /info": {
                "params": {"url": "TikTok or Instagram URL"},
                "returns": "Full metadata: title, uploader, views, likes, formats, thumbnails",
            },
            "GET /download": {
                "params": {
                    "url": "TikTok or Instagram URL",
                    "format": "360p | 480p | 720p | 1080p | best (default: best)",
                    "no_watermark": "true | false — TikTok only, prefer watermark-free source (default: false)",
                }
            },
            "GET /download/audio": {
                "params": {
                    "url": "TikTok or Instagram URL",
                    "audio_format": "mp3 | m4a (default: mp3) — requires ffmpeg installed",
                }
            },
            "GET /thumbnail": {
                "params": {"url": "TikTok or Instagram URL"},
                "returns": "JPEG thumbnail image download",
            },
            "POST /batch/info": {
                "body": {"urls": ["url1", "url2", "..."]},
                "notes": "Max 10 URLs per request; per-URL errors included in results",
            },
        },
        "notes": {
            "instagram_private": "Private Instagram posts cannot be downloaded without authentication.",
            "audio_extraction": "Requires ffmpeg to be installed on the server.",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time())}


@app.get("/info")
async def video_info(url: str = Query(..., description="TikTok or Instagram URL")):
    cache_key = url.strip()
    if cache_key in _info_cache:
        cached_data, ts = _info_cache[cache_key]
        if time.time() - ts < INFO_CACHE_TTL:
            return {**cached_data, "cached": True}

    try:
        resolved_url, candidates, platform = await prepare_urls(url)
        info, used_url = await run_ytdlp_with_fallback(
            candidates,
            build_ydl_opts("best", "temp.%(ext)s", download=False, platform=platform),
        )
        metadata = extract_video_metadata(info)
        result = {
            "platform": platform,
            "resolved_url": resolved_url,
            "download_url": used_url,
            **metadata,
            "cached": False,
        }
        _maybe_prune_cache()
        _info_cache[cache_key] = (result, time.time())
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch video info: {e}") from e


@app.get("/download")
async def download_video(
    url: str = Query(..., description="TikTok or Instagram URL"),
    format: str = Query("best", description="Quality: 360p | 480p | 720p | 1080p | best"),
    no_watermark: bool = Query(False, description="TikTok only: prefer watermark-free source"),
):
    actual_file: str | None = None
    try:
        resolved_url, candidates, platform = await prepare_urls(url)
        format_selector = VIDEO_FORMAT_MAP.get(format, VIDEO_FORMAT_MAP["best"])
        unique_id = uuid.uuid4().hex[:8]
        output_template = f"temp_{unique_id}.%(ext)s"

        ydl_opts = build_ydl_opts(
            format_selector,
            output_template,
            download=True,
            no_watermark=no_watermark,
            platform=platform,
        )

        info, used_url = await run_ytdlp_with_fallback(candidates, ydl_opts)
        actual_file = find_downloaded_file(f"temp_{unique_id}")

        if not actual_file:
            raise HTTPException(status_code=500, detail="Downloaded file not found")

        file_size = os.path.getsize(actual_file)
        title = info.get("title") or f"{platform}_video"
        filename = f"{safe_filename(title, f'{platform}_video')}_{int(time.time())}.mp4"

        # Hand file ownership to the stream generator; clear local ref so finally won't double-delete
        file_to_stream, actual_file = actual_file, None
        return StreamingResponse(
            file_stream_and_cleanup(file_to_stream),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
                "X-Platform": platform,
                "X-Resolved-Url": resolved_url,
                "X-Download-Url": used_url,
                "X-No-Watermark": str(no_watermark).lower(),
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
            try:
                os.unlink(actual_file)
            except OSError:
                pass


@app.get("/download/audio")
async def download_audio(
    url: str = Query(..., description="TikTok or Instagram URL"),
    audio_format: str = Query("mp3", description="Audio format: mp3 | m4a (requires ffmpeg)"),
):
    actual_file: str | None = None
    try:
        if audio_format not in AUDIO_FORMATS:
            raise ValueError(f"audio_format must be one of: {', '.join(sorted(AUDIO_FORMATS))}")

        resolved_url, candidates, platform = await prepare_urls(url)
        unique_id = uuid.uuid4().hex[:8]
        output_template = f"temp_{unique_id}.%(ext)s"

        ydl_opts = build_ydl_opts(
            "bestaudio/best",
            output_template,
            download=True,
            audio_only=True,
            audio_format=audio_format,
            platform=platform,
        )

        info, _ = await run_ytdlp_with_fallback(candidates, ydl_opts)
        actual_file = find_downloaded_file(f"temp_{unique_id}", extensions=AUDIO_EXTENSIONS)

        if not actual_file:
            raise HTTPException(status_code=500, detail="Audio file not found after extraction")

        file_size = os.path.getsize(actual_file)
        title = info.get("title") or f"{platform}_audio"
        filename = f"{safe_filename(title, f'{platform}_audio')}_{int(time.time())}.{audio_format}"
        mime = "audio/mpeg" if audio_format == "mp3" else "audio/mp4"

        file_to_stream, actual_file = actual_file, None
        return StreamingResponse(
            file_stream_and_cleanup(file_to_stream),
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
                "X-Platform": platform,
                "X-Resolved-Url": resolved_url,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio extraction error: {e}") from e
    finally:
        if actual_file and os.path.exists(actual_file):
            try:
                os.unlink(actual_file)
            except OSError:
                pass


@app.get("/thumbnail")
async def download_thumbnail(url: str = Query(..., description="TikTok or Instagram URL")):
    try:
        resolved_url, candidates, platform = await prepare_urls(url)
        info, _ = await run_ytdlp_with_fallback(
            candidates,
            build_ydl_opts("best", "temp.%(ext)s", download=False, platform=platform),
        )

        thumbnail_url: str | None = info.get("thumbnail")
        if not thumbnail_url:
            thumbnails = info.get("thumbnails") or []
            thumbnail_url = thumbnails[-1].get("url") if thumbnails else None

        if not thumbnail_url:
            raise HTTPException(status_code=404, detail="No thumbnail available for this video")

        referer = "https://www.instagram.com/" if platform == "instagram" else "https://www.tiktok.com/"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(
                thumbnail_url,
                headers={"User-Agent": DESKTOP_USER_AGENT, "Referer": referer},
                follow_redirects=True,
            )

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch thumbnail")

        title = info.get("title") or "thumbnail"
        filename = f"{safe_filename(title, f'{platform}_thumbnail')}_{int(time.time())}.jpg"

        return Response(
            content=resp.content,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(resp.content)),
                "Cache-Control": "no-cache",
                "X-Platform": platform,
                "X-Resolved-Url": resolved_url,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Thumbnail error: {e}") from e


class BatchInfoRequest(BaseModel):
    urls: list[str]


@app.post("/batch/info")
async def batch_video_info(body: BatchInfoRequest):
    if not body.urls:
        raise HTTPException(status_code=400, detail="urls list is empty")
    if len(body.urls) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 URLs per batch request")

    async def fetch_one(raw_url: str) -> dict:
        async with _batch_semaphore:
            try:
                resolved_url, candidates, platform = await prepare_urls(raw_url)
                info, used_url = await run_ytdlp_with_fallback(
                    candidates,
                    build_ydl_opts("best", "temp.%(ext)s", download=False, platform=platform),
                )
                return {
                    "url": raw_url,
                    "platform": platform,
                    "resolved_url": resolved_url,
                    "download_url": used_url,
                    **extract_video_metadata(info),
                    "error": None,
                }
            except Exception as e:
                return {"url": raw_url, "error": str(e)}

    results = await asyncio.gather(*[fetch_one(u) for u in body.urls])
    return {"results": list(results), "count": len(results)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
