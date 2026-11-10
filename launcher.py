from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "Cutroom"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-gpl-9.0.zip"
PORT_START = 8765
HEARTBEAT_TIMEOUT = 30.0
STARTUP_GRACE = 90.0


def run_folder_picker() -> int:
    """Small secondary process used by the browser Browse button."""
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    initial = os.environ.get("CUTROOM_PICKER_INITIAL_DIR", str(Path.home() / "Downloads"))
    try:
        selected = filedialog.askdirectory(parent=root, title="Choose Cutroom download folder", initialdir=initial, mustexist=True)
        result_file = os.environ.get("CUTROOM_PICKER_RESULT_FILE")
        if result_file:
            Path(result_file).write_text(selected or "", encoding="utf-8")
        return 0
    finally:
        root.destroy()


def run_video_picker() -> int:
    """Native picker for a source video in the cutter panel."""
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(parent=root, title="Choose a video to cut",
            filetypes=[("Video files", "*.mp4 *.mkv *.webm *.mov *.avi *.m4v"), ("All files", "*.*")])
        result_file = os.environ.get("CUTROOM_PICKER_RESULT_FILE")
        if result_file:
            Path(result_file).write_text(selected or "", encoding="utf-8")
        return 0
    finally:
        root.destroy()


def app_home() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "Cutroom"


class SetupWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Cutroom setup")
        self.root.geometry("440x150")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.label = tk.Label(self.root, text="Preparing Cutroom…", anchor="w")
        self.label.pack(fill="x", padx=18, pady=(18, 10))
        self.bar = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=18, pady=4)
        self.detail = tk.Label(self.root, text="First launch may download a large FFmpeg archive.", anchor="w", fg="#555555")
        self.detail.pack(fill="x", padx=18, pady=(8, 0))
        self.root.update()

    def update(self, title: str, detail: str = "", percent: float | None = None):
        self.label.config(text=title)
        self.detail.config(text=detail)
        if percent is not None:
            self.bar.config(mode="determinate", value=max(0, min(100, percent)))
        else:
            self.bar.config(mode="indeterminate")
            self.bar.start(12)
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def install_ffmpeg(home: Path, ui: SetupWindow) -> Path:
    target_bin = home / "tools" / "ffmpeg" / "bin"
    ffmpeg = target_bin / "ffmpeg.exe"
    ffprobe = target_bin / "ffprobe.exe"
    if ffmpeg.exists() and ffprobe.exists():
        return target_bin

    tools = home / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / "ffmpeg-download.zip"
    temp = tools / "ffmpeg-extract"
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    archive.unlink(missing_ok=True)

    ui.update("Downloading FFmpeg…", "Source: BtbN FFmpeg-Builds on GitHub", 0)
    request = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "Cutroom-Setup/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
            pct = received * 100 / total if total else None
            ui.update("Downloading FFmpeg…", f"{received / (1024**2):.0f} MiB received" + (f" of {total/(1024**2):.0f} MiB" if total else ""), pct)

    ui.update("Installing FFmpeg…", "Extracting the command-line tools. This can take a few minutes.", None)
    temp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        ffmpeg_member = next((n for n in names if n.lower().endswith("/bin/ffmpeg.exe")), None)
        if not ffmpeg_member:
            raise RuntimeError("The downloaded FFmpeg archive did not contain bin/ffmpeg.exe.")
        prefix = ffmpeg_member[: -len("ffmpeg.exe")]
        selected = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
        target_bin.mkdir(parents=True, exist_ok=True)
        for member in selected:
            name = Path(member).name
            if not name:
                continue
            with zf.open(member) as src, (target_bin / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)

    archive.unlink(missing_ok=True)
    shutil.rmtree(temp, ignore_errors=True)
    if not ffmpeg.exists() or not ffprobe.exists():
        raise RuntimeError("FFmpeg setup finished without both ffmpeg.exe and ffprobe.exe.")
    return target_bin


def load_webapp(home: Path):
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    app_file = bundle_dir / "app.py"
    if not app_file.exists():
        raise RuntimeError(f"Bundled app.py is missing: {app_file}")
    os.environ["CUTROOM_HOME"] = str(home)
    # Let yt-dlp/FFmpeg find the FFmpeg installation in the persistent app folder.
    ffmpeg_bin = home / "tools" / "ffmpeg" / "bin"
    os.environ["PATH"] = str(ffmpeg_bin) + os.pathsep + os.environ.get("PATH", "")
    spec = importlib.util.spec_from_file_location("cutroom_webapp", app_file)
    if not spec or not spec.loader:
        raise RuntimeError("Could not load the bundled Cutroom web app.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_tray_icon(home: Path, port: int, stop_event: threading.Event):
    from PIL import Image, ImageDraw
    import pystray

    image = Image.new("RGBA", (64, 64), (243, 241, 235, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=16, fill=(34, 36, 31, 255))
    draw.text((13, 20), "cr", fill=(255, 254, 250, 255), stroke_width=0)

    def open_site(icon, item):
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")

    def open_downloads(icon, item):
        settings_file = home / "settings.json"
        path = Path.home() / "Downloads"
        try:
            import json
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            candidate = Path(settings.get("download_dir", str(path))).expanduser()
            if candidate.is_absolute():
                path = candidate
        except (OSError, ValueError, TypeError):
            pass
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
        except Exception:
            pass

    def exit_app(icon, item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Cutroom", open_site),
        pystray.MenuItem("Open download folder", open_downloads, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit Cutroom", exit_app),
    )
    return pystray.Icon("Cutroom", image, "Cutroom is running", menu)


def choose_server(module):
    from werkzeug.serving import make_server
    last_error = None
    for port in range(PORT_START, PORT_START + 10):
        try:
            server = make_server("127.0.0.1", port, module.app, threaded=True)
            return server, port
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"Could not find a free local port from {PORT_START} to {PORT_START + 9}: {last_error}")


def main():
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    ui = SetupWindow()
    try:
        install_ffmpeg(home, ui)
        ui.update("Starting Cutroom…", "Loading the local website and tray icon.", None)
        module = load_webapp(home)
        server, port = choose_server(module)
    except Exception as exc:
        ui.close()
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Cutroom could not start", str(exc)[:1800])
        root.destroy()
        return 1
    ui.close()

    stop_event = threading.Event()
    server_thread = threading.Thread(target=server.serve_forever, name="CutroomWebServer", daemon=True)
    server_thread.start()
    tray = make_tray_icon(home, port, stop_event)
    tray_thread = threading.Thread(target=tray.run, name="CutroomTray", daemon=True)
    tray_thread.start()

    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}")
    started = time.monotonic()
    seen_heartbeat = False
    try:
        while not stop_event.wait(0.5):
            last = float(getattr(module, "LAST_CLIENT_HEARTBEAT", 0.0) or 0.0)
            now = time.monotonic()
            if last > 0:
                seen_heartbeat = True
                if now - last > HEARTBEAT_TIMEOUT:
                    stop_event.set()
                    break
            elif not seen_heartbeat and now - started > STARTUP_GRACE:
                stop_event.set()
                break
    finally:
        stop_event.set()
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if "--pick-folder" in sys.argv:
        raise SystemExit(run_folder_picker())
    if "--pick-video-file" in sys.argv:
        raise SystemExit(run_video_picker())
    raise SystemExit(main())
