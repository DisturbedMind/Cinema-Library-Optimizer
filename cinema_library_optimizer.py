from __future__ import annotations

import csv
import http.client
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BOTH, END, EW, LEFT, RIGHT, X, filedialog, messagebox, ttk
import tkinter as tk


APP_TITLE = "Cinema Library Optimizer"
APP_BUILD = "2026-06-23 publish-ready"
CONFIG_FILE = Path(__file__).resolve().with_name("settings.json")
LOG_FILE = Path(__file__).resolve().with_name("cinema_library_optimizer.log")
RESOURCE_DIR = Path(__file__).resolve().parent
LOGO_PATH = RESOURCE_DIR / "assets" / "wolf-banner.png"
STALE_TRAILER_TMP_DIRS = {".trailer-downloader-tmp", ".trailer-download-tmp"}
MEDIA_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".divx",
    ".f4v",
    ".flv",
    ".hevc",
    ".iso",
    ".m1v",
    ".m2v",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpe",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
    ".wtv",
}
NON_VIDEO_EXTENSIONS = {
    ".ass",
    ".db",
    ".gif",
    ".idx",
    ".jpeg",
    ".jpg",
    ".json",
    ".log",
    ".nfo",
    ".part",
    ".png",
    ".srt",
    ".ssa",
    ".sub",
    ".txt",
    ".url",
    ".webp",
    ".xml",
}
QUALITY_PATTERNS = (
    ("4K", re.compile(r"(?i)(?<![a-z0-9])(4k|2160p|uhd|ultra[\W_]*hd|3840[\W_]*x[\W_]*2160)(?![a-z0-9])")),
    ("1080p", re.compile(r"(?i)(?<![a-z0-9])(1080p|fhd|full[\W_]*hd|1920[\W_]*x[\W_]*1080)(?![a-z0-9])")),
    ("720p", re.compile(r"(?i)(?<![a-z0-9])(720p|1280[\W_]*x[\W_]*720|hd)(?![a-z0-9])")),
    ("576p", re.compile(r"(?i)(?<![a-z0-9])576p(?![a-z0-9])")),
    ("480p", re.compile(r"(?i)(?<![a-z0-9])480p(?![a-z0-9])")),
    ("360p", re.compile(r"(?i)(?<![a-z0-9])360p(?![a-z0-9])")),
)
TRAILER_STEM_PATTERN = re.compile(r"(?i)(^|[\W_])(trailer|sample|featurette|teaser|interview|deleted[\W_]*scene)s?($|[\W_])")
AUXILIARY_MEDIA_FOLDERS = {
    "behind the scenes",
    "deleted scenes",
    "extras",
    "featurettes",
    "interviews",
    "samples",
    "theme-music",
    "trailers",
}
FFPROBE_TIMEOUT_SECONDS = 20
RADARR_TIMEOUT_SECONDS = 30
RADARR_DOWNLOAD_POLL_SECONDS = 60
RADARR_DOWNLOAD_MONITOR_SECONDS = 7 * 24 * 60 * 60
RADARR_DOWNLOAD_RETRY_LIMIT = 2


