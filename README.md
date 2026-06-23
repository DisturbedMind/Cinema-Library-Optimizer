# Cinema Library Optimizer

<p align="center">
  <img src="assets/wolf-banner.png" alt="Cinema Library Optimizer wolf banner" width="100%">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Tkinter" src="https://img.shields.io/badge/GUI-Tkinter-0f766e">
  <img alt="Radarr" src="https://img.shields.io/badge/Radarr-focused-ffc230">
  <img alt="FFmpeg" src="https://img.shields.io/badge/ffprobe-supported-007808?logo=ffmpeg&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-blue">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white">
</p>

## About

Cinema Library Optimizer is a Windows Python/Tkinter desktop app for auditing and optimizing Radarr-managed movie libraries. It scans a cinema directory tree, shows folder and file sizes, identifies real video formats with ffprobe, and helps find the movies that are eating the most storage.

The app is built around practical Radarr workflows: select one or more oversized movies, choose a smaller target quality profile, let Radarr search or grab a matching release, manually inspect available releases when Radarr rejects them, and monitor the download/import process until the replacement file lands on disk. It is designed for home cinema, homelab, and self-hosted media setups where large 4K, remux, or stale media folders need to be brought under control without blindly deleting the existing library.

Use it when you want to:

- Find the largest movies and folders in a cinema library.
- Confirm whether files are really 4K, 1080p, 720p, or another format.
- Downsize huge 4K or remux movies through Radarr without manually hunting through every movie.
- Refresh scanned movie folders after Radarr or Emby changes files on disk.
- Remove stale trailer downloader temp folders during scans.

## Tags

`radarr` `radarr-tools` `movie-library` `media-library` `cinema-library` `media-management` `movie-management` `storage-optimizer` `disk-space-analyzer` `folder-size` `file-size` `python` `tkinter` `desktop-app` `windows` `ffmpeg` `ffprobe` `video-quality` `4k` `1080p` `720p` `remux` `quality-profiles` `self-hosted` `homelab` `media-server` `emby` `plex` `arr-stack` `automation` `gui` `open-source` `mit-license`
## Features

- Enter a directory path manually or choose one with Browse.
- Scan runs in the background so the GUI stays responsive.
- Folder tree drills down to file level.
- Movie/video rows show detected format tags such as 4K, 1080p, 720p, 576p, 480p, and 360p when those tags appear in the file or folder name.
- Common aliases are also detected, including UHD, Ultra HD, 3840x2160, FullHD, 1920x1080, HD, and 1280x720.
- Folder format prefers the largest primary video file, so trailers/theme videos and stale folder names like `4K` do not override refreshed file probes.
- Rows show kind, format, extension, total size, file count, child folder count, skipped entries, and full path.
- Empty/not-applicable table cells are shown as `-`, and skipped entries show `0` when nothing was skipped.
- Search box jumps to the matching movie folder as you type without expanding the whole tree.
- Click column headers to sort.
- Right-click a row to open it, open its location, or delete it.
- Probe media formats during the scan with ffprobe when it is available.
- Right-click a media file or folder and use ffprobe to re-check the real video resolution later. This walks the actual filesystem again, so it can detect files Radarr/Emby changed after the original scan. Known video files and large non-sidecar files are probed, while subtitles, images, metadata, and partial downloads are skipped.
- Right-click a movie folder or file and choose `Refresh Selected Movie` to rescan only that movie branch. Refresh first rebuilds the file list from disk, then runs ffprobe against the freshly discovered files.
- Connect to Radarr, choose a quality profile from the right-click action, see exactly which qualities the profile allows, grab matching release results, or manually choose a specific release for oversized movies without deleting the current files.
- Use `Open Log` to inspect scan, ffprobe, and Radarr diagnostics in `cinema_library_optimizer.log`.
- Press Delete on a selected row to delete it.
- Cancel long scans.
- Export the report to CSV.

## Requirements

- Windows with Python 3.11 or newer.
- Tkinter, which ships with most standard Windows Python installs.
- Optional: FFmpeg/ffprobe on PATH, or `ffprobe.exe` beside the script, for accurate video resolution detection.
- Optional: Radarr URL and API key for profile/search/download workflows.

There are no required third-party Python packages.

## Run

```powershell
python .\cinema_library_optimizer.py
```

Or use the helper:

```powershell
.\run_cinema_library_optimizer.ps1
```

On first launch, click `Radarr`, enter your Radarr URL and API key, use `Test`, then save. The app stores local settings in `settings.json`; that file is ignored for publishing so API keys are not committed.

