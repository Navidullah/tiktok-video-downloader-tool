import os
import uuid
import time
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

app = FastAPI()

# Load environment variables
load_dotenv()

# CORS configuration - allow your website
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGIN", "https://www.shopyor.com,https://shopyor.com,http://localhost:3000").split(",")

app.add_middleware(CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/download")
async def download_video(url: str = Query(...), format: str = Query("best")):
    try:
        # Format mapping for quality options
        format_map = {
            '360p': 'best[height<=360]/best',
            '720p': 'best[height<=720]/best',
            '1080p': 'best[height<=1080]/best',
            'best': 'best'
        }
        
        format_selector = format_map.get(format, 'best')
        
        print(f"Downloading: {url}")
        print(f"Format: {format} -> Selector: {format_selector}")
        
        # Get video info first
        info_opts = {
            'quiet': True,
            'skip_download': True,
        }
        
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"Video title: {info.get('title', 'Unknown')}")
        
        # Create unique filename
        unique_id = uuid.uuid4().hex[:8]
        output_template = f"temp_{unique_id}.%(ext)s"
        
        # Configure yt-dlp
        ydl_opts = {
            'format': format_selector,
            'outtmpl': output_template,
            'quiet': False,
            'merge_output_format': 'mp4',
            'retries': 10,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
        }
        
        # Download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Find downloaded file
        actual_file = None
        for f in os.listdir("."):
            if f.startswith(f"temp_{unique_id}") and f.endswith('.mp4'):
                actual_file = f
                break
        
        if not actual_file:
            raise HTTPException(status_code=500, detail="File not found after download")
        
        # Read file
        with open(actual_file, "rb") as f:
            video_data = f.read()
        
        # Clean up
        os.unlink(actual_file)
        
        # Generate clean filename
        timestamp = int(time.time())
        clean_filename = f"tiktok_video_{timestamp}.mp4"
        
        return StreamingResponse(
            iter([video_data]),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename=\"{clean_filename}\"",
                "Content-Length": str(len(video_data)),
                "Cache-Control": "no-cache"
            }
        )
        
    except Exception as e:
        print(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/")
async def root():
    return {
        "message": "TikTok Video Downloader API",
        "endpoints": {
            "/download": "Download TikTok videos",
            "params": {"url": "TikTok URL", "format": "360p, 720p, 1080p, best"}
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)