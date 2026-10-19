
# api.py
import os
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Video 4K Enhancement API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

BASE_DIR = Path("video_jobs")
BASE_DIR.mkdir(exist_ok=True)

MAX_SIZE = 500 * 1024 * 1024  # 500 MB
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


@app.get("/")
def home():
    return {
        "name": "Video 4K API",
        "status": "online",
        "resolution": "3840x2160",
    }


@app.post("/api/enhance-4k")
async def enhance_video(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Định dạng video không được hỗ trợ."
        )

    job_id = uuid.uuid4().hex
    input_path = BASE_DIR / f"{job_id}_input{ext}"
    output_path = BASE_DIR / f"{job_id}_4k.mp4"

    try:
        # Lưu file theo từng khối, giới hạn dung lượng upload
        size = 0
        with input_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)
                if size > MAX_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Video vượt quá giới hạn 500 MB."
                    )
                f.write(chunk)

        await file.close()

        # Kiểm tra FFmpeg có hoạt động không
        if not shutil.which("ffmpeg"):
            raise HTTPException(
                status_code=500,
                detail="Máy chủ chưa cài đặt FFmpeg."
            )

        command = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf",
            (
                "scale=3840:2160:"
                "force_original_aspect_ratio=decrease:"
                "force_divisible_by=2,"
                "pad=3840:2160:(ow-iw)/2:(oh-ih)/2,"
                "unsharp=5:5:0.8:3:3:0.4"
            ),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]

        result = await __import__("asyncio").create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await result.communicate()

        if result.returncode != 0 or not output_path.exists():
            error = stderr.decode(errors="replace")[-1500:]
            raise HTTPException(
                status_code=500,
                detail=f"Không xử lý được video: {error}"
            )

        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename="video_4k.mp4",
            background=BackgroundTask(
                lambda: cleanup(input_path, output_path)
            ),
        )

    except HTTPException:
        cleanup(input_path, output_path)
        raise
    except Exception as exc:
        cleanup(input_path, output_path)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        await file.close()


def cleanup(*paths):
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


# Bổ sung import asyncio ở đầu file
import asyncio
