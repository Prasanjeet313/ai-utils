
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Reel Merger API")

API_KEY = os.environ.get("MERGE_API_KEY", "")


@app.get("/")
def health():
    return {"status": "ok", "service": "reel-merger-api"}


def run_command(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode != 0:
        # Keep server logs useful without returning secrets to callers.
        print("FFmpeg error:", result.stderr[-5000:])
        raise HTTPException(
            status_code=500,
            detail="Video processing failed. Check Render logs.",
        )


def duration_seconds(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail="Could not read media duration.")
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise HTTPException(status_code=500, detail="Invalid media duration.")


@app.post("/merge")
def merge(
    scene1: Annotated[UploadFile, File()],
    scene2: Annotated[UploadFile, File()],
    scene3: Annotated[UploadFile, File()],
    scene4: Annotated[UploadFile, File()],
    voiceover: Annotated[UploadFile, File()],
    x_api_key: Annotated[str | None, Header()] = None,
):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server API key is not configured.")

    if x_api_key is None or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    uploads = [
        ("scene1.mp4", scene1),
        ("scene2.mp4", scene2),
        ("scene3.mp4", scene3),
        ("scene4.mp4", scene4),
        ("voiceover.mp3", voiceover),
    ]

    temp_dir = Path(tempfile.mkdtemp(prefix="reel-"))
    try:
        for filename, upload in uploads:
            content_type = (upload.content_type or "").lower()
            if filename.endswith(".mp4") and content_type not in (
                "video/mp4", "application/octet-stream"
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"{filename} must be an MP4 file.",
                )
            if filename.endswith(".mp3") and content_type not in (
                "audio/mpeg", "audio/mp3", "application/octet-stream"
            ):
                raise HTTPException(
                    status_code=400,
                    detail="voiceover must be an MP3 file.",
                )

            destination = temp_dir / filename
            total = 0
            with destination.open("wb") as output:
                while True:
                    chunk = upload.file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 100 * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail=f"{filename} exceeds the 100 MB file limit.",
                        )
                    output.write(chunk)

        # Normalize all four clips, then concatenate their video streams.
        inputs = []
        filters = []
        concat_inputs = []
        for index in range(1, 5):
            inputs.extend(["-i", str(temp_dir / f"scene{index}.mp4")])
            filters.append(
                f"[{index - 1}:v]"
                "scale=720:1280:force_original_aspect_ratio=decrease,"
                "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
                "fps=30,setsar=1,format=yuv420p,"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            concat_inputs.append(f"[v{index}]")

        filters.append(
            "".join(concat_inputs) + "concat=n=4:v=1:a=0[vout]"
        )

        joined = temp_dir / "joined.mp4"
        run_command([
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "+faststart",
            str(joined),
        ])

        audio = temp_dir / "voiceover.mp3"
        video_duration = duration_seconds(joined)
        audio_duration = duration_seconds(audio)

        # If narration is longer, freeze the last video frame to fill the gap.
        extra = max(0.0, audio_duration - video_duration)
        final_path = temp_dir / "final_reel.mp4"

        run_command([
            "ffmpeg", "-y",
            "-i", str(joined),
            "-i", str(audio),
            "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            str(final_path),
        ])

        return FileResponse(
            path=final_path,
            media_type="video/mp4",
            filename="final_reel.mp4",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )

    except Exception:
        # The response file is handled by its own cleanup task on success.
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        for _, upload in uploads:
            upload.file.close()