## ffprobe Support

Filename detection is fast, but it only works when the file itself or a folder row includes a tag like `4K` or `1080p`.

For accurate detection from the actual video stream:

1. Put `ffprobe.exe` in this folder, or install FFmpeg and add it to your PATH.
2. Scan a folder.
3. Leave `Probe formats during scan` enabled to fill the Format column automatically.
4. Right-click a media file and choose `Probe Format with ffprobe` to re-check one item.
5. Or right-click a folder and choose `Probe Missing Formats Here`.

If a huge folder scan becomes too slow, uncheck `Probe formats during scan` before starting the scan.

## Radarr Profile/Search Workflow

Use this when you find a huge 4K movie and want Radarr to look for a smaller release:

1. Click `Radarr`.
2. Enter the Radarr URL, for example `http://localhost:7878`.
3. Enter the Radarr API key from Radarr Settings > General.
4. Save.
5. Scan your movie folder.
6. Select one or more movie files/folders. Use Ctrl-click or Shift-click for multiple selections.
7. Right-click one of the selected rows.
8. Choose `Radarr: Change Profile and Search`.
9. Pick the smaller target profile. The picker shows each profile's maximum allowed resolution and allowed qualities.

After confirmation, the app asks Radarr to update the movie to the selected profile, checks Radarr's current release results, skips releases above the selected profile maximum or rejected by Radarr, and grabs the best remaining release. If Radarr only returns rejected releases, the app queues Radarr's normal automatic search instead of failing the whole batch and reports the available formats as a bullet list. Use `Radarr: Choose Release...` to inspect those releases in a table and pick a specific release; if Radarr marks that release as rejected, the app asks before trying a force grab. The app does not delete the current movie file.

The app then keeps monitoring those Radarr movies while the GUI is open. It reports live status from Radarr, including release checks, grabbed releases, downloading progress, waiting/import pending, imported, failed, and no-results outcomes. If Radarr reports a failed download, the app retries the movie up to two more times before giving up. When Radarr reports that a new movie file has been downloaded/imported, the app shows a completion notification for that movie and automatically refreshes the matching scanned movie folder. Monitoring runs in the background, so after Radarr grabs are requested you can select more movies and start another replacement batch.

If Radarr grabs or imports a release above the selected profile's maximum resolution, the app flags it in the status/notification. The Radarr profile itself controls what Radarr is allowed to grab.

Radarr imports are detected from Radarr's movie file id/import metadata, not from the file's modified date on disk. Some releases keep old filesystem timestamps after import.

If a replacement does not complete while monitoring is active, select the movie again and choose `Radarr: Retry/Search Movie`. Retry/search also asks for the target quality profile, applies it to the selected movie, checks Radarr's release results again, grabs an acceptable release or queues Radarr's automatic search, and monitors the result against that profile's maximum resolution. If Radarr grabbed a bad release, blocklist/remove that release in Radarr first so the same bad release is not grabbed again.

The app first matches the selected path against Radarr's movie path, then falls back to matching the visible movie folder name and year, for example `Avatar The Way of Water (2022)`. If Radarr runs in Docker with completely different paths, the error message includes sample Radarr paths so path mapping can be added cleanly.

## Notes

- Symbolic links are not followed to avoid loops.
- Stale `.trailer-downloader-tmp` and `.trailer-download-tmp` folders are deleted automatically when encountered during scans or selected movie refreshes.
- Permission errors are counted in the Skipped column.
- Very large drives can take time because every file must be measured.
- Delete permanently removes the selected file or folder after confirmation.
## Project Layout

```text
Cinema-Library-Optimizer/
├─ assets/
│  └─ wolf-banner.png
├─ cinema_library_optimizer.py
├─ README.md
├─ requirements.txt
├─ settings.example.json
├─ run_cinema_library_optimizer.ps1
└─ build_exe.ps1
```

## Publishing Notes

Before publishing, keep these files out of the repository:

- `settings.json`, because it contains the local Radarr URL/API key.
- `*.log`, because logs can contain local paths, server addresses, and movie names.
- `__pycache__/`, `build/`, and `dist/`, because they are generated artifacts.

The included `.gitignore` already excludes those files. Use `settings.example.json` as the safe template for new users.

To build a Windows executable after installing PyInstaller:

```powershell
python -m pip install pyinstaller
.\build_exe.ps1
```

Use `-OneFile` if you prefer a single-file executable:

```powershell
.\build_exe.ps1 -OneFile
```

## License

MIT. See [LICENSE](LICENSE).

