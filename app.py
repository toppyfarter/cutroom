from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
import yt_dlp

APP_DIR = Path(os.environ.get("CUTROOM_HOME", Path(__file__).resolve().parent)).resolve()
SETTINGS_PATH = APP_DIR / "settings.json"
DEFAULT_DOWNLOAD_ROOT = Path.home() / "Downloads"


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def current_download_root() -> Path:
    raw = load_settings().get("download_dir")
    try:
        candidate = Path(raw).expanduser() if raw else DEFAULT_DOWNLOAD_ROOT
        if not candidate.is_absolute():
            return DEFAULT_DOWNLOAD_ROOT
        return candidate.resolve()
    except (OSError, TypeError, ValueError):
        return DEFAULT_DOWNLOAD_ROOT


def save_download_root(path: Path) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps({"download_dir": str(path.resolve())}, indent=2), encoding="utf-8")


app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))
LAST_CLIENT_HEARTBEAT = 0.0
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac", "opus"}
VIDEO_FORMATS = {"mp4", "mkv", "webm", "mov", "avi"}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
VIDEO_PREVIEWS: dict[str, Path] = {}
VIDEO_PREVIEWS_LOCK = threading.Lock()


def validate_url(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 2048:
        raise ValueError("Paste a YouTube video, playlist, or YouTube Music link.")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("Use a standard youtube.com, music.youtube.com, or youtu.be link.")
    if parsed.username or parsed.password or parsed.path in {"", "/"}:
        raise ValueError("Paste a link to a video or playlist, not the YouTube homepage.")
    return value

def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value or "")
    return value.strip(" .")[:100] or "youtube-download"

def ffmpeg_location() -> str | None:
    local_bin = APP_DIR / "tools" / "ffmpeg" / "bin"
    if (local_bin / "ffmpeg.exe").exists():
        return str(local_bin)
    if shutil.which("ffmpeg"):
        return None
    return None

def ffmpeg_executable() -> str | None:
    local_bin = APP_DIR / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if local_bin.exists():
        return str(local_bin)
    return shutil.which("ffmpeg")

def ffprobe_executable(ffmpeg: str) -> str | None:
    """Find ffprobe beside bundled FFmpeg or on PATH for normal installs."""
    if str(ffmpeg).lower().endswith((".exe", ".bat", ".cmd")):
        candidate = Path(ffmpeg).with_name("ffprobe.exe")
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffprobe")

def probe_codecs(source: Path, ffmpeg: str) -> tuple[str, str]:
    """Return video/audio codec names when ffprobe is available."""
    ffprobe = ffprobe_executable(ffmpeg)
    if not ffprobe:
        return "", ""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=20,
        )
        video = (result.stdout or "").strip().splitlines()[0] if result.stdout.strip() else ""
        audio_result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=20,
        )
        audio = (audio_result.stdout or "").strip().splitlines()[0] if audio_result.stdout.strip() else ""
        return video, audio
    except (OSError, subprocess.SubprocessError, IndexError):
        return "", ""


