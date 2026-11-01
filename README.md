# Cutroom YouTube Downloader

A local Windows app for inspecting YouTube video/playlist entries, selecting which items to download, and saving permitted media in common audio or video formats.

## Install and run
1. Extract this folder.
2. Run `install.bat` and let setup finish.
3. The app opens at `http://127.0.0.1:8765`.
4. Paste a public video or playlist URL, click **Find videos**, tick the entries you want, choose audio/video and output format, then click **Download selected**.

Files are saved in `Downloads\Cutroom`.

## MP4 conversion progress
MP4 output is encoded as H.264 video + AAC audio for broad Windows compatibility. The progress line reports FFmpeg's conversion progress instead of remaining frozen at 99%. Conversion speed depends on video length and computer performance. FFmpeg is installed by the setup script.

Only download content you own, have permission to download, or are otherwise legally allowed to save. The app does not bypass DRM, paid access, or private-video restrictions.
