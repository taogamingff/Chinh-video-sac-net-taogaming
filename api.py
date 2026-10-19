import asyncio
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.getenv("VIDEO_WORK_DIR", "/tmp/vision4k_jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_SIZE = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
jobs = {}

app = FastAPI(title="Vision 4K Studio API", version="2.0.0")

# FRONTEND_ORIGIN có thể là "*" khi thử nghiệm.
# Khi đã có website, nên đặt thành https://ten-website.vercel.app
frontend_origin = os.getenv("FRONTEND_ORIGIN", "*")
origins = [x.strip() for x in frontend_origin.split(",") if x.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def cleanup(*paths):
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


async def probe_video(path: Path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "default=noprint_wrappers=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("Không đọc được video. Hãy thử file MP4 khác.")

    values = {}
    for line in stdout.decode(errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    try:
        width = int(values["width"])
        height = int(values["height"])
        duration = max(float(values.get("duration", "0")), 0.1)
    except (KeyError, ValueError):
        raise RuntimeError("Không xác định được kích thước hoặc thời lượng video.")

    return width, height, duration


def choose_output_size(width, height):
    # Nhận diện hướng video và không kéo méo hình.
    if width > height:
        return 3840, 2160, "ngang"
    if height > width:
        return 2160, 3840, "dọc"
    return 2160, 2160, "vuông"


async def process_job(job_id, input_path, output_path):
    job = jobs[job_id]
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("Máy chủ API chưa cài FFmpeg và FFprobe.")

        job.update(status="Đang phân tích khung hình...", progress=2)
        width, height, duration = await probe_video(input_path)
        out_w, out_h, orientation = choose_output_size(width, height)

        job.update(
            input_width=width,
            input_height=height,
            output_width=out_w,
            output_height=out_h,
            orientation=orientation,
            duration_seconds=round(duration, 2),
            status=f"Đã nhận diện video {orientation}. Đang xử lý...",
            progress=3,
        )

        video_filter = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,"
            "unsharp=5:5:0.7:3:3:0.25"
        )

        command = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(output_path),
        ]

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def drain_stderr():
            if proc.stderr:
                await proc.stderr.read()

        stderr_task = asyncio.create_task(drain_stderr())
        if proc.stdout:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                item = line.decode(errors="replace").strip()
                if item.startswith("out_time="):
                    try:
                        stamp = item.split("=", 1)[1]
                        hh, mm, ss = stamp.split(":")
                        seconds = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        progress = min(99, max(3, int(seconds / duration * 100)))
                        job.update(progress=progress, status=f"Đang xử lý video... {progress}%")
                    except (ValueError, TypeError):
                        pass

        code = await proc.wait()
        await stderr_task
        if code != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg xử lý thất bại. Hãy thử video ngắn hơn hoặc MP4.")

        job.update(
            state="completed",
            progress=100,
            status="Hoàn tất! Video đã sẵn sàng tải xuống.",
            output_name=f"Vision4K_{job_id[:8]}.mp4",
        )
    except Exception as exc:
        job.update(state="error", progress=0, status=str(exc))
        cleanup(output_path)
    finally:
        cleanup(input_path)


@app.get("/")
async def health():
    return {
        "name": "Vision 4K Studio API",
        "status": "online",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))}


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    filename = Path(file.filename or "video.mp4").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Định dạng không hỗ trợ. Hãy dùng MP4, MOV, MKV, WEBM hoặc AVI."
        )
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(
            status_code=503,
            detail="API chưa có FFmpeg. Hãy kiểm tra cấu hình máy chủ."
        )

    job_id = uuid.uuid4().hex
    input_path = WORK_DIR / f"{job_id}_input{extension}"
    output_path = WORK_DIR / f"{job_id}_4k.mp4"
    size = 0

    try:
        with input_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SIZE:
                    cleanup(input_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Video vượt giới hạn {MAX_SIZE // (1024 * 1024)} MB."
                    )
                output.write(chunk)
    finally:
        await file.close()

    jobs[job_id] = {
        "state": "processing",
        "status": "Đã nhận video, đang khởi tạo...",
        "progress": 1,
        "original_name": filename,
    }
    asyncio.create_task(process_job(job_id, input_path, output_path))
    return {"job_id": job_id, "state": "processing"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ.")
    return {
        "job_id": job_id,
        **job,
        "download_url": f"/api/jobs/{job_id}/download"
        if job.get("state") == "completed" else None,
    }


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("state") != "completed":
        raise HTTPException(status_code=404, detail="Video chưa sẵn sàng.")
    output_path = WORK_DIR / f"{job_id}_4k.mp4"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="File kết quả không còn tồn tại.")
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=job.get("output_name", "video_4k.mp4"),
        background=BackgroundTask(cleanup, output_path),
    )