def convert_video_to_compatible_mp4(source: Path, job_id: str, preset: str = "ultrafast") -> Path:
    """Prefer a fast stream-copy/remux. Re-encode only when the source codecs need it."""
    executable = ffmpeg_executable()
    if not executable:
        raise RuntimeError("FFmpeg is missing. Restart Cutroom to repair setup, then retry.")
    target = source.with_name(source.stem + ".cutroom-converted.mp4")
    video_codec, audio_codec = probe_codecs(source, executable)
    can_copy = video_codec in {"h264", "avc1"} and audio_codec in {"aac", "mp3", "alac"}
    duration_seconds = 0.0
    ffprobe = ffprobe_executable(executable)
    if ffprobe:
        try:
            probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(source)], capture_output=True, text=True, timeout=20)
            duration_seconds = float((probe.stdout or "").strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            duration_seconds = 0.0

    if can_copy:
        command = [executable, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
                   "-progress", "pipe:1", "-stats_period", "1", "-nostats", str(target)]
        set_job(job_id, state="converting", percent=86, detail=f"Fast MP4 remux · {source.name}")
    else:
        command = [executable, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", preset,
                   "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                   "-progress", "pipe:1", "-stats_period", "1", "-nostats", str(target)]
        set_job(job_id, state="converting", percent=85, detail=f"Compatibility conversion · {source.name}")

    import tempfile
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as error_log:
        process = None
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1)
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time=") and duration_seconds > 0:
                    raw_time = line.partition("=")[2]
                    try:
                        hh, mm, ss = raw_time.split(":")
                        elapsed = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        fraction = max(0.0, min(1.0, elapsed / duration_seconds))
                        overall_pct = (86 if can_copy else 85) + fraction * (13 if can_copy else 14)
                        set_job(job_id, state="converting", percent=round(overall_pct, 1),
                                detail=f"{'Fast MP4 remux' if can_copy else 'Converting to compatible MP4'} · {source.name} · {round(fraction*100)}%")
                    except (ValueError, TypeError):
                        pass
            return_code = process.wait(timeout=60 * 60)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(); target.unlink(missing_ok=True)
            raise RuntimeError("MP4 processing took too long and was stopped. Try a shorter video.")
        except Exception:
            if process and process.poll() is None:
                process.kill(); process.wait()
            target.unlink(missing_ok=True)
            raise
        error_log.seek(0)
        error_text = error_log.read().strip()

    if return_code != 0 or not target.exists() or target.stat().st_size < 1024:
        target.unlink(missing_ok=True)
        detail = error_text.splitlines()
        raise RuntimeError("FFmpeg couldn't create a playable MP4. " + (detail[-1][:240] if detail else "Check that FFmpeg is installed."))
    final_path = source.with_suffix(".mp4")
    if final_path.exists() and final_path.resolve() != source.resolve():
        final_path = source.with_name(source.stem + "-compatible.mp4")
    target.replace(final_path)
    if source.exists() and source.resolve() != final_path.resolve():
        source.unlink(missing_ok=True)
    return final_path


def parse_time_seconds(value: str) -> float:
    """Parse seconds or HH:MM:SS / MM:SS input."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Enter both a start time and an end time.")
    try:
        if ":" not in raw:
            result = float(raw)
        else:
            parts = [float(part) for part in raw.split(":")]
            if len(parts) == 2:
                result = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                result = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                raise ValueError
        if result < 0 or result > 7 * 24 * 3600:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS for the cut times.")


def run_cut(job_id: str, source_raw: str, start_seconds: float, end_seconds: float,
            destination: str, mode: str) -> None:
    source = Path(source_raw).expanduser().resolve()
    dest_dir = Path(destination).expanduser().resolve()
    temp_path = None
    try:
        if not source.is_file() or source.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}:
            raise RuntimeError("Choose an existing video file (MP4, MKV, WebM, MOV, AVI, or M4V).")
        if end_seconds <= start_seconds:
            raise RuntimeError("End time must be later than start time.")
        executable = ffmpeg_executable()
        if not executable:
            raise RuntimeError("FFmpeg is missing. Restart Cutroom to repair setup.")
        dest_dir.mkdir(parents=True, exist_ok=True)
        duration = end_seconds - start_seconds
        temp_path = dest_dir / f"{source.stem}.cutroom-cutting.mp4"
        if mode == "accurate":
            command = [executable, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start_seconds),
                       "-i", str(source), "-t", str(duration), "-map", "0:v:0", "-map", "0:a?",
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", "-b:a", "160k",
                       "-movflags", "+faststart", "-progress", "pipe:1", "-stats_period", "1", "-nostats", str(temp_path)]
        else:
            command = [executable, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start_seconds),
                       "-i", str(source), "-t", str(duration), "-map", "0:v:0", "-map", "0:a?",
                       "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
                       "-progress", "pipe:1", "-stats_period", "1", "-nostats", str(temp_path)]
        set_job(job_id, state="converting", percent=1, detail="Cutting video…")
        import tempfile
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as error_log:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time="):
                        raw_time = line.partition("=")[2]
                        try:
                            hh, mm, ss = raw_time.split(":")
                            elapsed = int(hh) * 3600 + int(mm) * 60 + float(ss)
                            pct = max(1.0, min(99.0, elapsed / duration * 100))
                            set_job(job_id, state="converting", percent=round(pct, 1), detail=f"Cutting video… {round(pct)}%")
                        except (ValueError, TypeError):
                            pass
                return_code = proc.wait(timeout=60 * 60)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
                raise RuntimeError("Video cutting took too long and was stopped.")
            error_log.seek(0)
            error_text = error_log.read().strip()
        if return_code != 0 or not temp_path.exists() or temp_path.stat().st_size < 1024:
            raise RuntimeError("Video cut failed: " + ((error_text or "FFmpeg produced no output.").strip().splitlines()[-1][:240]))
        final_path = unique_destination(dest_dir, f"{source.stem}-cut.mp4")
        temp_path.replace(final_path)
        set_job(job_id, state="done", percent=100, detail=f"Cut complete · saved to {dest_dir}",
                files=[{"name": final_path.name, "path": final_path.name, "size": final_path.stat().st_size}],
                output_dir=str(dest_dir), download_dir=str(dest_dir))
    except Exception as exc:
        set_job(job_id, state="error", percent=0, detail=str(exc)[:500], files=[])
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def set_job(job_id: str, **updates) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def unique_destination(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for number in range(2, 10000):
        candidate = dest_dir / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many files named {name!r} already exist in the selected folder.")


def run_download(job_id: str, url: str, kind: str, fmt: str, quality: str, playlist: bool,
                 selected_indices: list[int] | None = None, destination: str | None = None,
                 conversion_speed: str = "ultrafast", resolution: str = "best") -> None:
    dest_dir = Path(destination).expanduser().resolve() if destination else current_download_root()
    dest_dir.mkdir(parents=True, exist_ok=True)
    staging_root = APP_DIR / "work"
    job_dir = staging_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(job_dir / ("%(playlist_index)03d - %(title).180B.%(ext)s" if playlist else "%(title).180B.%(ext)s"))

    def progress_hook(data):
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            done = data.get("downloaded_bytes") or 0
            percent = (done / total * 100) if total else 0
            speed = data.get("_speed_str") or ""
            eta = data.get("_eta_str") or ""
            set_job(job_id, state="downloading", percent=round(min(percent, 84 if kind == "video" and fmt == "mp4" else 99), 1), detail=f"Downloading · {percent:.1f}%" + (f" · {speed}" if speed else "") + (f" · ETA {eta}" if eta else ""))
        elif status == "finished":
            set_job(job_id, state="converting", percent=85 if kind == "video" and fmt == "mp4" else 99, detail="Download received. Preparing media…")

    def postprocessor_hook(data):
        set_job(job_id, state="converting", detail="Converting audio/video…")

    opts = {
        "outtmpl": outtmpl,
        "noplaylist": not playlist,
        "ignoreerrors": True,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 30,
        "windowsfilenames": True,
        "trim_file_name": 180,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "ffmpeg_location": ffmpeg_location(),
        "overwrites": False,
    }

    if selected_indices:
        opts["playlist_items"] = ",".join(str(i) for i in selected_indices)

    if kind == "audio":
        opts["format"] = "bestaudio/best"
        codec_map = {"mp3": "mp3", "m4a": "m4a", "wav": "wav", "flac": "flac", "opus": "opus"}
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": codec_map[fmt], "preferredquality": quality if fmt == "mp3" else "0"}]
    else:
        if fmt == "mp4":
            height_filter = "" if resolution == "best" else f"[height<={int(resolution)}]"
            opts["format"] = (
                f"bestvideo{height_filter}[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
                f"bestvideo{height_filter}[ext=mp4]+bestaudio[ext=m4a]/"
                f"best{height_filter}[ext=mp4]/bestvideo{height_filter}+bestaudio/best{height_filter}/best"
            )
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestvideo*+bestaudio/best"
            opts["merge_output_format"] = fmt
            if fmt in {"mkv", "mov", "avi", "webm"}:
                opts["recodevideo"] = fmt

    set_job(job_id, state="starting", percent=0, detail="Connecting to YouTube…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        if kind == "video" and fmt == "mp4":
            media_candidates = [
                p for p in job_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".mkv", ".webm", ".mp4", ".mov", ".avi"}
                and ".cutroom-converted." not in p.name
            ]
            if not media_candidates:
                raise RuntimeError("The download didn't produce a video file to convert.")
            converted = []
            for source in media_candidates:
                set_job(job_id, state="converting", percent=99, detail=f"Making a compatible MP4 · {source.name}")
                converted.append(convert_video_to_compatible_mp4(source, job_id, conversion_speed))
        staged_files = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".temp"}]
        if not staged_files:
            raise RuntimeError("No files were created. The video may be unavailable, or FFmpeg may be missing.")
        file_data = []
        for path in sorted(staged_files, key=lambda p: p.name.lower()):
            try:
                final_path = unique_destination(dest_dir, path.name)
                shutil.move(str(path), str(final_path))
                file_data.append({"name": final_path.name, "path": final_path.name, "size": final_path.stat().st_size})
            except OSError as exc:
                raise RuntimeError(f"Couldn't move the finished file to {dest_dir}: {exc}") from exc
        set_job(job_id, state="done", percent=100,
                detail=f"Finished · {len(file_data)} file(s) saved to {dest_dir}",
                files=file_data, output_dir=str(dest_dir), download_dir=str(dest_dir))
    except Exception as exc:
        message = str(exc).strip() or "Download failed."
        if "ffmpeg" in message.lower() or "ffprobe" in message.lower():
            message = "FFmpeg is missing. Run Cutroom again to repair setup, then retry."
        elif "sign in" in message.lower() or "bot" in message.lower():
            message = "YouTube blocked this request. Try a different public video you are allowed to save."
        set_job(job_id, state="error", percent=0, detail=message[:500])
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/heartbeat")
def client_heartbeat():
    """Heartbeat from the open browser tab; the desktop launcher exits when it stops."""
    global LAST_CLIENT_HEARTBEAT
    import time
    LAST_CLIENT_HEARTBEAT = time.monotonic()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    return jsonify(ok=True, ffmpeg=bool(ffmpeg_location() or shutil.which("ffmpeg")), download_folder=str(current_download_root()))


@app.post("/api/choose-folder")
def choose_folder():
    """Ask the desktop launcher to show a native Windows folder picker."""
    import sys
    import subprocess
    picker_result = APP_DIR / f"folder-picker-{uuid.uuid4().hex}.txt"
    current = str(current_download_root())
    try:
        env = os.environ.copy()
        env["CUTROOM_PICKER_INITIAL_DIR"] = current
        env["CUTROOM_PICKER_RESULT_FILE"] = str(picker_result)
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--pick-folder"]
        else:
            launcher_script = Path(__file__).resolve().parent / "launcher.py"
            command = [sys.executable, str(launcher_script), "--pick-folder"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=env,
                                creationflags=flags, check=False)
        if result.returncode != 0:
            return jsonify(error=(result.stderr or "Folder picker could not start.")[:300]), 500
        selected = picker_result.read_text(encoding="utf-8").strip() if picker_result.exists() else ""
        if not selected:
            return jsonify(cancelled=True, download_folder=current)
        folder = Path(selected).expanduser().resolve()
        if not folder.is_dir():
            return jsonify(error="That folder does not exist."), 400
        save_download_root(folder)
        return jsonify(ok=True, download_folder=str(folder))
    except subprocess.TimeoutExpired:
        return jsonify(error="Folder picker timed out."), 500
    except Exception as exc:
        return jsonify(error=f"Couldn't choose a folder: {exc}"[:300]), 500
    finally:
        picker_result.unlink(missing_ok=True)


@app.post("/api/settings/download-folder")
def set_download_folder():
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("path") or "").strip().strip('"')
    if not raw:
        return jsonify(error="Enter a folder path or use Browse."), 400
    try:
        folder = Path(raw).expanduser()
        if not folder.is_absolute():
            return jsonify(error="Use a full folder path, such as C:\\Users\\You\\Downloads."), 400
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve()
        save_download_root(folder)
        return jsonify(ok=True, download_folder=str(folder))
    except OSError as exc:
        return jsonify(error=f"Couldn't use that folder: {exc}"[:300]), 400



@app.post("/api/choose-video")
def choose_video():
    """Open a native file picker for the built-in video cutter."""
    import sys
    picker_result = APP_DIR / f"video-picker-{uuid.uuid4().hex}.txt"
    try:
        env = os.environ.copy()
        env["CUTROOM_PICKER_RESULT_FILE"] = str(picker_result)
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--pick-video-file"]
        else:
            command = [sys.executable, str(Path(__file__).resolve().parent / "launcher.py"), "--pick-video-file"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if result.returncode != 0:
            return jsonify(error=(result.stderr or "Video picker could not start.")[:300]), 500
        selected = picker_result.read_text(encoding="utf-8").strip() if picker_result.exists() else ""
        return jsonify(cancelled=not bool(selected), path=selected)
    except subprocess.TimeoutExpired:
        return jsonify(error="Video picker timed out."), 500
    except Exception as exc:
        return jsonify(error=f"Couldn't choose a video: {exc}"[:300]), 500
    finally:
        picker_result.unlink(missing_ok=True)




@app.post("/api/video-preview")
def create_video_preview():
    """Register a local video for browser preview and return its duration."""
    payload = request.get_json(silent=True) or {}
    raw_source = (payload.get("source") or "").strip().strip('"')
    try:
        source = Path(raw_source).expanduser()
        allowed = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
        if not source.is_absolute() or not source.is_file() or source.suffix.lower() not in allowed:
            raise ValueError("Choose an existing video file (MP4, MKV, WebM, MOV, AVI, or M4V).")
        source = source.resolve()
        executable = ffmpeg_executable()
        ffprobe = ffprobe_executable(executable) if executable else shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("FFprobe is missing. Restart Cutroom to repair setup, then retry.")
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
             "-of", "json", str(source)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError("Cutroom couldn't read this video's metadata.")
        metadata = json.loads(probe.stdout or "{}")
        duration = float((metadata.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("This video has no readable duration.")
        video_stream = next((stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"), {})
        token = uuid.uuid4().hex
        with VIDEO_PREVIEWS_LOCK:
            VIDEO_PREVIEWS[token] = source
        return jsonify(
            id=token, name=source.name, duration=duration,
            width=video_stream.get("width"), height=video_stream.get("height"),
            preview_url=f"/api/video-preview/{token}",
        )
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return jsonify(error=str(exc)[:400]), 400
    except Exception as exc:
        return jsonify(error=str(exc)[:400]), 500


@app.get("/api/video-preview/<token>")
def stream_video_preview(token: str):
    """Stream a registered local video with HTTP range support for seeking."""
    with VIDEO_PREVIEWS_LOCK:
        source = VIDEO_PREVIEWS.get(token)
    if not source or not source.is_file():
        return jsonify(error="That preview has expired. Choose the video again."), 404
    return send_file(source, as_attachment=False, conditional=True, download_name=source.name)


@app.post("/api/cut")
def start_cut():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "").strip().strip('"')
    destination = (payload.get("destination") or str(current_download_root())).strip().strip('"')
    try:
        if not source:
            raise ValueError("Choose a video file first.")
        source_path = Path(source).expanduser()
        if not source_path.is_absolute() or not source_path.is_file():
            raise ValueError("Choose an existing video file using Browse.")
        start_seconds = parse_time_seconds(payload.get("start", "0"))
        end_seconds = parse_time_seconds(payload.get("end", ""))
        dest_path = Path(destination).expanduser()
        if not dest_path.is_absolute():
            raise ValueError("Choose a full output folder path.")
        dest_path.mkdir(parents=True, exist_ok=True)
        dest_path = dest_path.resolve()
        mode = payload.get("mode", "fast")
        if mode not in {"fast", "accurate"}:
            mode = "fast"
    except (ValueError, OSError) as exc:
        return jsonify(error=str(exc)), 400
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "state": "queued", "percent": 0, "detail": "Waiting to cut…", "files": []}
    thread = threading.Thread(target=run_cut, args=(job_id, str(source_path.resolve()), start_seconds, end_seconds, str(dest_path), mode), daemon=True)
    thread.start()
    return jsonify(id=job_id), 202


@app.post("/api/inspect")
def inspect_url():
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_url(payload.get("url", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist", "ignoreerrors": True,
        "playlistend": 500, "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("YouTube didn't return any video details for that link.")
        entries = info.get("entries")
        if entries is None:
            entries = [info]
        results = []
        for position, item in enumerate(entries, start=1):
            if not item:
                continue
            title = (item.get("title") or item.get("id") or f"Video {position}").strip()
            duration = item.get("duration")
            mins = f"{int(duration)//60}:{int(duration)%60:02d}" if isinstance(duration, (int, float)) else "Duration unknown"
            results.append({
                "index": int(item.get("playlist_index") or position),
                "id": item.get("id", ""), "title": title, "duration": mins,
                "url": item.get("url") or item.get("webpage_url") or "",
                "thumbnail": item.get("thumbnail") or "",
            })
        if not results:
            raise RuntimeError("No videos were found. Check that the link is public and available.")
        return jsonify(title=info.get("title") or "YouTube results", entries=results, truncated=len(results) >= 500)
    except Exception as exc:
        message = str(exc).strip() or "Couldn't read videos from that link."
        if "sign in" in message.lower() or "bot" in message.lower():
            message = "YouTube blocked this request. Try a public video or playlist you are allowed to access."
        return jsonify(error=message[:500]), 400


@app.post("/api/download")
def start_download():
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_url(payload.get("url", ""))
        kind = payload.get("kind", "video")
        fmt = payload.get("format", "mp4")
        quality = payload.get("quality", "192")
        raw_indices = payload.get("selected_indices")
        playlist = bool(raw_indices) or bool(payload.get("playlist", True))
        selected_indices = sorted({int(i) for i in raw_indices if str(i).isdigit() and 1 <= int(i) <= 500}) if isinstance(raw_indices, list) else None
        if raw_indices is not None and not selected_indices:
            raise ValueError("Tick at least one video to download.")
        destination_raw = (payload.get("destination") or str(current_download_root())).strip().strip('"')
        destination_path = Path(destination_raw).expanduser()
        if not destination_path.is_absolute():
            raise ValueError("Choose a full save-location path.")
        destination_path.mkdir(parents=True, exist_ok=True)
        destination_path = destination_path.resolve()
        save_download_root(destination_path)
        conversion_speed = payload.get("conversion_speed", "ultrafast")
        if conversion_speed not in {"ultrafast", "veryfast", "fast"}:
            conversion_speed = "ultrafast"
        resolution = str(payload.get("resolution", "best"))
        if resolution not in {"best", "2160", "1440", "1080", "720", "480", "360", "240"}:
            resolution = "best"
        if kind not in {"audio", "video"}:
            raise ValueError("Choose audio or video.")
        allowed = AUDIO_FORMATS if kind == "audio" else VIDEO_FORMATS
        if fmt not in allowed:
            raise ValueError("Choose a supported output format.")
        if quality not in {"128", "192", "256", "320"}:
            quality = "192"
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "state": "queued", "percent": 0, "detail": "Waiting to start…", "files": []}
    thread = threading.Thread(target=run_download,
        args=(job_id, url, kind, fmt, quality, playlist, selected_indices, str(destination_path), conversion_speed, resolution),
        daemon=True)
    thread.start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="That job was not found. Restart a download."), 404
        return jsonify(job)

@app.get("/api/jobs/<job_id>/file")
def get_file(job_id: str):
    rel = request.args.get("path", "")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("state") != "done":
            return jsonify(error="This file is not ready yet."), 404
        root = Path(job.get("output_dir", "")).resolve()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return jsonify(error="File not found."), 404
    return send_from_directory(candidate.parent, candidate.name, as_attachment=True)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
