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

from flask import Flask, jsonify, render_template, request, send_from_directory
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
    # Also accept FFmpeg in PATH or the conventional local tools folder.
    return None


def ffmpeg_executable() -> str | None:
    local_bin = APP_DIR / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if local_bin.exists():
        return str(local_bin)
    return shutil.which("ffmpeg")


def convert_video_to_compatible_mp4(source: Path, job_id: str, preset: str = "ultrafast") -> Path:
    """Encode H.264/AAC and report FFmpeg's real progress to the web UI."""
    executable = ffmpeg_executable()
    if not executable:
        raise RuntimeError("FFmpeg is missing. Run install.bat again, then retry the video download.")

    target = source.with_name(source.stem + ".cutroom-converted.mp4")
    duration_seconds = 0.0
    ffprobe = Path(executable).with_name("ffprobe.exe") if str(executable).lower().endswith(".exe") else None
    if ffprobe and ffprobe.exists():
        try:
            probe = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                capture_output=True, text=True, timeout=20,
            )
            if probe.returncode == 0:
                duration_seconds = float((probe.stdout or "").strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            duration_seconds = 0.0

    command = [
        executable, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", preset,
        "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-progress", "pipe:1", "-stats_period", "1", "-nostats", str(target),
    ]
    import tempfile
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as error_log:
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=error_log, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
            )
            last_percent = -1
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time=") and duration_seconds > 0:
                    raw_time = line.partition("=")[2]
                    try:
                        hh, mm, ss = raw_time.split(":")
                        elapsed = int(hh) * 3600 + int(mm) * 60 + float(ss)
                        fraction = max(0.0, min(1.0, elapsed / duration_seconds))
                        conversion_pct = round(fraction * 100)
                        if conversion_pct != last_percent:
                            overall_pct = 85 + fraction * 14
                            set_job(job_id, state="converting", percent=round(overall_pct, 1),
                                    detail=f"Making a compatible MP4 · {source.name} · {conversion_pct}%")
                            last_percent = conversion_pct
                    except (ValueError, TypeError):
                        pass
                elif line == "progress=continue" and duration_seconds <= 0:
                    set_job(job_id, state="converting", percent=90,
                            detail=f"Making a compatible MP4 · {source.name} · encoding…")
            return_code = process.wait(timeout=60 * 60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            target.unlink(missing_ok=True)
            raise RuntimeError("MP4 conversion took too long and was stopped. Try a shorter video or a faster PC.")
        except Exception:
            if 'process' in locals() and process.poll() is None:
                process.kill()
                process.wait()
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
                 conversion_speed: str = "ultrafast") -> None:
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
        opts["format"] = "bestvideo*+bestaudio/best"
        if fmt == "mp4":
            opts["merge_output_format"] = "mkv"
        else:
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
        args=(job_id, url, kind, fmt, quality, playlist, selected_indices, str(destination_path), conversion_speed),
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