def log_event(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def log_exception(context: str, exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_event(f"{context}\n{detail.rstrip()}")


def log_thread_exception(args: threading.ExceptHookArgs) -> None:
    thread_name = args.thread.name if args.thread else "unknown"
    detail = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    log_event(f"Uncaught exception in thread {thread_name}\n{detail.rstrip()}")


@dataclass
class RadarrConfig:
    base_url: str = "http://localhost:7878"
    api_key: str = ""
    target_quality_profile_id: int | None = None
    target_quality_profile_name: str = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip())


@dataclass
class FileSystemItem:
    path: Path
    item_type: str
    size: int = 0
    file_count: int = 0
    folder_count: int = 0
    skipped_count: int = 0
    quality: str = ""
    extension: str = ""
    parent: "FileSystemItem | None" = field(default=None, repr=False)
    children: list["FileSystemItem"] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    @property
    def kind(self) -> str:
        return "Folder" if self.item_type == "folder" else "File"


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def display_extension(item: "FileSystemItem") -> str:
    return item.extension or "-"


def display_count(value: int, applicable: bool = True) -> str:
    if not applicable:
        return "-"
    return f"{value:,}"


def infer_quality(text: str) -> str:
    for label, pattern in QUALITY_PATTERNS:
        if pattern.search(text):
            return label
    return ""


def infer_quality_from_name(path: Path) -> str:
    return infer_quality(path.name)


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def is_probe_candidate(path: Path, size: int = 0) -> bool:
    suffix = path.suffix.lower()
    if suffix in NON_VIDEO_EXTENSIONS:
        return False
    if suffix in MEDIA_EXTENSIONS:
        return True
    return size >= 50 * 1024 * 1024


def is_auxiliary_media_path(path: Path) -> bool:
    if TRAILER_STEM_PATTERN.search(path.stem):
        return True
    return any(part.lower() in AUXILIARY_MEDIA_FOLDERS for part in path.parts[:-1])


def find_ffprobe() -> str | None:
    local_names = ("ffprobe.exe", "ffprobe")
    script_dir = Path(__file__).resolve().parent
    for name in local_names:
        local_path = script_dir / name
        if local_path.exists():
            return str(local_path)
    return shutil.which("ffprobe")


def quality_from_resolution(width: int, height: int) -> str:
    longest = max(width, height)
    if longest >= 7000:
        return "8K"
    if longest >= 3800:
        return "4K"
    if longest >= 1900:
        return "1080p"
    if longest >= 1200:
        return "720p"
    if longest >= 700 and min(width, height) >= 540:
        return "576p"
    if longest >= 700:
        return "480p"
    if longest >= 480:
        return "360p"
    return f"{width}x{height}" if width and height else ""


def probe_media_quality(path: Path, ffprobe_path: str) -> tuple[str, str]:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe could not read this file."
        return "", detail

    try:
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        if not streams:
            return "", "No video stream found."
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", "ffprobe returned unreadable stream data."

    quality = quality_from_resolution(width, height)
    return quality, f"{width}x{height}" if width and height else ""


def load_radarr_config() -> RadarrConfig:
    if not CONFIG_FILE.exists():
        return RadarrConfig()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RadarrConfig()
    profile_id = data.get("target_quality_profile_id")
    return RadarrConfig(
        base_url=str(data.get("base_url") or "http://localhost:7878"),
        api_key=str(data.get("api_key") or ""),
        target_quality_profile_id=int(profile_id) if profile_id else None,
        target_quality_profile_name=str(data.get("target_quality_profile_name") or ""),
    )


def save_radarr_config(config: RadarrConfig) -> None:
    data = {
        "base_url": config.base_url.strip().rstrip("/"),
        "api_key": config.api_key.strip(),
        "target_quality_profile_id": config.target_quality_profile_id,
        "target_quality_profile_name": config.target_quality_profile_name,
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def normalize_media_path(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/").lower()
    return re.sub(r"/+", "/", normalized)


def normalize_movie_label(value: object) -> str:
    label = str(value or "").lower()
    label = re.sub(r"\(\d{4}\)", " ", label)
    label = re.sub(r"\b(19|20)\d{2}\b", " ", label)
    label = re.sub(r"[^a-z0-9]+", " ", label)
    return " ".join(label.split())


def movie_year_from_label(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def path_name_candidates(path: Path) -> list[str]:
    candidates: list[str] = []
    parts = [part for part in path.parts if part and part not in {path.anchor, "\\", "/"}]
    if is_media_file(path):
        parts = parts[:-1]
    for part in reversed(parts):
        if part and part not in candidates:
            candidates.append(part)
    return candidates


def movie_file_id(movie: dict) -> int | None:
    movie_file = movie.get("movieFile") or {}
    value = movie_file.get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def movie_file_path(movie: dict) -> str:
    movie_file = movie.get("movieFile") or {}
    path = movie_file.get("path")
    if path:
        return str(path)
    movie_path = movie.get("path")
    relative_path = movie_file.get("relativePath")
    if movie_path and relative_path:
        return str(Path(str(movie_path)) / str(relative_path))
    return ""


def movie_file_date_added(movie: dict) -> str:
    movie_file = movie.get("movieFile") or {}
    return str(movie_file.get("dateAdded") or "")


def release_title(release: dict) -> str:
    return str(release.get("title") or release.get("releaseTitle") or "Unknown release")


def release_quality_name(release: dict) -> str:
    quality = release.get("quality") or {}
    if not isinstance(quality, dict):
        return ""
    quality_detail = quality.get("quality") or {}
    if not isinstance(quality_detail, dict):
        return ""
    return str(quality_detail.get("name") or "")


def release_quality_resolution(release: dict) -> int:
    quality = release.get("quality") or {}
    if not isinstance(quality, dict):
        return 0
    quality_detail = quality.get("quality") or {}
    if not isinstance(quality_detail, dict):
        return 0
    try:
        return int(quality_detail.get("resolution") or 0)
    except (TypeError, ValueError):
        return 0


def release_size(release: dict) -> int:
    try:
        return int(float(release.get("size") or release.get("sizeBytes") or 0))
    except (TypeError, ValueError):
        return 0


def release_rejections(release: dict) -> list[str]:
    reasons: list[str] = []
    for rejection in release.get("rejections") or []:
        if isinstance(rejection, dict):
            reason = rejection.get("reason") or rejection.get("message") or rejection.get("type")
            reasons.append(str(reason or rejection))
        else:
            reasons.append(str(rejection))
    if release.get("downloadAllowed") is False and not reasons:
        reasons.append("Download not allowed by Radarr")
    if release.get("rejected") and not reasons:
        reasons.append("Rejected by Radarr")
    return reasons


def summarize_release_skips(skipped: list[str]) -> str:
    if not skipped:
        return "No releases matched the selected rules."
    counts: dict[str, int] = {}
    for reason in skipped:
        counts[reason] = counts.get(reason, 0) + 1
    summary = []
    for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]:
        suffix = f" ({count} releases)" if count > 1 else ""
        summary.append(f"{reason}{suffix}")
    return "; ".join(summary)


def format_size_range(sizes: list[int]) -> str:
    values = sorted(size for size in sizes if size > 0)
    if not values:
        return "unknown size"
    if values[0] == values[-1]:
        return format_size(values[0])
    return f"{format_size(values[0])} to {format_size(values[-1])}"


def available_release_format_list(releases: list[dict], max_resolution: int) -> str:
    groups: dict[str, dict[str, object]] = {}
    for release in releases:
        quality_name = release_quality_name(release) or "unknown quality"
        resolution = release_quality_resolution(release)
        if resolution and f"{resolution}" not in quality_name:
            label = f"{quality_name} ({resolution}p)"
        else:
            label = quality_name
        group = groups.setdefault(label, {"count": 0, "sizes": [], "reasons": {}})
        group["count"] = int(group["count"]) + 1
        sizes = group["sizes"]
        if isinstance(sizes, list):
            sizes.append(release_size(release))
        reasons = group["reasons"]
        if not isinstance(reasons, dict):
            continue
        release_reasons = release_rejections(release)
        if max_resolution and resolution and resolution > max_resolution:
            release_reasons.append(f"above selected max {max_resolution}p")
        if not release_reasons:
            release_reasons.append("acceptable")
        for reason in release_reasons:
            reasons[reason] = int(reasons.get(reason, 0)) + 1

    if not groups:
        return "- No release formats returned by Radarr"

    lines: list[str] = []
    for label, group in sorted(groups.items(), key=lambda item: item[0].lower()):
        count = int(group["count"])
        sizes = group["sizes"] if isinstance(group["sizes"], list) else []
        reasons = group["reasons"] if isinstance(group["reasons"], dict) else {}
        reason_parts = []
        for reason, reason_count in sorted(reasons.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            suffix = f" x{reason_count}" if int(reason_count) > 1 else ""
            reason_parts.append(f"{reason}{suffix}")
        reason_text = "; ".join(reason_parts[:4]) if reason_parts else "unknown status"
        lines.append(f"- {label}: {count} release(s), {format_size_range(sizes)}, {reason_text}")
    return "\n".join(lines)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def radarr_datetime_value(value: object) -> float:
    text = str(value or "")
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def profile_allowed_qualities(profile: dict) -> list[tuple[str, int]]:
    qualities: list[tuple[str, int]] = []
    for item in profile.get("items") or []:
        if not item.get("allowed"):
            continue
        quality = item.get("quality") or {}
        name = str(quality.get("name") or "")
        if not name or name.lower() == "none":
            continue
        try:
            resolution = int(quality.get("resolution") or 0)
        except (TypeError, ValueError):
            resolution = 0
        qualities.append((name, resolution))
    return qualities


def profile_max_resolution(profile: dict) -> int:
    return max((resolution for _name, resolution in profile_allowed_qualities(profile)), default=0)


def profile_allowed_summary(profile: dict) -> str:
    qualities = profile_allowed_qualities(profile)
    names = [name for name, _resolution in qualities]
    max_resolution = profile_max_resolution(profile)
    max_text = f"max {max_resolution}p" if max_resolution else "unknown max"
    return f"{max_text}; allows {', '.join(names) if names else 'no named qualities'}"


def profile_display_name(profile: dict) -> str:
    return f"{profile.get('name') or 'Unnamed'} ({profile_allowed_summary(profile)})"


class RadarrClient:
    def __init__(self, config: RadarrConfig) -> None:
        self.config = config
        self.base_url = config.base_url.strip().rstrip("/")
        self.api_key = config.api_key.strip()

    def request(self, method: str, endpoint: str, body: object | None = None) -> object:
        data = None
        headers = {
            "Accept": "application/json",
            "X-Api-Key": self.api_key,
            "User-Agent": "Cinema-Library-Optimizer",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=data,
            headers=headers,
            method=method,
        )
        log_event(f"Radarr request: {method} {self.base_url}{endpoint}")
        try:
            with urllib.request.urlopen(request, timeout=RADARR_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            log_event(f"Radarr HTTP error: {method} {endpoint}: HTTP {exc.code}: {detail or exc.reason}")
            raise RuntimeError(f"Radarr returned HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            log_event(f"Radarr URL error: {method} {endpoint}: {exc.reason}")
            raise RuntimeError(f"Could not connect to Radarr: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            log_event(f"Radarr timeout: {method} {endpoint}: {exc}")
            raise RuntimeError(
                f"Radarr did not respond within {RADARR_TIMEOUT_SECONDS} seconds. "
                "Check that Radarr is running and not busy, then try again."
            ) from exc
        except (ConnectionError, http.client.HTTPException, OSError) as exc:
            log_event(f"Radarr connection error: {method} {endpoint}: {exc}")
            raise RuntimeError(f"Connection to Radarr failed: {exc}") from exc

        if not raw:
            log_event(f"Radarr response: {method} {endpoint}: empty")
            return None
        log_event(f"Radarr response: {method} {endpoint}: {len(raw):,} bytes")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            log_event(f"Radarr JSON error: {method} {endpoint}: {exc}")
            raise RuntimeError(f"Radarr returned unreadable JSON from {endpoint}: {exc}") from exc

    def get_quality_profiles(self) -> list[dict]:
        result = self.request("GET", "/api/v3/qualityprofile")
        return list(result) if isinstance(result, list) else []

    def get_movies(self) -> list[dict]:
        result = self.request("GET", "/api/v3/movie")
        return list(result) if isinstance(result, list) else []

    def get_movie(self, movie_id: int) -> dict:
        result = self.request("GET", f"/api/v3/movie/{movie_id}")
        return dict(result) if isinstance(result, dict) else {}

    def update_movie_quality_profile(self, movie: dict, quality_profile_id: int) -> dict:
        updated = dict(movie)
        updated["qualityProfileId"] = quality_profile_id
        result = self.request("PUT", f"/api/v3/movie/{movie['id']}", updated)
        return dict(result) if isinstance(result, dict) else updated

    def search_movie(self, movie_id: int) -> dict:
        result = self.request("POST", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]})
        return dict(result) if isinstance(result, dict) else {}

    def get_releases(self, movie_id: int) -> list[dict]:
        query = urllib.parse.urlencode({"movieId": movie_id})
        result = self.request("GET", f"/api/v3/release?{query}")
        return [dict(item) for item in result] if isinstance(result, list) else []

    def grab_release(self, release: dict, force: bool = False) -> dict:
        body = dict(release)
        if force:
            body["force"] = True
        result = self.request("POST", "/api/v3/release", body)
        return dict(result) if isinstance(result, dict) else {}

    def get_command(self, command_id: int) -> dict:
        result = self.request("GET", f"/api/v3/command/{command_id}")
        return dict(result) if isinstance(result, dict) else {}

    def get_queue_items(self, movie_id: int) -> list[dict]:
        query = urllib.parse.urlencode({"page": 1, "pageSize": 250, "sortKey": "timeleft", "sortDirection": "ascending"})
        result = self.request("GET", f"/api/v3/queue?{query}")
        records = result.get("records") if isinstance(result, dict) else result
        if not isinstance(records, list):
            return []
        return [dict(item) for item in records if int(item.get("movieId") or 0) == movie_id]

    def get_movie_history(self, movie_id: int, page_size: int = 50) -> list[dict]:
        query = urllib.parse.urlencode(
            {"page": 1, "pageSize": page_size, "sortKey": "date", "sortDirection": "descending", "movieId": movie_id}
        )
        result = self.request("GET", f"/api/v3/history?{query}")
        records = result.get("records") if isinstance(result, dict) else result
        if not isinstance(records, list):
            return []
        return [dict(item) for item in records if int(item.get("movieId") or 0) == movie_id]

    def find_movie_for_path(self, selected_path: Path) -> dict | None:
        selected = normalize_media_path(selected_path)
        selected_names = path_name_candidates(selected_path)
        best_match: dict | None = None
        best_score = -1
        movies = self.get_movies()
        log_event(f"Radarr movie match: server={self.base_url}; selected={selected_path}; movies={len(movies)}")

        for movie in movies:
            candidates = self._movie_path_candidates(movie)
            for candidate in candidates:
                if not candidate:
                    continue
                if selected == candidate or selected.startswith(candidate + "/") or candidate.startswith(selected + "/"):
                    score = len(candidate)
                    if score > best_score:
                        best_match = movie
                        best_score = score
        if best_match:
            log_event(f"Radarr movie match by path: selected={selected_path}; title={best_match.get('title')}; path={best_match.get('path')}")
            return best_match

        match = self._find_movie_by_folder_name(selected_names, movies)
        if match:
            log_event(f"Radarr movie match by folder name: selected={selected_path}; title={match.get('title')}; path={match.get('path')}")
        else:
            log_event(f"Radarr movie no match: selected={selected_path}; names={selected_names[:5]}")
        return match

    def sample_movie_paths(self, limit: int = 5) -> list[str]:
        samples: list[str] = []
        for movie in self.get_movies()[:limit]:
            movie_path = movie.get("path")
            title = movie.get("title")
            if movie_path:
                samples.append(str(movie_path))
            elif title:
                samples.append(str(title))
        return samples

    def _find_movie_by_folder_name(self, selected_names: list[str], movies: list[dict]) -> dict | None:
        selected_labels = [(normalize_movie_label(name), movie_year_from_label(name)) for name in selected_names]
        for selected_label, selected_year in selected_labels:
            if not selected_label:
                continue
            for movie in movies:
                title_label = normalize_movie_label(movie.get("title"))
                path_label = normalize_movie_label(Path(str(movie.get("path") or "")).name)
                movie_year = movie.get("year")
                year_matches = not selected_year or not movie_year or int(movie_year) == selected_year
                if year_matches and selected_label in {title_label, path_label}:
                    return movie

        for selected_label, _selected_year in selected_labels:
            if not selected_label:
                continue
            for movie in movies:
                path_label = normalize_movie_label(Path(str(movie.get("path") or "")).name)
                if selected_label == path_label:
                    return movie
        return None

    def _movie_path_candidates(self, movie: dict) -> list[str]:
        candidates = [normalize_media_path(movie.get("path"))]
        movie_file = movie.get("movieFile") or {}
        file_path = movie_file.get("path")
        if file_path:
            candidates.append(normalize_media_path(file_path))
        relative_path = movie_file.get("relativePath")
        movie_path = movie.get("path")
        if relative_path and movie_path:
            candidates.append(normalize_media_path(f"{movie_path}/{relative_path}"))
        return candidates


def scan_folder(
    path: Path,
    cancel_event: threading.Event,
    progress_queue: queue.Queue,
    ffprobe_path: str | None = None,
) -> FileSystemItem:
    item = FileSystemItem(path=path, item_type="folder")
    progress_queue.put(("progress", str(path)))

    if cancel_event.is_set():
        return item

    try:
        entries = list(os.scandir(path))
    except (OSError, PermissionError):
        item.skipped_count += 1
        return item

    children: list[FileSystemItem] = []

    for entry in entries:
        if cancel_event.is_set():
            break

        try:
            entry_path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if entry.name.lower() in STALE_TRAILER_TMP_DIRS:
                    progress_queue.put(("progress", f"Removing stale temp folder {entry_path}"))
                    try:
                        shutil.rmtree(entry_path)
                        log_event(f"Removed stale trailer downloader temp folder: {entry_path}")
                    except (OSError, PermissionError) as exc:
                        item.skipped_count += 1
                        log_event(f"Could not remove stale trailer downloader temp folder: {entry_path}: {exc}")
                    continue
                child = scan_folder(entry_path, cancel_event, progress_queue, ffprobe_path)
                child.parent = item
                children.append(child)
                item.size += child.size
                item.file_count += child.file_count
                item.folder_count += child.folder_count + 1
                item.skipped_count += child.skipped_count
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                quality = infer_quality_from_name(entry_path)
                if ffprobe_path and is_probe_candidate(entry_path, stat.st_size):
                    progress_queue.put(("progress", f"Probing {entry_path}"))
                    probed_quality, _detail = probe_media_quality(entry_path, ffprobe_path)
                    quality = probed_quality or quality
                child = FileSystemItem(
                    path=entry_path,
                    item_type="file",
                    size=stat.st_size,
                    file_count=1,
                    quality=quality,
                    extension=entry_path.suffix.lower(),
                    parent=item,
                )
                children.append(child)
                item.size += child.size
                item.file_count += 1
        except (OSError, PermissionError):
            item.skipped_count += 1

    item.children = sorted(children, key=lambda child: (child.item_type != "folder", -child.size, child.name.lower()))
    item.quality = folder_quality(item)
    return item


def folder_quality(item: FileSystemItem) -> str:
    return primary_child_quality(item.children) or infer_quality_from_name(item.path)


def primary_child_quality(children: list[FileSystemItem]) -> str:
    primary: list[FileSystemItem] = []
    auxiliary: list[FileSystemItem] = []
    for child in children:
        for media_item in qualified_media_items(child):
            if is_auxiliary_media_path(media_item.path):
                auxiliary.append(media_item)
            else:
                primary.append(media_item)
    if primary:
        return max(primary, key=lambda item: item.size).quality
    if auxiliary:
        return max(auxiliary, key=lambda item: item.size).quality
    return ""


def qualified_media_items(item: FileSystemItem) -> list[FileSystemItem]:
    if item.item_type == "file":
        return [item] if item.quality and is_media_file(item.path) else []
    rows: list[FileSystemItem] = []
    for child in item.children:
        rows.extend(qualified_media_items(child))
    return rows


def probe_report_files(report: FileSystemItem, ffprobe_path: str, progress_queue: queue.Queue) -> tuple[int, int]:
    targets = [
        item
        for item in flatten_report(report)
        if item.item_type == "file" and item.path.exists() and is_probe_candidate(item.path, item.size)
    ]
    log_event(f"Probing refreshed report: {report.path}; targets={len(targets)}")
    changed = 0
    errors = 0
    for index, item in enumerate(targets, start=1):
        progress_queue.put(("progress", f"Probing {index:,}/{len(targets):,}: {item.path}"))
        detail = ""
        try:
            quality, detail = probe_media_quality(item.path, ffprobe_path)
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
            quality = ""
        if quality:
            item.quality = quality
            changed += 1
            log_event(f"ffprobe refreshed file: {item.path}: {quality}")
        else:
            errors += 1
            log_event(f"ffprobe could not read refreshed file: {item.path}: {detail or 'no video stream detected'}")
    refresh_folder_qualities(report)
    return changed, errors


def refresh_folder_qualities(item: FileSystemItem) -> None:
    if item.item_type != "folder":
        return
    for child in item.children:
        refresh_folder_qualities(child)
    item.quality = folder_quality(item)


def flatten_report(item: FileSystemItem) -> list[FileSystemItem]:
    rows = [item]
    for child in item.children:
        rows.extend(flatten_report(child))
    return rows


class DriveSpaceAnalyzer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - {APP_BUILD}")
        self.geometry("1240x760")
        self.minsize(980, 600)
        self.configure(bg="#0f172a")
        self._configure_style()

        self.progress_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.scan_thread: threading.Thread | None = None
        self.probe_thread: threading.Thread | None = None
        self.refresh_thread: threading.Thread | None = None
        self.radarr_thread: threading.Thread | None = None
        self.current_report: FileSystemItem | None = None
        self.current_rows: list[FileSystemItem] = []
        self.tree_items: dict[str, FileSystemItem] = {}
        self.item_tree_ids: dict[int, str] = {}
        self.sort_column = "size"
        self.sort_reverse = True
        self.scan_started_at = 0.0
        self.ffprobe_path = find_ffprobe()
        self.radarr_config = load_radarr_config()
        self.search_var = tk.StringVar()
        self.logo_image: tk.PhotoImage | None = None
        self.header_logo: tk.PhotoImage | None = None

        log_event(f"Started {APP_TITLE} build {APP_BUILD} from {Path(__file__).resolve()}")
        log_event(f"ffprobe path: {self.ffprobe_path or 'not found'}")
        log_event(f"Radarr base URL: {self.radarr_config.base_url or 'not configured'}")

        self._build_ui()
        self.after(100, self._poll_queue)

    def _load_logo_images(self) -> None:
        if not LOGO_PATH.exists():
            return
        try:
            self.logo_image = tk.PhotoImage(file=str(LOGO_PATH))
            self.iconphoto(True, self.logo_image)
            scale = max(self.logo_image.width() // 220, self.logo_image.height() // 96, 1)
            self.header_logo = self.logo_image.subsample(scale, scale)
        except tk.TclError as exc:
            log_event(f"Could not load logo image {LOGO_PATH}: {exc}")
            self.logo_image = None
            self.header_logo = None
    def _configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f8fafc")
        style.configure("App.TFrame", background="#f8fafc")
        style.configure("Hero.TFrame", background="#0f172a")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#cbd5e1", relief="solid")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#0f172a", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#f8fafc", foreground="#1e293b", font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background="#f8fafc", foreground="#64748b")
        style.configure("HeroTitle.TLabel", background="#0f172a", foreground="#f8fafc", font=("Segoe UI", 22, "bold"))
        style.configure("HeroSub.TLabel", background="#0f172a", foreground="#99f6e4", font=("Segoe UI", 10))
        style.configure("TButton", background="#e5e7eb", foreground="#111827", padding=(10, 6), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#d1d5db")])
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#111827")
        style.configure("TCheckbutton", background="#f8fafc", foreground="#1e293b", font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9), fieldbackground="#ffffff", background="#ffffff", foreground="#111827")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#e2e8f0", foreground="#0f172a")
        style.map("Treeview", background=[("selected", "#0f766e")], foreground=[("selected", "#ffffff")])
    def _build_ui(self) -> None:
        shell = tk.Frame(self, bg="#0f172a")
        shell.pack(fill=BOTH, expand=True)
        self._load_logo_images()

        hero = ttk.Frame(shell, style="Hero.TFrame", padding=(22, 18, 22, 16))
        hero.pack(fill=X)
        hero.columnconfigure(1, weight=1)
        if self.header_logo is not None:
            tk.Label(hero, image=self.header_logo, bg="#0f172a", bd=0).grid(
                row=0, column=0, rowspan=2, sticky=tk.W, padx=(0, 16)
            )
        ttk.Label(hero, text=APP_TITLE, style="HeroTitle.TLabel").grid(row=0, column=1, sticky=tk.W)
        ttk.Label(
            hero,
            text="Scan, optimize, and refresh your movie library",
            style="HeroSub.TLabel",
        ).grid(row=1, column=1, sticky=tk.W, pady=(4, 0))

        root = ttk.Frame(shell, style="App.TFrame", padding=16)
        root.pack(fill=BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        path_bar = ttk.LabelFrame(root, text="Scan Controls", style="Card.TLabelframe", padding=12)
        path_bar.grid(row=0, column=0, sticky=EW)
        path_bar.columnconfigure(1, weight=1)

        ttk.Label(path_bar, text="Directory").grid(row=0, column=0, padx=(0, 8))
        self.path_var = tk.StringVar(value=str(Path.home()))
        self.path_entry = ttk.Entry(path_bar, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky=EW, padx=(0, 8))
        self.path_entry.bind("<Return>", lambda _event: self.start_scan())

        self.browse_button = ttk.Button(path_bar, text="Browse", command=self.browse_directory)
        self.browse_button.grid(row=0, column=2, padx=(0, 8))
        self.scan_button = ttk.Button(path_bar, text="Scan", command=self.start_scan)
        self.scan_button.grid(row=0, column=3, padx=(0, 8))
        self.cancel_button = ttk.Button(path_bar, text="Cancel", command=self.cancel_scan, state=tk.DISABLED)
        self.cancel_button.grid(row=0, column=4)
        self.radarr_button = ttk.Button(path_bar, text="Radarr", command=self.open_radarr_settings)
        self.radarr_button.grid(row=0, column=5, padx=(8, 0))

        self.probe_during_scan_var = tk.BooleanVar(value=bool(self.ffprobe_path))
        self.probe_during_scan_check = ttk.Checkbutton(
            path_bar,
            text="Probe formats during scan",
            variable=self.probe_during_scan_var,
        )
        self.probe_during_scan_check.grid(row=1, column=1, sticky=tk.W, pady=(8, 0))

        summary_bar = ttk.LabelFrame(root, text="Library Summary", style="Card.TLabelframe", padding=10)
        summary_bar.grid(row=1, column=0, sticky=EW, pady=(12, 8))
        summary_bar.columnconfigure(0, weight=1)

        self.summary_var = tk.StringVar(value="Choose a directory and start a scan.")
        ttk.Label(summary_bar, textvariable=self.summary_var).grid(row=0, column=0, sticky=EW)
        ttk.Label(summary_bar, text="Search").grid(row=0, column=1, padx=(12, 6))
        self.search_entry = ttk.Entry(summary_bar, textvariable=self.search_var, width=28)
        self.search_entry.grid(row=0, column=2, sticky=EW, padx=(0, 8))
        self.search_var.trace_add("write", self.on_search_changed)
        self.export_button = ttk.Button(summary_bar, text="Export CSV", command=self.export_csv, state=tk.DISABLED)
        self.export_button.grid(row=0, column=3)
        self.log_button = ttk.Button(summary_bar, text="Open Log", command=self.open_log)
        self.log_button.grid(row=0, column=4, padx=(8, 0))

        tree_frame = ttk.LabelFrame(root, text="Library Browser", style="Card.TLabelframe", padding=10)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("kind", "quality", "extension", "size", "files", "folders", "skipped", "path")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Name", command=lambda: self.sort_tree("name"))
        self.tree.heading("kind", text="Kind", command=lambda: self.sort_tree("kind"))
        self.tree.heading("quality", text="Format", command=lambda: self.sort_tree("quality"))
        self.tree.heading("extension", text="Ext", command=lambda: self.sort_tree("extension"))
        self.tree.heading("size", text="Size", command=lambda: self.sort_tree("size"))
        self.tree.heading("files", text="Files", command=lambda: self.sort_tree("files"))
        self.tree.heading("folders", text="Folders", command=lambda: self.sort_tree("folders"))
        self.tree.heading("skipped", text="Skipped", command=lambda: self.sort_tree("skipped"))
        self.tree.heading("path", text="Full Path", command=lambda: self.sort_tree("path"))

        self.tree.column("#0", width=300, minwidth=190)
        self.tree.column("kind", width=74, anchor=tk.W)
        self.tree.column("quality", width=84, anchor=tk.W)
        self.tree.column("extension", width=70, anchor=tk.W)
        self.tree.column("size", width=110, anchor=tk.E)
        self.tree.column("files", width=88, anchor=tk.E)
        self.tree.column("folders", width=88, anchor=tk.E)
        self.tree.column("skipped", width=80, anchor=tk.E)
        self.tree.column("path", width=420, minwidth=240)

        y_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<Delete>", self.delete_selected)

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="Open", command=self.open_selected)
        self.context_menu.add_command(label="Open Location", command=self.open_selected_location)
        self.context_menu.add_command(label="Refresh Selected Movie", command=self.refresh_selected_movie)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Probe Format with ffprobe", command=self.probe_selected)
        self.context_menu.add_command(label="Probe Missing Formats Here", command=self.probe_missing_under_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Radarr: Change Profile and Search", command=self.radarr_replace_selected)
        self.context_menu.add_command(label="Radarr: Retry/Search Movie", command=self.radarr_search_selected)
        self.context_menu.add_command(label="Radarr: Choose Release...", command=self.radarr_choose_release_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Delete", command=self.delete_selected)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky=EW)

        status_bar = ttk.Frame(root)
        status_bar.grid(row=3, column=0, sticky=EW, pady=(8, 0))
        status_bar.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=LEFT, fill=X, expand=True)
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=180)
        self.progress.pack(side=RIGHT)

    def browse_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if selected:
            self.path_var.set(selected)

    def open_radarr_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Radarr Connection")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        base_url_var = tk.StringVar(value=self.radarr_config.base_url)
        api_key_var = tk.StringVar(value=self.radarr_config.api_key)
        ttk.Label(frame, text="Radarr URL").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=(0, 8))
        ttk.Entry(frame, textvariable=base_url_var, width=48).grid(row=0, column=1, sticky=EW, pady=(0, 8))

        ttk.Label(frame, text="API Key").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=(0, 8))
        ttk.Entry(frame, textvariable=api_key_var, width=48, show="*").grid(row=1, column=1, sticky=EW, pady=(0, 8))

        status_var = tk.StringVar(value="Quality profile is chosen from the right-click Radarr action.")
        ttk.Label(frame, textvariable=status_var).grid(row=2, column=0, columnspan=2, sticky=EW, pady=(0, 10))

        def save_settings() -> None:
            new_base_url = base_url_var.get().strip().rstrip("/")
            new_api_key = api_key_var.get().strip()
            server_changed = (
                new_base_url != self.radarr_config.base_url.strip().rstrip("/")
                or new_api_key != self.radarr_config.api_key.strip()
            )
            self.radarr_config = RadarrConfig(
                base_url=new_base_url,
                api_key=new_api_key,
                target_quality_profile_id=None if server_changed else self.radarr_config.target_quality_profile_id,
                target_quality_profile_name="" if server_changed else self.radarr_config.target_quality_profile_name,
            )
            save_radarr_config(self.radarr_config)
            if server_changed:
                log_event(f"Radarr server/API changed; cleared cached quality profile for {new_base_url}")
            dialog.destroy()

        def test_settings() -> None:
            test_config = RadarrConfig(
                base_url=base_url_var.get().strip().rstrip("/"),
                api_key=api_key_var.get().strip(),
            )
            try:
                client = RadarrClient(test_config)
                movies = client.get_movies()
                profiles = client.get_quality_profiles()
            except RuntimeError as exc:
                status_var.set("Connection failed.")
                messagebox.showerror(APP_TITLE, f"Could not connect to this Radarr server:\n\n{exc}", parent=dialog)
                return
            status_var.set(f"Connected: {len(movies):,} movies, {len(profiles):,} quality profiles.")
            messagebox.showinfo(
                APP_TITLE,
                f"Connected to Radarr.\n\nMovies: {len(movies):,}\nQuality profiles: {len(profiles):,}",
                parent=dialog,
            )

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=3, column=0, columnspan=2, sticky=EW)
        ttk.Button(button_bar, text="Save", command=save_settings).pack(side=RIGHT)
        ttk.Button(button_bar, text="Test", command=test_settings).pack(side=RIGHT, padx=(0, 8))
        ttk.Button(button_bar, text="Cancel", command=dialog.destroy).pack(side=RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _event: save_settings())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.wait_window()

    def open_log(self) -> None:
        try:
            LOG_FILE.touch(exist_ok=True)
            os.startfile(LOG_FILE)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not open log:\n{LOG_FILE}\n\n{exc}")

    def start_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if self.refresh_thread and self.refresh_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "Please wait for the selected movie refresh to finish before starting a full scan.")
            return

        selected_path = Path(self.path_var.get().strip()).expanduser()
        if not selected_path.exists() or not selected_path.is_dir():
            messagebox.showerror(APP_TITLE, "Please enter a valid directory path.")
            return

        self.cancel_event.clear()
        self.current_report = None
        self.current_rows = []
        self.tree_items = {}
        self.scan_started_at = time.time()
        self._set_scanning(True)
        self._clear_tree()
        self.summary_var.set("Scanning...")
        ffprobe_path = self.ffprobe_path if self.probe_during_scan_var.get() else None
        if self.probe_during_scan_var.get() and not ffprobe_path:
            self.status_var.set("ffprobe not found; scanning with filename tags only.")
        else:
            self.status_var.set(f"Scanning {selected_path}")

        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(selected_path, ffprobe_path),
            daemon=True,
        )
        self.scan_thread.start()

    def cancel_scan(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling scan...")

    def _scan_worker(self, selected_path: Path, ffprobe_path: str | None) -> None:
        try:
            report = scan_folder(selected_path, self.cancel_event, self.progress_queue, ffprobe_path)
            if self.cancel_event.is_set():
                self.progress_queue.put(("cancelled", report))
            else:
                self.progress_queue.put(("done", report))
        except Exception as exc:
            log_exception("Scan worker failed", exc)
            self.progress_queue.put(("error", str(exc)))

    def _log_queue_event(self, event: str, payload: object) -> None:
        if event in {"progress", "refresh_progress", "probe_progress", "probe_result"}:
            return
        log_event(f"Queue event {event}: {self._summarize_log_payload(payload)}")

    def _summarize_log_payload(self, payload: object) -> str:
        if isinstance(payload, FileSystemItem):
            return (
                f"{payload.path} size={payload.size} files={payload.file_count} "
                f"folders={payload.folder_count} skipped={payload.skipped_count}"
            )
        if isinstance(payload, tuple) and payload:
            parts: list[str] = []
            for value in payload:
                if isinstance(value, FileSystemItem):
                    parts.append(str(value.path))
                elif isinstance(value, list):
                    parts.append(f"list[{len(value)}]")
                elif isinstance(value, dict):
                    title = value.get("title") or value.get("movie_id") or "dict"
                    parts.append(str(title))
                else:
                    parts.append(str(value))
            text = " | ".join(parts)
        else:
            text = str(payload)
        return text[:1000]

    def _poll_queue(self) -> None:
        try:
            while True:
                event, payload = self.progress_queue.get_nowait()
                self._log_queue_event(event, payload)
                if event == "progress":
                    text = str(payload)
                    self.status_var.set(text if text.startswith("Probing ") else f"Scanning {text}")
                elif event == "done":
                    self._finish_scan(payload, cancelled=False)
                elif event == "cancelled":
                    self._finish_scan(payload, cancelled=True)
                elif event == "error":
                    self._set_scanning(False)
                    messagebox.showerror(APP_TITLE, f"Scan failed:\n{payload}")
                    self.status_var.set("Scan failed.")
                    self.summary_var.set("Scan failed.")
                elif event == "refresh_progress":
                    self.status_var.set(payload)
                elif event == "refresh_done":
                    target, refreshed, elapsed = payload
                    self._finish_selected_refresh(target, refreshed, elapsed)
                elif event == "refresh_error":
                    self.progress.stop()
                    self.status_var.set("Refresh failed.")
                    messagebox.showerror(APP_TITLE, f"Refresh failed:\n{payload}")
                elif event == "probe_progress":
                    self.status_var.set(payload)
                elif event == "probe_result":
                    item, quality = payload
                    if quality:
                        self._apply_probed_quality(item, quality)
                elif event == "probe_done":
                    count, changed, errors = payload
                    self._render_tree()
                    self.current_rows = self._flatten_report(self.current_report) if self.current_report else []
                    self.status_var.set(f"ffprobe complete: {changed}/{count} formats updated.")
                    if errors:
                        messagebox.showwarning(
                            APP_TITLE,
                            f"ffprobe finished with {errors} file(s) it could not read.",
                        )
                elif event == "radarr_progress":
                    self.status_var.set(payload)
                elif event == "radarr_done":
                    self.status_var.set(payload)
                    messagebox.showinfo(APP_TITLE, payload)
                elif event == "radarr_download_complete":
                    message, file_path = payload
                    self.status_var.set(message)
                    messagebox.showinfo(APP_TITLE, message)
                    if file_path:
                        self._refresh_completed_download(Path(file_path))
                elif event == "radarr_error":
                    self.status_var.set("Radarr action failed.")
                    messagebox.showerror(APP_TITLE, str(payload))
                elif event == "radarr_confirm_replace":
                    matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary = payload
                    self._confirm_radarr_replace(matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary)
                elif event == "radarr_confirm_search":
                    matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary = payload
                    self._confirm_radarr_search(matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary)
                elif event == "radarr_choose_release":
                    movie, selected_path, profile_id, profile_name, max_resolution, releases = payload
                    self._choose_and_grab_radarr_release(movie, selected_path, profile_id, profile_name, max_resolution, releases)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish_scan(self, report: FileSystemItem, cancelled: bool) -> None:
        self.current_report = report
        self.current_rows = self._flatten_report(report)
        self._render_tree()
        self._set_scanning(False)

        elapsed = time.time() - self.scan_started_at
        status = "Cancelled" if cancelled else "Completed"
        self.summary_var.set(
            f"{status}: {format_size(report.size)} across "
            f"{report.file_count:,} files and {report.folder_count:,} folders "
            f"({report.skipped_count:,} skipped) in {elapsed:.1f}s."
        )
        self.status_var.set("Ready.")
        self.export_button.configure(state=tk.NORMAL if self.current_rows else tk.DISABLED)

    def _set_scanning(self, scanning: bool) -> None:
        state_when_idle = tk.NORMAL if not scanning else tk.DISABLED
        self.path_entry.configure(state=state_when_idle)
        self.browse_button.configure(state=state_when_idle)
        self.scan_button.configure(state=state_when_idle)
        self.radarr_button.configure(state=state_when_idle)
        self.probe_during_scan_check.configure(state=state_when_idle)
        self.cancel_button.configure(state=tk.NORMAL if scanning else tk.DISABLED)
        self.export_button.configure(state=tk.DISABLED if scanning else (tk.NORMAL if self.current_rows else tk.DISABLED))
        if scanning:
            self.progress.start(12)
        else:
            self.progress.stop()

    def sort_tree(self, column: str) -> None:
        if not self.current_report:
            return
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = column in {"size", "files", "folders", "skipped"}
        self._render_tree()

    def on_search_changed(self, *_args: object) -> None:
        self._jump_to_search_match()

    def _render_tree(self) -> None:
        self._clear_tree()
        self.tree_items = {}
        self.item_tree_ids = {}
        if not self.current_report:
            return
        self._insert_item("", self.current_report, open_item=True)
        self._jump_to_search_match()

    def _insert_item(
        self,
        parent_id: str,
        item: FileSystemItem,
        open_item: bool = False,
    ) -> None:
        item_id = self.tree.insert(
            parent_id,
            END,
            text=item.name,
            values=(
                item.kind,
                item.quality or "-",
                display_extension(item),
                format_size(item.size),
                display_count(item.file_count, item.item_type == "folder"),
                display_count(item.folder_count, item.item_type == "folder"),
                display_count(item.skipped_count),
                str(item.path),
            ),
            open=open_item,
        )
        self.tree_items[item_id] = item
        self.item_tree_ids[id(item)] = item_id

        for child in self._sorted_children(item.children):
            self._insert_item(item_id, child)

    def _search_terms(self) -> list[str]:
        return [term for term in self.search_var.get().strip().lower().split() if term]

    def _jump_to_search_match(self) -> None:
        search_terms = self._search_terms()
        if not search_terms or not self.current_report:
            return

        target = self._find_search_target(self.current_report, search_terms)
        if not target:
            self.status_var.set(f"No match for: {self.search_var.get().strip()}")
            return

        item_id = self.item_tree_ids.get(id(target))
        if not item_id:
            return
        self._open_ancestors(item_id)
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.see(item_id)
        self.status_var.set(f"Found: {target.path}")

    def _find_search_target(self, item: FileSystemItem, search_terms: list[str]) -> FileSystemItem | None:
        first_file_parent: FileSystemItem | None = None
        for current in self._walk_items(item):
            if not self._item_matches_search(current, search_terms):
                continue
            if current.item_type == "folder":
                return current
            if first_file_parent is None and current.parent:
                first_file_parent = current.parent
        return first_file_parent

    def _walk_items(self, item: FileSystemItem) -> list[FileSystemItem]:
        rows = [item]
        for child in self._sorted_children(item.children):
            rows.extend(self._walk_items(child))
        return rows

    def _open_ancestors(self, item_id: str) -> None:
        parent_id = self.tree.parent(item_id)
        while parent_id:
            self.tree.item(parent_id, open=True)
            parent_id = self.tree.parent(parent_id)

    def _item_matches_search(self, item: FileSystemItem, search_terms: list[str]) -> bool:
        if not search_terms:
            return True
        searchable = " ".join(
            [
                item.name,
                item.kind,
                item.quality,
                item.extension,
                str(item.path),
                format_size(item.size),
            ]
        ).lower()
        return all(term in searchable for term in search_terms)

    def _sorted_children(self, children: list[FileSystemItem]) -> list[FileSystemItem]:
        key_map = {
            "name": lambda item: item.name.lower(),
            "kind": lambda item: item.kind,
            "quality": lambda item: item.quality,
            "extension": lambda item: item.extension,
            "size": lambda item: item.size,
            "files": lambda item: item.file_count,
            "folders": lambda item: item.folder_count,
            "skipped": lambda item: item.skipped_count,
            "path": lambda item: str(item.path).lower(),
        }
        return sorted(children, key=key_map[self.sort_column], reverse=self.sort_reverse)

    def _flatten_report(self, item: FileSystemItem) -> list[FileSystemItem]:
        rows = [item]
        for child in item.children:
            rows.extend(self._flatten_report(child))
        return rows

    def _clear_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def show_context_menu(self, event: tk.Event) -> None:
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        if item_id not in self.tree.selection():
            self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def selected_item(self) -> FileSystemItem | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.tree_items.get(selection[0])

    def selected_items(self) -> list[FileSystemItem]:
        return [self.tree_items[item_id] for item_id in self.tree.selection() if item_id in self.tree_items]

    def open_selected(self, _event: tk.Event | None = None) -> None:
        item = self.selected_item()
        if not item:
            return
        try:
            os.startfile(item.path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not open:\n{item.path}\n\n{exc}")

    def open_selected_location(self) -> None:
        item = self.selected_item()
        if not item:
            return

        try:
            if item.item_type == "file":
                subprocess.run(["explorer", "/select,", str(item.path)], check=False)
            else:
                os.startfile(item.path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not open location:\n{item.path}\n\n{exc}")

    def refresh_selected_movie(self) -> None:
        item = self.selected_item()
        if not item:
            return
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "Please wait for the current scan to finish before refreshing.")
            return
        if self.refresh_thread and self.refresh_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "A selected movie refresh is already running.")
            return

        target = item if item.item_type == "folder" else item.parent
        if not target:
            messagebox.showwarning(APP_TITLE, "Select a movie folder or a file inside a movie folder.")
            return
        if not target.path.exists() or not target.path.is_dir():
            messagebox.showerror(APP_TITLE, f"Cannot refresh missing folder:\n{target.path}")
            return

        self._start_refresh_for_target(target)

    def _start_refresh_for_target(self, target: FileSystemItem) -> bool:
        if self.refresh_thread and self.refresh_thread.is_alive():
            return False
        ffprobe_path = self.ffprobe_path or find_ffprobe()
        self.status_var.set(f"Refreshing {target.path}")
        self.progress.start(12)
        self.refresh_thread = threading.Thread(
            target=self._refresh_selected_worker,
            args=(target, ffprobe_path),
            daemon=True,
        )
        self.refresh_thread.start()
        return True

    def _refresh_completed_download(self, file_path: Path) -> None:
        target = self._find_report_folder_for_path(file_path)
        if not target:
            self.status_var.set(f"Download completed, but no matching scanned folder was found for {file_path}")
            return
        if not self._start_refresh_for_target(target):
            self.status_var.set(f"Download completed for {file_path}; refresh is busy, use Refresh Selected Movie after it finishes.")

    def _find_report_folder_for_path(self, path: Path) -> FileSystemItem | None:
        selected = normalize_media_path(path)
        best_match: FileSystemItem | None = None
        best_score = -1
        for row in self.current_rows:
            if row.item_type != "folder":
                continue
            candidate = normalize_media_path(row.path)
            if selected == candidate or selected.startswith(candidate + "/") or candidate.startswith(selected + "/"):
                score = len(candidate)
                if score > best_score:
                    best_match = row
                    best_score = score
        if best_match:
            return best_match

        for selected_name in path_name_candidates(path):
            selected_label = normalize_movie_label(selected_name)
            selected_year = movie_year_from_label(selected_name)
            if not selected_label:
                continue
            for row in self.current_rows:
                if row.item_type != "folder":
                    continue
                row_label = normalize_movie_label(row.path.name)
                row_year = movie_year_from_label(row.path.name)
                year_matches = not selected_year or not row_year or selected_year == row_year
                if year_matches and selected_label == row_label:
                    log_event(f"Matched Radarr path by folder label: {path} -> {row.path}")
                    return row
        return best_match

    def _refresh_selected_worker(self, target: FileSystemItem, ffprobe_path: str | None) -> None:
        started_at = time.time()
        log_event(f"Refresh started: {target.path}; ffprobe={'yes' if ffprobe_path else 'no'}")

        class RefreshProgressQueue:
            def put(self, message: tuple[str, object]) -> None:
                event, payload = message
                if event == "progress":
                    text = str(payload)
                    status = text if text.startswith("Probing ") else f"Refreshing {text}"
                    self_outer.progress_queue.put(("refresh_progress", status))

        self_outer = self

        try:
            cancel_event = threading.Event()
            refreshed = scan_folder(target.path, cancel_event, RefreshProgressQueue(), None)
            if ffprobe_path:
                probe_report_files(refreshed, ffprobe_path, RefreshProgressQueue())
            elapsed = time.time() - started_at
            log_event(f"Refresh finished: {target.path}; elapsed={elapsed:.1f}s")
            self.progress_queue.put(("refresh_done", (target, refreshed, elapsed)))
        except Exception as exc:
            log_exception(f"Refresh worker failed: {target.path}", exc)
            self.progress_queue.put(("refresh_error", str(exc)))

    def _finish_selected_refresh(self, target: FileSystemItem, refreshed: FileSystemItem, elapsed: float) -> None:
        refreshed.parent = target.parent
        if self.current_report is target:
            self.current_report = refreshed
        elif target.parent:
            siblings = target.parent.children
            for index, child in enumerate(siblings):
                if child is target:
                    siblings[index] = refreshed
                    break
            ancestor = refreshed.parent
            while ancestor:
                self._recalculate_folder_totals(ancestor)
                ancestor = ancestor.parent

        if self.current_report:
            self._recalculate_folder_totals(self.current_report)
            self.current_rows = self._flatten_report(self.current_report)
        self._render_tree()
        self.progress.stop()
        self._refresh_summary("After refresh")
        messagebox.showinfo(
            APP_TITLE,
            f"Refresh complete:\n{refreshed.path}\n\n"
            f"{format_size(refreshed.size)} across {refreshed.file_count:,} files "
            f"and {refreshed.folder_count:,} folders in {elapsed:.1f}s.",
        )
        self.status_var.set(f"Refresh complete: {refreshed.path}")

    def _recalculate_folder_totals(self, item: FileSystemItem) -> None:
        if item.item_type != "folder":
            return
        item.size = 0
        item.file_count = 0
        item.folder_count = 0
        item.skipped_count = 0
        for child in item.children:
            child.parent = item
            item.size += child.size
            item.file_count += child.file_count
            item.skipped_count += child.skipped_count
            if child.item_type == "folder":
                item.folder_count += child.folder_count + 1
        item.quality = folder_quality(item)

    def delete_selected(self, _event: tk.Event | None = None) -> None:
        item = self.selected_item()
        if not item:
            return
        if self.scan_thread and self.scan_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "Please wait for the current scan to finish before deleting.")
            return
        if self.current_report and item.path == self.current_report.path:
            messagebox.showwarning(APP_TITLE, "The scan root cannot be deleted from inside this report.")
            return

        action = "folder and everything inside it" if item.item_type == "folder" else "file"
        confirmed = messagebox.askyesno(
            APP_TITLE,
            f"Delete this {action}?\n\n{item.path}\n\nThis permanently deletes it from disk.",
            icon=messagebox.WARNING,
        )
        if not confirmed:
            return

        try:
            if item.item_type == "folder":
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not delete:\n{item.path}\n\n{exc}")
            return

        self._remove_item_from_report(item)
        self._render_tree()
        self._refresh_summary_after_delete()
        self.status_var.set(f"Deleted {item.path}")

    def probe_selected(self) -> None:
        item = self.selected_item()
        if not item:
            return
        self._start_probe_for_item(item, only_missing=False)

    def probe_missing_under_selected(self) -> None:
        item = self.selected_item()
        if not item:
            return
        self._start_probe_for_item(item, only_missing=False)

    def _start_probe_for_item(self, item: FileSystemItem, only_missing: bool) -> None:
        target_folder = item if item.item_type == "folder" else item.parent
        targets = self._probe_targets_from_disk(item, only_missing)
        self._start_probe(targets, refresh_target=target_folder)

    def _start_probe(self, targets: list[FileSystemItem], refresh_target: FileSystemItem | None = None) -> None:
        if self.probe_thread and self.probe_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "ffprobe is already running.")
            return

        targets = [item for item in targets if item.item_type == "file" and item.path.exists() and is_probe_candidate(item.path, item.size)]
        log_event(f"Manual ffprobe requested: targets={len(targets)}")
        if not targets:
            messagebox.showinfo(
                APP_TITLE,
                "No probe candidates found.\n\n"
                "Known video extensions and large non-sidecar files are probed. "
                "Subtitle, metadata, image, and partial download files are skipped.",
            )
            return

        ffprobe_path = find_ffprobe()
        if not ffprobe_path:
            messagebox.showerror(
                APP_TITLE,
                "ffprobe was not found.\n\nPut ffprobe.exe in this app folder or install FFmpeg and add it to PATH.",
            )
            return

        if len(targets) > 25:
            confirmed = messagebox.askyesno(
                APP_TITLE,
                f"Probe {len(targets):,} media files with ffprobe?\n\n"
                "This can take a while on large movie folders.",
                icon=messagebox.WARNING,
            )
            if not confirmed:
                return

        self.probe_thread = threading.Thread(
            target=self._probe_worker,
            args=(targets, ffprobe_path, refresh_target),
            daemon=True,
        )
        self.probe_thread.start()

    def _probe_worker(self, targets: list[FileSystemItem], ffprobe_path: str, refresh_target: FileSystemItem | None = None) -> None:
        changed = 0
        errors = 0
        count = len(targets)
        log_event(f"Manual ffprobe started: targets={count}; refresh_target={refresh_target.path if refresh_target else 'none'}")
        for index, item in enumerate(targets, start=1):
            self.progress_queue.put(("probe_progress", f"ffprobe {index:,}/{count:,}: {item.path.name}"))
            detail = ""
            try:
                quality, detail = probe_media_quality(item.path, ffprobe_path)
            except (OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
                quality = ""
            if quality:
                changed += 1
                log_event(f"Manual ffprobe file: {item.path}: {quality}")
                self.progress_queue.put(("probe_result", (item, quality)))
            else:
                errors += 1
                log_event(f"Manual ffprobe could not read file: {item.path}: {detail or 'no video stream detected'}")
        self.progress_queue.put(("probe_done", (count, changed, errors)))
        if refresh_target and refresh_target.path.exists():
            self._refresh_selected_worker(refresh_target, ffprobe_path)

    def _probe_targets_from_disk(self, item: FileSystemItem, only_missing: bool) -> list[FileSystemItem]:
        if item.item_type == "file":
            return [self._file_item_from_disk(item.path, item.parent)] if item.path.exists() else []

        if not item.path.exists() or not item.path.is_dir():
            return []

        targets: list[FileSystemItem] = []
        for root, _dirs, files in os.walk(item.path):
            for file_name in files:
                file_path = Path(root) / file_name
                try:
                    size = file_path.stat().st_size
                except OSError:
                    continue
                if is_probe_candidate(file_path, size):
                    targets.append(self._file_item_from_disk(file_path, item))
        return targets

    def _file_item_from_disk(self, path: Path, parent: FileSystemItem | None) -> FileSystemItem:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return FileSystemItem(
            path=path,
            item_type="file",
            size=size,
            file_count=1,
            quality=infer_quality_from_name(path),
            extension=path.suffix.lower(),
            parent=parent,
        )

    def _media_files_under(self, item: FileSystemItem, only_missing: bool) -> list[FileSystemItem]:
        if item.item_type == "file":
            if only_missing and item.quality:
                return []
            return [item]

        targets: list[FileSystemItem] = []
        for child in item.children:
            targets.extend(self._media_files_under(child, only_missing))
        return targets

    def _apply_probed_quality(self, item: FileSystemItem, quality: str) -> None:
        item.quality = quality
        ancestor = item.parent
        while ancestor:
            ancestor.quality = folder_quality(ancestor)
            ancestor = ancestor.parent

    def _remove_item_from_report(self, item: FileSystemItem) -> None:
        if item.parent:
            item.parent.children = [child for child in item.parent.children if child is not item]
            ancestor = item.parent
            while ancestor:
                ancestor.size = max(0, ancestor.size - item.size)
                ancestor.file_count = max(0, ancestor.file_count - item.file_count)
                ancestor.folder_count = max(0, ancestor.folder_count - (item.folder_count + (1 if item.item_type == "folder" else 0)))
                ancestor.skipped_count = max(0, ancestor.skipped_count - item.skipped_count)
                ancestor.quality = folder_quality(ancestor)
                ancestor = ancestor.parent

        self.current_rows = self._flatten_report(self.current_report) if self.current_report else []

    def _refresh_summary_after_delete(self) -> None:
        self._refresh_summary("After delete")

    def _refresh_summary(self, prefix: str) -> None:
        report = self.current_report
        if not report:
            return
        self.summary_var.set(
            f"{prefix}: {format_size(report.size)} across "
            f"{report.file_count:,} files and {report.folder_count:,} folders "
            f"({report.skipped_count:,} skipped)."
        )
        self.export_button.configure(state=tk.NORMAL if self.current_rows else tk.DISABLED)

    def radarr_replace_selected(self) -> None:
        self.radarr_config = load_radarr_config()
        items = self.selected_items()
        if not items or not self._ensure_radarr_ready(require_profile=False):
            return
        if self.radarr_thread and self.radarr_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "A Radarr action is already running.")
            return
        profile = self._choose_radarr_quality_profile()
        if not profile:
            return
        profile_id, profile_name, max_resolution, allowed_summary = profile
        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=(
                "prepare profile/search",
                self._radarr_prepare_replace_worker,
                [item.path for item in items],
                profile_id,
                profile_name,
                max_resolution,
                allowed_summary,
            ),
            daemon=True,
        )
        self.radarr_thread.start()

    def radarr_search_selected(self) -> None:
        self.radarr_config = load_radarr_config()
        items = self.selected_items()
        if not items or not self._ensure_radarr_ready(require_profile=False):
            return
        if self.radarr_thread and self.radarr_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "A Radarr action is already running.")
            return
        profile = self._choose_radarr_quality_profile()
        if not profile:
            return
        profile_id, profile_name, max_resolution, allowed_summary = profile
        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=(
                "prepare retry/search",
                self._radarr_prepare_search_worker,
                [item.path for item in items],
                profile_id,
                profile_name,
                max_resolution,
                allowed_summary,
            ),
            daemon=True,
        )
        self.radarr_thread.start()

    def radarr_choose_release_selected(self) -> None:
        self.radarr_config = load_radarr_config()
        items = self.selected_items()
        if not items or not self._ensure_radarr_ready(require_profile=False):
            return
        if len(items) != 1:
            messagebox.showwarning(APP_TITLE, "Choose one movie at a time when selecting a specific Radarr release.")
            return
        if self.radarr_thread and self.radarr_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "A Radarr action is already running.")
            return
        profile = self._choose_radarr_quality_profile()
        if not profile:
            return
        profile_id, profile_name, max_resolution, allowed_summary = profile
        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=(
                "prepare choose release",
                self._radarr_prepare_choose_release_worker,
                items[0].path,
                profile_id,
                profile_name,
                max_resolution,
                allowed_summary,
            ),
            daemon=True,
        )
        self.radarr_thread.start()
    def _radarr_worker_guard(self, action: str, worker: object, *args: object) -> None:
        try:
            worker(*args)
        except Exception as exc:
            log_exception(f"Radarr worker failed: {action}", exc)
            self.progress_queue.put(("radarr_error", f"Radarr {action} failed:\n{exc}"))

    def _ensure_radarr_ready(self, require_profile: bool) -> bool:
        if not self.radarr_config.is_ready or (require_profile and not self.radarr_config.target_quality_profile_id):
            self.open_radarr_settings()
        if not self.radarr_config.is_ready:
            messagebox.showerror(APP_TITLE, "Configure the Radarr URL and API key first.")
            return False
        if require_profile and not self.radarr_config.target_quality_profile_id:
            messagebox.showerror(APP_TITLE, "Choose the smaller Radarr quality profile first.")
            return False
        return True

    def _choose_radarr_quality_profile(self) -> tuple[int, str, int, str] | None:
        self.radarr_config = load_radarr_config()
        try:
            client = RadarrClient(self.radarr_config)
            profiles = client.get_quality_profiles()
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return None

        profiles_by_display: dict[str, dict] = {}
        display_by_name: dict[str, str] = {}
        for profile in profiles:
            name = str(profile.get("name") or "")
            if name and profile.get("id") is not None:
                display_name = profile_display_name(profile)
                profiles_by_display[display_name] = profile
                display_by_name[name] = display_name
        display_names = sorted(profiles_by_display)
        if not display_names:
            messagebox.showerror(APP_TITLE, "Radarr did not return any quality profiles.")
            return None

        result: dict[str, tuple[int, str, int, str] | None] = {"profile": None}
        dialog = tk.Toplevel(self)
        dialog.title("Choose Radarr Profile")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Quality profile for selected movie/movies").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        profile_var = tk.StringVar()
        combo = ttk.Combobox(frame, textvariable=profile_var, values=display_names, state="readonly", width=76)
        combo.grid(row=1, column=0, sticky=EW, pady=(0, 12))
        if self.radarr_config.target_quality_profile_name in display_by_name:
            profile_var.set(display_by_name[self.radarr_config.target_quality_profile_name])
        else:
            profile_var.set(display_names[0])

        def start() -> None:
            selected_display = profile_var.get()
            selected_profile = profiles_by_display.get(selected_display)
            if not selected_profile:
                messagebox.showerror(APP_TITLE, "Choose a Radarr quality profile.", parent=dialog)
                return
            selected_name = str(selected_profile.get("name") or "")
            profile_id = int(selected_profile["id"])
            max_resolution = profile_max_resolution(selected_profile)
            allowed_summary = profile_allowed_summary(selected_profile)
            if not self._validate_radarr_profile_id(client, profile_id, selected_name):
                return
            self.radarr_config.target_quality_profile_id = profile_id
            self.radarr_config.target_quality_profile_name = selected_name
            save_radarr_config(self.radarr_config)
            result["profile"] = (profile_id, selected_name, max_resolution, allowed_summary)
            dialog.destroy()

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=2, column=0, sticky=EW)
        ttk.Button(button_bar, text="Start Search", command=start).pack(side=RIGHT)
        ttk.Button(button_bar, text="Cancel", command=dialog.destroy).pack(side=RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _event: start())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        combo.focus_set()
        dialog.wait_window()
        return result["profile"]

    def _validate_radarr_profile_id(self, client: RadarrClient, profile_id: int, profile_name: str) -> bool:
        try:
            profiles = client.get_quality_profiles()
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return False
        for profile in profiles:
            if int(profile.get("id") or 0) == profile_id and str(profile.get("name") or "") == profile_name:
                return True
        messagebox.showerror(
            APP_TITLE,
            "The selected quality profile is not valid on the currently configured Radarr server.\n\n"
            "Open Radarr settings, confirm the URL/API key, then choose the profile again.",
        )
        return False

    def _radarr_prepare_replace_worker(
        self,
        selected_paths: list[Path],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
        allowed_summary: str,
    ) -> None:
        try:
            self.progress_queue.put(("radarr_progress", "Finding selected movie in Radarr..."))
            client = RadarrClient(self.radarr_config)
            matches, unmatched_paths = self._match_radarr_movies(client, selected_paths)
            if not matches:
                self.progress_queue.put(("radarr_error", self._radarr_match_error(selected_paths[0], client)))
                return
            self.progress_queue.put(("radarr_confirm_replace", (matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary)))
        except RuntimeError as exc:
            self.progress_queue.put(("radarr_error", str(exc)))

    def _radarr_prepare_search_worker(
        self,
        selected_paths: list[Path],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
        allowed_summary: str,
    ) -> None:
        try:
            self.progress_queue.put(("radarr_progress", "Finding selected movie in Radarr..."))
            client = RadarrClient(self.radarr_config)
            matches, unmatched_paths = self._match_radarr_movies(client, selected_paths)
            if not matches:
                self.progress_queue.put(("radarr_error", self._radarr_match_error(selected_paths[0], client)))
                return
            self.progress_queue.put(("radarr_confirm_search", (matches, unmatched_paths, profile_id, profile_name, max_resolution, allowed_summary)))
        except RuntimeError as exc:
            self.progress_queue.put(("radarr_error", str(exc)))

    def _radarr_prepare_choose_release_worker(
        self,
        selected_path: Path,
        profile_id: int,
        profile_name: str,
        max_resolution: int,
        _allowed_summary: str,
    ) -> None:
        try:
            self.progress_queue.put(("radarr_progress", "Finding selected movie in Radarr..."))
            client = RadarrClient(self.radarr_config)
            matches, unmatched_paths = self._match_radarr_movies(client, [selected_path])
            if not matches:
                self.progress_queue.put(("radarr_error", self._radarr_match_error(selected_path, client)))
                return
            movie, matched_path = matches[0]
            title = str(movie.get("title") or "Movie")
            movie_id = int(movie["id"])
            self.progress_queue.put(("radarr_progress", f"Updating profile for {title}..."))
            updated_movie = client.update_movie_quality_profile(movie, profile_id)
            confirmed_movie = client.get_movie(movie_id)
            if int(confirmed_movie.get("qualityProfileId") or 0) != profile_id:
                raise RuntimeError(f"Radarr did not keep the selected profile {profile_name} on {title}.")
            self.progress_queue.put(("radarr_progress", f"Loading Radarr releases for {title}..."))
            releases = client.get_releases(movie_id)
            if not releases:
                self.progress_queue.put((
                    "radarr_error",
                    f"No releases returned by Radarr for {title}.\n\nAvailable formats:\n{available_release_format_list(releases, max_resolution)}",
                ))
                return
            self.progress_queue.put(("radarr_choose_release", (updated_movie, matched_path, profile_id, profile_name, max_resolution, releases)))
        except (KeyError, ValueError, RuntimeError) as exc:
            self.progress_queue.put(("radarr_error", str(exc)))
    def _match_radarr_movies(self, client: RadarrClient, selected_paths: list[Path]) -> tuple[list[tuple[dict, Path]], list[Path]]:
        matches: list[tuple[dict, Path]] = []
        unmatched_paths: list[Path] = []
        seen_movie_ids: set[int] = set()
        for selected_path in selected_paths:
            movie = client.find_movie_for_path(selected_path)
            if not movie:
                unmatched_paths.append(selected_path)
                continue
            try:
                movie_id = int(movie["id"])
            except (KeyError, TypeError, ValueError):
                unmatched_paths.append(selected_path)
                continue
            if movie_id in seen_movie_ids:
                continue
            seen_movie_ids.add(movie_id)
            matches.append((movie, selected_path))
        return matches, unmatched_paths

    def _radarr_match_error(self, selected_path: Path, client: RadarrClient) -> str:
        try:
            samples = client.sample_movie_paths()
        except RuntimeError:
            samples = []
        sample_text = "\n".join(f"- {sample}" for sample in samples) or "- No movies returned by Radarr"
        return (
            "Could not match the selected path to a Radarr movie.\n\n"
            f"Radarr server:\n{client.base_url}\n\n"
            f"Selected path:\n{selected_path}\n\n"
            "Sample paths returned by Radarr:\n"
            f"{sample_text}\n\n"
            "If those paths point to the same library but look different, add path mapping support next."
        )

    def _release_reasons_for_profile(self, release: dict, max_resolution: int) -> list[str]:
        reasons = release_rejections(release)
        resolution = release_quality_resolution(release)
        if max_resolution and resolution and resolution > max_resolution:
            reasons.append(f"above selected max {max_resolution}p")
        return reasons

    def _choose_and_grab_radarr_release(
        self,
        movie: dict,
        _selected_path: Path,
        _profile_id: int,
        profile_name: str,
        max_resolution: int,
        releases: list[dict],
    ) -> None:
        release = self._show_radarr_release_picker(movie, profile_name, max_resolution, releases)
        if not release:
            self.status_var.set("Radarr release selection cancelled.")
            return

        title = str(movie.get("title") or "Movie")
        reasons = self._release_reasons_for_profile(release, max_resolution)
        force = False
        if reasons:
            confirmed = messagebox.askyesno(
                APP_TITLE,
                f"Radarr marks this release as rejected for {title}:\n\n"
                + "\n".join(f"- {reason}" for reason in reasons[:8])
                + "\n\nTry to force-grab this release anyway?",
                icon=messagebox.WARNING,
            )
            if not confirmed:
                self.status_var.set("Radarr release selection cancelled.")
                return
            force = True

        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=("manual release grab", self._radarr_grab_selected_release_worker, movie, release, force, profile_name, max_resolution),
            daemon=True,
        )
        self.radarr_thread.start()

    def _show_radarr_release_picker(
        self,
        movie: dict,
        profile_name: str,
        max_resolution: int,
        releases: list[dict],
    ) -> dict | None:
        title = str(movie.get("title") or "Movie")
        dialog = tk.Toplevel(self)
        dialog.title(f"Choose Release - {title}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("1180x520")
        dialog.minsize(900, 420)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text=f"{title} - {profile_name} (max {max_resolution or 'unknown'}p)").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))

        columns = ("quality", "size", "status", "reasons", "title")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        headings = {
            "quality": ("Quality", 130),
            "size": ("Size", 110),
            "status": ("Status", 90),
            "reasons": ("Radarr Reason", 320),
            "title": ("Release", 480),
        }
        for column, (heading, width) in headings.items():
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=70, anchor=tk.W, stretch=(column in {"reasons", "title"}))

        release_by_id: dict[str, dict] = {}
        sorted_releases = sorted(
            releases,
            key=lambda item: (release_quality_resolution(item), -release_size(item), release_title(item).lower()),
            reverse=True,
        )
        for index, item in enumerate(sorted_releases):
            reasons = self._release_reasons_for_profile(item, max_resolution)
            status = "Accepted" if not reasons else "Rejected"
            reason_text = "; ".join(reasons[:4]) if reasons else ""
            item_id = str(index)
            release_by_id[item_id] = item
            tree.insert(
                "",
                END,
                iid=item_id,
                values=(
                    release_quality_name(item) or "unknown quality",
                    format_size(release_size(item)) if release_size(item) else "unknown size",
                    status,
                    reason_text,
                    release_title(item),
                ),
            )
        if sorted_releases:
            tree.selection_set("0")
            tree.focus("0")

        result: dict[str, dict | None] = {"release": None}

        def choose() -> None:
            selection = tree.selection()
            if not selection:
                messagebox.showwarning(APP_TITLE, "Choose a Radarr release.", parent=dialog)
                return
            result["release"] = release_by_id.get(selection[0])
            dialog.destroy()

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=2, column=0, columnspan=2, sticky=EW, pady=(10, 0))
        ttk.Button(button_bar, text="Grab Selected", command=choose).pack(side=RIGHT)
        ttk.Button(button_bar, text="Cancel", command=dialog.destroy).pack(side=RIGHT, padx=(0, 8))

        tree.bind("<Double-1>", lambda _event: choose())
        dialog.bind("<Return>", lambda _event: choose())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        tree.focus_set()
        dialog.wait_window()
        return result["release"]
    def _radarr_live_status(self, client: RadarrClient, movie_id: int, target: dict, movie: dict) -> tuple[str, bool]:
        title = str(target["title"])
        max_resolution = int(target.get("max_resolution") or 0)
        queue_items = client.get_queue_items(movie_id)
        if queue_items:
            queue_quality = self._queue_quality_name(queue_items[0])
            queue_resolution = self._queue_quality_resolution(queue_items[0])
            if max_resolution and queue_resolution > max_resolution:
                return (
                    f"Wrong quality: {title} grabbed {queue_quality or str(queue_resolution) + 'p'} "
                    f"above selected max {max_resolution}p.",
                    False,
                )
            return self._format_radarr_queue_status(title, queue_items[0]), False

        started_at = float(target.get("monitor_started_at") or 0)
        for history_item in client.get_movie_history(movie_id):
            if radarr_datetime_value(history_item.get("date")) < started_at:
                continue
            event_type = str(history_item.get("eventType") or "")
            source_title = str(history_item.get("sourceTitle") or title)
            if event_type == "downloadFailed":
                return f"Failed: {title} - {source_title}", True
            if event_type == "grabbed":
                quality = self._history_quality_name(history_item)
                quality_resolution = self._history_quality_resolution(history_item)
                if max_resolution and quality_resolution > max_resolution:
                    return f"Wrong quality grabbed: {title} got {quality} above selected max {max_resolution}p.", False
                suffix = f" ({quality})" if quality else ""
                return f"Grabbed: {title}{suffix}", False
            if event_type == "downloadFolderImported":
                quality = self._history_quality_name(history_item)
                quality_resolution = self._history_quality_resolution(history_item)
                if max_resolution and quality_resolution > max_resolution:
                    return f"Imported wrong quality: {title} got {quality} above selected max {max_resolution}p.", True
                return f"Imported: {title}", True

        command_id = target.get("search_command_id")
        if command_id:
            command = client.get_command(int(command_id))
            status = str(command.get("status") or "").lower()
            result = str(command.get("result") or "").lower()
            if status in {"queued", "started", "running"}:
                return f"Searching: {title} ({status})", False
            if status == "completed":
                if result and result != "successful":
                    return f"Search failed: {title} ({result})", True
                if time.time() - started_at > RADARR_DOWNLOAD_POLL_SECONDS:
                    note = str(target.get("grab_note") or "")
                    if note.startswith("automatic search fallback"):
                        return f"No accepted results yet: {title}\n\n{note}", True
                    return f"No results: {title}", True
                return f"Search complete: {title}; waiting for grab status", False
            if status == "failed":
                return f"Search failed: {title}", True

        current_file_id = movie_file_id(movie)
        if current_file_id and current_file_id == int(target["old_movie_file_id"]):
            return f"Waiting: {title}; old file still active", False
        return f"Waiting for Radarr status: {title}", False

    def _format_radarr_queue_status(self, title: str, queue_item: dict) -> str:
        status = str(queue_item.get("status") or "queued")
        tracked_status = str(queue_item.get("trackedDownloadStatus") or "")
        tracked_state = str(queue_item.get("trackedDownloadState") or "")
        size = int(float(queue_item.get("size") or 0))
        size_left = int(float(queue_item.get("sizeleft") or queue_item.get("sizeLeft") or 0))
        percent = ""
        if size > 0:
            complete = max(0.0, min(100.0, (1 - (size_left / size)) * 100))
            percent = f" {complete:.1f}%"
        time_left = str(queue_item.get("timeleft") or queue_item.get("timeLeft") or "")
        detail = tracked_state or tracked_status or status
        message = f"{detail.title()}: {title}{percent}"
        if time_left:
            message += f" remaining {time_left}"
        status_messages = queue_item.get("statusMessages")
        if isinstance(status_messages, list) and status_messages:
            first = status_messages[0]
            if isinstance(first, dict):
                messages = first.get("messages")
                if isinstance(messages, list) and messages:
                    message += f" - {messages[0]}"
        return message

    def _history_quality_name(self, history_item: dict) -> str:
        quality = history_item.get("quality") or {}
        if not isinstance(quality, dict):
            return ""
        quality_detail = quality.get("quality") or {}
        if not isinstance(quality_detail, dict):
            return ""
        return str(quality_detail.get("name") or "")

    def _history_quality_resolution(self, history_item: dict) -> int:
        quality = history_item.get("quality") or {}
        if not isinstance(quality, dict):
            return 0
        quality_detail = quality.get("quality") or {}
        if not isinstance(quality_detail, dict):
            return 0
        try:
            return int(quality_detail.get("resolution") or 0)
        except (TypeError, ValueError):
            return 0

    def _queue_quality_name(self, queue_item: dict) -> str:
        quality = queue_item.get("quality") or {}
        if isinstance(quality, dict):
            quality_detail = quality.get("quality") or {}
            if isinstance(quality_detail, dict):
                return str(quality_detail.get("name") or "")
        return ""

    def _queue_quality_resolution(self, queue_item: dict) -> int:
        quality = queue_item.get("quality") or {}
        if not isinstance(quality, dict):
            return 0
        quality_detail = quality.get("quality") or {}
        if not isinstance(quality_detail, dict):
            return 0
        try:
            return int(quality_detail.get("resolution") or 0)
        except (TypeError, ValueError):
            return 0

    def _grab_best_radarr_release(
        self,
        client: RadarrClient,
        movie_id: int,
        title: str,
        max_resolution: int,
    ) -> tuple[dict | None, dict, str]:
        releases = client.get_releases(movie_id)
        log_event(
            f"Radarr release search: title={title}; movie_id={movie_id}; "
            f"releases={len(releases)}; max_resolution={max_resolution or 'unknown'}"
        )
        if not releases:
            note = (
                "automatic search fallback:\n"
                "No manual release results were returned by Radarr.\n\n"
                "Available formats:\n"
                f"{available_release_format_list(releases, max_resolution)}"
            )
            self.progress_queue.put((
                "radarr_progress",
                f"No manual release results for {title}; asking Radarr to run an automatic search...",
            ))
            command = client.search_movie(movie_id)
            return None, command, note

        candidates: list[dict] = []
        skipped: list[str] = []
        for release in releases:
            quality_name = release_quality_name(release) or "unknown quality"
            resolution = release_quality_resolution(release)
            size = release_size(release)
            reasons = release_rejections(release)
            if max_resolution and resolution and resolution > max_resolution:
                skipped.append(f"{quality_name}: above selected max {max_resolution}p")
                continue
            if reasons:
                skipped.append(f"{quality_name}: {', '.join(reasons[:2])}")
                continue
            candidates.append(release)
            log_event(
                "Radarr release candidate: "
                f"title={title}; quality={quality_name}; resolution={resolution or 'unknown'}; "
                f"size={format_size(size) if size else 'unknown'}; release={release_title(release)}"
            )

        if not candidates:
            detail = summarize_release_skips(skipped)
            available_formats = available_release_format_list(releases, max_resolution)
            note = (
                "automatic search fallback:\n"
                "No acceptable releases found for the selected profile.\n\n"
                "Available formats:\n"
                f"{available_formats}"
            )
            log_event(f"Radarr release search found no acceptable candidate for {title}: {detail}; formats={available_formats}")
            self.progress_queue.put((
                "radarr_progress",
                f"No acceptable manual releases for {title}; asking Radarr to run an automatic search. See available formats in the completion message.",
            ))
            command = client.search_movie(movie_id)
            return None, command, note

        candidates.sort(
            key=lambda item: (
                release_quality_resolution(item),
                -release_size(item),
                release_title(item).lower(),
            ),
            reverse=True,
        )
        selected = candidates[0]
        quality_name = release_quality_name(selected) or "unknown quality"
        resolution = release_quality_resolution(selected)
        size = release_size(selected)
        self.progress_queue.put((
            "radarr_progress",
            f"Grabbing {title}: {quality_name} {format_size(size) if size else ''}".strip(),
        ))
        log_event(
            "Radarr grabbing release: "
            f"title={title}; quality={quality_name}; resolution={resolution or 'unknown'}; "
            f"size={format_size(size) if size else 'unknown'}; release={release_title(selected)}"
        )
        result = client.grab_release(selected)
        return selected, result, "release grabbed"

    def _confirm_radarr_replace(
        self,
        matches: list[tuple[dict, Path]],
        unmatched_paths: list[Path],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
        allowed_summary: str,
    ) -> None:
        replaceable_matches = [(movie, selected_path) for movie, selected_path in matches if movie_file_id(movie) is not None]
        if not replaceable_matches:
            messagebox.showerror(APP_TITLE, "Radarr found the selected movie/movies, but none have a current movie file to replace.")
            return

        titles = [str(movie.get("title") or "Unknown movie") for movie, _selected_path in replaceable_matches]
        title_text = "\n".join(f"- {title}" for title in titles[:12])
        if len(titles) > 12:
            title_text += f"\n- ...and {len(titles) - 12} more"
        unmatched_text = ""
        if unmatched_paths:
            unmatched_text = f"\n\nSkipped {len(unmatched_paths)} selected row(s) that could not be matched in Radarr."
        confirmed = messagebox.askyesno(
            APP_TITLE,
            f"Change profile and search for {len(replaceable_matches)} Radarr movie/movies?\n\n"
            f"{title_text}\n\n"
            f"New quality profile: {profile_name}\n\n"
            f"Profile rules: {allowed_summary}\n\n"
            "Radarr will update each movie profile, inspect current release results, "
            "and grab a release that fits the selected profile. "
            "The current movie files will NOT be deleted by this app."
            f"{unmatched_text}",
            icon=messagebox.WARNING,
        )
        if not confirmed:
            self.status_var.set("Radarr action cancelled.")
            return

        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=("profile/search", self._radarr_replace_worker, replaceable_matches, profile_id, profile_name, max_resolution),
            daemon=True,
        )
        self.radarr_thread.start()

    def _confirm_radarr_search(
        self,
        matches: list[tuple[dict, Path]],
        unmatched_paths: list[Path],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
        allowed_summary: str,
    ) -> None:
        titles = [str(movie.get("title") or "Unknown movie") for movie, _selected_path in matches]
        title_text = "\n".join(f"- {title}" for title in titles[:12])
        if len(titles) > 12:
            title_text += f"\n- ...and {len(titles) - 12} more"
        unmatched_text = ""
        if unmatched_paths:
            unmatched_text = f"\n\nSkipped {len(unmatched_paths)} selected row(s) that could not be matched in Radarr."
        confirmed = messagebox.askyesno(
            APP_TITLE,
            f"Change profile and retry/search {len(matches)} Radarr movie/movies?\n\n"
            f"{title_text}\n\n"
            f"New quality profile: {profile_name}\n\n"
            f"Profile rules: {allowed_summary}\n\n"
            "Radarr will inspect current release results and grab a release that fits the selected profile. "
            "The current movie files will NOT be deleted by this app."
            f"{unmatched_text}",
        )
        if not confirmed:
            self.status_var.set("Radarr action cancelled.")
            return

        self.radarr_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=("retry/search", self._radarr_search_worker, matches, profile_id, profile_name, max_resolution),
            daemon=True,
        )
        self.radarr_thread.start()

    def _radarr_replace_worker(
        self,
        matches: list[tuple[dict, Path]],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
    ) -> None:
        client = RadarrClient(self.radarr_config)
        monitor_targets: list[dict] = []
        failures: list[str] = []

        for index, (movie, _selected_path) in enumerate(matches, start=1):
            title = str(movie.get("title") or "Movie")
            try:
                movie_id = int(movie["id"])
                old_movie_file_id = movie_file_id(movie)
                if old_movie_file_id is None:
                    raise RuntimeError("Radarr did not report a current movie file id.")

                self.progress_queue.put(("radarr_progress", f"[{index}/{len(matches)}] Updating profile for {title}..."))
                updated_movie = client.update_movie_quality_profile(movie, profile_id)
                confirmed_movie = client.get_movie(movie_id)
                if int(confirmed_movie.get("qualityProfileId") or 0) != profile_id:
                    raise RuntimeError(f"Radarr did not keep the selected profile {profile_name} on {title}.")

                self.progress_queue.put(("radarr_progress", f"[{index}/{len(matches)}] Checking Radarr releases for {title}..."))
                monitor_started_at = time.time()
                selected_release, grab_result, grab_note = self._grab_best_radarr_release(client, movie_id, title, max_resolution)
                quality_name = release_quality_name(selected_release) if selected_release else grab_note

                monitor_targets.append(
                    {
                        "movie_id": movie_id,
                        "old_movie_file_id": old_movie_file_id,
                        "title": updated_movie.get("title") or title,
                        "search_command_id": grab_result.get("id"),
                        "monitor_started_at": monitor_started_at,
                        "monitor_started_utc": utc_now_iso(),
                        "max_resolution": max_resolution,
                        "profile_name": profile_name,
                        "grabbed_quality": quality_name,
                        "grab_note": grab_note,
                        "retry_count": 0,
                    }
                )
            except (KeyError, ValueError, RuntimeError) as exc:
                failures.append(f"{title}: {exc}")

        if failures:
            self.progress_queue.put(("radarr_error", "Some Radarr replacements failed:\n\n" + "\n".join(failures[:10])))
        if not monitor_targets:
            return

        self.progress_queue.put((
            "radarr_progress",
            f"Started {len(monitor_targets)} Radarr replacement request(s) using {profile_name} (max {max_resolution}p). Monitoring downloads...",
        ))
        monitor_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=("download monitor", self._monitor_radarr_downloads, RadarrClient(self.radarr_config), monitor_targets),
            daemon=True,
        )
        monitor_thread.start()

    def _radarr_grab_selected_release_worker(
        self,
        movie: dict,
        release: dict,
        force: bool,
        profile_name: str,
        max_resolution: int,
    ) -> None:
        client = RadarrClient(self.radarr_config)
        title = str(movie.get("title") or "Movie")
        movie_id = int(movie["id"])
        old_movie_file_id = movie_file_id(movie) or 0
        quality_name = release_quality_name(release) or "unknown quality"
        size = release_size(release)
        self.progress_queue.put((
            "radarr_progress",
            f"Grabbing selected release for {title}: {quality_name} {format_size(size) if size else ''}".strip(),
        ))
        monitor_started_at = time.time()
        grab_result = client.grab_release(release, force=force)
        monitor_targets = [
            {
                "movie_id": movie_id,
                "old_movie_file_id": old_movie_file_id,
                "title": title,
                "search_command_id": grab_result.get("id"),
                "monitor_started_at": monitor_started_at,
                "monitor_started_utc": utc_now_iso(),
                "max_resolution": max_resolution,
                "profile_name": profile_name,
                "grabbed_quality": quality_name,
                "grab_note": "manual release selected",
                "retry_count": 0,
            }
        ]
        self.progress_queue.put(("radarr_progress", f"Selected release sent to Radarr for {title}. Monitoring download..."))
        monitor_thread = threading.Thread(
            target=self._radarr_worker_guard,
            args=("manual release monitor", self._monitor_radarr_downloads, RadarrClient(self.radarr_config), monitor_targets),
            daemon=True,
        )
        monitor_thread.start()
    def _wait_for_radarr_download(
        self,
        client: RadarrClient,
        movie_id: int,
        old_movie_file_id: int,
        title: str,
    ) -> dict | None:
        deadline = time.time() + RADARR_DOWNLOAD_MONITOR_SECONDS
        while time.time() < deadline:
            time.sleep(RADARR_DOWNLOAD_POLL_SECONDS)
            try:
                movie = client.get_movie(movie_id)
            except Exception as exc:
                log_exception(f"Radarr monitor poll failed for {title}", exc)
                self.progress_queue.put(("radarr_progress", f"Radarr monitor waiting for {title}: {exc}"))
                continue

            current_file_id = movie_file_id(movie)
            has_file = bool(movie.get("hasFile")) or current_file_id is not None
            if has_file and current_file_id and current_file_id != old_movie_file_id:
                return movie
            self.progress_queue.put(("radarr_progress", f"Monitoring Radarr download for {title}..."))
        return None

    def _retry_radarr_target(self, client: RadarrClient, movie_id: int, target: dict, reason: str) -> bool:
        retry_count = int(target.get("retry_count") or 0)
        if retry_count >= RADARR_DOWNLOAD_RETRY_LIMIT:
            return False
        title = str(target["title"])
        max_resolution = int(target.get("max_resolution") or 0)
        target["retry_count"] = retry_count + 1
        target["monitor_started_at"] = time.time()
        target["monitor_started_utc"] = utc_now_iso()
        self.progress_queue.put((
            "radarr_progress",
            f"Retrying {title} after failed download ({retry_count + 1}/{RADARR_DOWNLOAD_RETRY_LIMIT}): {reason}",
        ))
        log_event(f"Radarr retry: title={title}; movie_id={movie_id}; attempt={retry_count + 1}; reason={reason}")
        try:
            selected_release, grab_result, grab_note = self._grab_best_radarr_release(client, movie_id, title, max_resolution)
        except RuntimeError as exc:
            log_exception(f"Radarr retry failed for {title}", exc)
            self.progress_queue.put(("radarr_progress", f"Retry failed for {title}: {exc}"))
            return False
        target["search_command_id"] = grab_result.get("id")
        target["grab_note"] = grab_note
        target["grabbed_quality"] = release_quality_name(selected_release) if selected_release else grab_note
        return True
    def _monitor_radarr_downloads(self, client: RadarrClient, targets: list[dict]) -> None:
        pending = {int(target["movie_id"]): target for target in targets}
        deadline = time.time() + RADARR_DOWNLOAD_MONITOR_SECONDS
        while pending and time.time() < deadline:
            time.sleep(RADARR_DOWNLOAD_POLL_SECONDS)
            for movie_id in list(pending):
                target = pending[movie_id]
                title = str(target["title"])
                try:
                    movie = client.get_movie(movie_id)
                except Exception as exc:
                    log_exception(f"Radarr monitor poll failed for {title}", exc)
                    self.progress_queue.put(("radarr_progress", f"Radarr monitor waiting for {title}: {exc}"))
                    continue

                current_file_id = movie_file_id(movie)
                has_file = bool(movie.get("hasFile")) or current_file_id is not None
                if has_file and current_file_id and current_file_id != int(target["old_movie_file_id"]):
                    file_path = movie_file_path(movie)
                    date_added = movie_file_date_added(movie)
                    max_resolution = int(target.get("max_resolution") or 0)
                    current_quality = movie.get("movieFile", {}).get("quality") if isinstance(movie.get("movieFile"), dict) else {}
                    quality_detail = current_quality.get("quality") if isinstance(current_quality, dict) else {}
                    quality_name = str(quality_detail.get("name") or "") if isinstance(quality_detail, dict) else ""
                    try:
                        quality_resolution = int(quality_detail.get("resolution") or 0) if isinstance(quality_detail, dict) else 0
                    except (TypeError, ValueError):
                        quality_resolution = 0
                    detail = f"\n\nFile:\n{file_path}" if file_path else ""
                    if date_added:
                        detail += f"\n\nRadarr imported:\n{date_added}"
                    if max_resolution and quality_resolution > max_resolution:
                        detail += f"\n\nWARNING:\nImported {quality_name} above selected max {max_resolution}p."
                    log_event(
                        "Radarr import complete: "
                        f"title={title}; movie_id={movie_id}; old_file_id={target['old_movie_file_id']}; "
                        f"new_file_id={current_file_id}; date_added={date_added or 'unknown'}; "
                        f"quality={quality_name or 'unknown'}; max_resolution={max_resolution or 'unknown'}; path={file_path or 'unknown'}"
                    )
                    self.progress_queue.put(("radarr_download_complete", (f"Radarr download/import complete for {title}.{detail}", file_path)))
                    del pending[movie_id]
                    continue

                status_message, terminal = self._radarr_live_status(client, movie_id, target, movie)
                log_event(f"Radarr status: {status_message}")
                if terminal:
                    if status_message.startswith("Failed:") and self._retry_radarr_target(client, movie_id, target, status_message):
                        continue
                    self.progress_queue.put(("radarr_done", status_message))
                    del pending[movie_id]
                else:
                    self.progress_queue.put(("radarr_progress", status_message))

            if pending:
                self.progress_queue.put(("radarr_progress", f"Monitoring {len(pending)} Radarr replacement download(s)..."))

        if pending:
            remaining = ", ".join(str(target["title"]) for target in list(pending.values())[:8])
            if len(pending) > 8:
                remaining += f", and {len(pending) - 8} more"
            self.progress_queue.put((
                "radarr_done",
                "These replacement download(s) did not complete while monitoring was active:\n\n"
                f"{remaining}\n\n"
                "To retry, select them and use Radarr: Retry/Search Movie. "
                "If Radarr picked a bad release, blocklist/remove it in Radarr first so the same release is not grabbed again.",
            ))
        else:
            self.progress_queue.put(("radarr_done", "All monitored Radarr replacement downloads completed."))

    def _radarr_search_worker(
        self,
        matches: list[tuple[dict, Path]],
        profile_id: int,
        profile_name: str,
        max_resolution: int,
    ) -> None:
        client = RadarrClient(self.radarr_config)
        queued = 0
        failures: list[str] = []
        monitor_targets: list[dict] = []
        for index, (movie, _selected_path) in enumerate(matches, start=1):
            title = str(movie.get("title") or "Movie")
            try:
                movie_id = int(movie["id"])
                self.progress_queue.put(("radarr_progress", f"[{index}/{len(matches)}] Updating profile for {title}..."))
                updated_movie = client.update_movie_quality_profile(movie, profile_id)
                confirmed_movie = client.get_movie(movie_id)
                if int(confirmed_movie.get("qualityProfileId") or 0) != profile_id:
                    raise RuntimeError(f"Radarr did not keep the selected profile {profile_name} on {title}.")

                self.progress_queue.put(("radarr_progress", f"[{index}/{len(matches)}] Checking Radarr releases for {title}..."))
                monitor_started_at = time.time()
                selected_release, grab_result, grab_note = self._grab_best_radarr_release(client, movie_id, title, max_resolution)
                quality_name = release_quality_name(selected_release) if selected_release else grab_note
                monitor_targets.append(
                    {
                        "movie_id": movie_id,
                        "old_movie_file_id": movie_file_id(movie) or 0,
                        "title": updated_movie.get("title") or title,
                        "search_command_id": grab_result.get("id"),
                        "monitor_started_at": monitor_started_at,
                        "monitor_started_utc": utc_now_iso(),
                        "max_resolution": max_resolution,
                        "profile_name": profile_name,
                        "grabbed_quality": quality_name,
                        "grab_note": grab_note,
                        "retry_count": 0,
                    }
                )
                queued += 1
            except (KeyError, ValueError, RuntimeError) as exc:
                failures.append(f"{title}: {exc}")
        if failures:
            self.progress_queue.put(("radarr_error", "Some Radarr searches failed:\n\n" + "\n".join(failures[:10])))
        if queued:
            self.progress_queue.put(("radarr_progress", f"Started {queued} Radarr retry/search request(s) using {profile_name} (max {max_resolution}p). Monitoring status..."))
            monitor_thread = threading.Thread(
                target=self._radarr_worker_guard,
                args=("retry/search monitor", self._monitor_radarr_downloads, RadarrClient(self.radarr_config), monitor_targets),
                daemon=True,
            )
            monitor_thread.start()

    def export_csv(self) -> None:
        if not self.current_rows:
            return

        default_name = "cinema-library-report.csv"
        output_path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        if not output_path:
            return

        try:
            with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(["Name", "Kind", "Format", "Extension", "Bytes", "Size", "Files", "Folders", "Skipped", "Path"])
                for row in self.current_rows:
                    writer.writerow(
                        [
                            row.name,
                            row.kind,
                            row.quality or "",
                            row.extension or "",
                            row.size,
                            format_size(row.size),
                            row.file_count,
                            row.folder_count,
                            row.skipped_count,
                            row.path,
                        ]
                    )
            messagebox.showinfo(APP_TITLE, f"Report exported to:\n{output_path}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not export CSV:\n{exc}")


if __name__ == "__main__":
    threading.excepthook = log_thread_exception
    app = DriveSpaceAnalyzer()
    app.mainloop()

























