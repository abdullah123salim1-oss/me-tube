"""Probe a single video URL and list its downloadable formats.

Read-only companion to the Browse dialog: nothing is downloaded, queued or
persisted. The rows power the UI's format picker (thumbnail + Music/Video
lists like the one-shot downloader sites), and each row carries an exact
yt-dlp format selector that ``/add`` accepts as ``ytdl_format``.

Format rows are data derived from the extractor's info dict; titles,
thumbnails and filenames shown from them are untrusted display values.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Optional

import yt_dlp
import yt_dlp.networking.impersonate

from dl_formats import merge_ytdl_option_layers
from url_guard import validate_url, download_proxies

log = logging.getLogger("format_probe")

VIDEO_MSG = "This URL is a playlist or channel, not a single video."


def _impersonate_opt(ytdl_options: dict) -> dict:
    opts = dict(ytdl_options)
    if "impersonate" in opts:
        opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(
            opts["impersonate"]
        )
    return opts


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _filesize(f: dict) -> Optional[int]:
    size = _num(f.get("filesize") or f.get("filesize_approx"))
    return int(size) if size and size > 0 else None


def _has_video(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none")


def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def _is_storyboard(f: dict) -> bool:
    fmt_id = str(f.get("format_id") or "")
    return (
        f.get("ext") == "mhtml"
        or str(f.get("format_note") or "").lower() == "storyboard"
        or fmt_id.startswith("sb")
    )


def _audio_rows(formats: Optional[list]) -> list[dict]:
    """One row per source bitrate, lowest first (the downloader-site order)."""
    best_by_label: dict[str, dict] = {}
    for f in formats or []:
        if not isinstance(f, dict) or not f.get("format_id") or _is_storyboard(f):
            continue
        if _has_video(f) or not _has_audio(f):
            continue
        abr = _num(f.get("abr") or f.get("tbr"))
        label = f"{int(round(abr))}KBPS" if abr else "LOW"
        row = {
            "id": str(f["format_id"]),
            "kind": "audio",
            # MeTube's audio pipeline converts the picked stream to MP3 with
            # tags/cover, so the picker advertises the file the user gets.
            "badge": "MP3",
            "label": label,
            "filesize": _filesize(f),
            # A single stream needs no ffmpeg merge: quick everywhere.
            "fast": True,
            # Exact source stream; conversion happens after, via postprocessors.
            "selector": str(f["format_id"]),
            "ext": str(f.get("ext") or "mp3"),
            "abr": abr,
        }
        prev = best_by_label.get(label)
        if prev is None or (row["filesize"] or 0) > (prev["filesize"] or 0):
            best_by_label[label] = row
    rows = list(best_by_label.values())
    rows.sort(key=lambda r: (r["abr"] is not None, r["abr"] or 0))
    return rows[:8]


def _video_rows(formats: Optional[list]) -> list[dict]:
    """One row per resolution, best stream per resolution, highest first."""
    best_by_label: dict[str, dict] = {}
    for f in formats or []:
        if not isinstance(f, dict) or not f.get("format_id") or _is_storyboard(f):
            continue
        if not _has_video(f):
            continue
        height = _num(f.get("height"))
        label = f"{int(height)}P" if height else str(f.get("format_note") or "HD").upper()
        ext = str(f.get("ext") or "mp4").lower()
        progressive = _has_audio(f)
        fmt_id = str(f["format_id"])
        row = {
            "id": fmt_id,
            "kind": "video",
            "badge": "MP4" if ext in ("mp4", "m4v", "mov") else ext.upper(),
            "label": label,
            "filesize": _filesize(f),
            # Progressive files come with audio already muxed in: no merge step.
            "fast": progressive,
            # A video-only stream is merged with the site's best audio at
            # download time, with the raw stream as the fallback.
            "selector": fmt_id if progressive else f"{fmt_id}+bestaudio/{fmt_id}",
            "ext": ext,
            "height": int(height) if height else None,
        }
        rank = (
            1 if progressive else 0,
            1 if ext in ("mp4", "m4v", "mov") else 0,
            _num(f.get("tbr")) or 0,
            row["filesize"] or 0,
        )
        prev = best_by_label.get(label)
        if prev is None or rank > prev["_rank"]:
            row["_rank"] = rank
            best_by_label[label] = row
    rows = [dict(r, _rank=None) for r in best_by_label.values()]
    rows.sort(key=lambda r: (r["height"] is not None, r["height"] or 0), reverse=True)
    return rows[:12]


def summarize_formats(info: dict) -> dict:
    """Build the picker payload from a yt-dlp info dict (pure, no I/O)."""
    duration = _num(info.get("duration"))
    thumbnail = info.get("thumbnail")
    if not thumbnail:
        thumbs = info.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs and isinstance(thumbs[-1], dict):
            thumbnail = thumbs[-1].get("url")
    return {
        "title": info.get("title") or info.get("webpage_url") or "",
        "thumbnail": thumbnail or None,
        "duration": duration,
        "music": _audio_rows(info.get("formats")),
        "video": _video_rows(info.get("formats")),
    }


def _extract_single(config, url: str, extra_opts: dict) -> Optional[dict]:
    params: dict[str, Any] = {
        "quiet": not logging.getLogger().isEnabledFor(logging.DEBUG),
        "verbose": logging.getLogger().isEnabledFor(logging.DEBUG),
        "no_color": True,
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "allow_playlist_files": False,
        **config.YTDL_OPTIONS,
        **(extra_opts or {}),
    }
    params = _impersonate_opt(params)
    with yt_dlp.YoutubeDL(params=params) as ydl:
        return ydl.extract_info(url, download=False)


async def probe_video_formats(
    config,
    url: str,
    ytdl_options_presets: Optional[list[str]],
    ytdl_options_overrides: Optional[dict[str, Any]],
) -> dict:
    """Full-format probe of a single video URL; queues nothing.

    Returns ``{'status': 'ok', ...formats}`` for a video, ``{'status':
    'playlist', ...}`` for a playlist/channel URL (the UI falls back to the
    regular Download / Browse flows there), or ``{'status': 'error', 'msg'}``.
    """
    extra = merge_ytdl_option_layers(
        ytdl_options_presets, ytdl_options_overrides, config.YTDL_OPTIONS_PRESETS
    )
    loop = asyncio.get_running_loop()
    # SSRF guard, same as /add and /browse: block non-http(s) schemes and
    # internal/metadata hosts before yt-dlp fetches anything.
    proxies = download_proxies({**config.YTDL_OPTIONS, **extra})
    url_error = await loop.run_in_executor(
        None,
        partial(
            validate_url,
            url,
            allow_private=getattr(config, "ALLOW_PRIVATE_ADDRESSES", False),
            proxies=proxies,
        ),
    )
    if url_error is not None:
        log.warning('Rejected probe URL "%s": %s', url, url_error)
        return {"status": "error", "msg": url_error}
    try:
        info = await loop.run_in_executor(None, partial(_extract_single, config, url, extra))
    except yt_dlp.utils.YoutubeDLError as exc:
        return {"status": "error", "msg": str(exc)}
    if not info:
        return {"status": "error", "msg": "Could not resolve URL"}
    if (info.get("_type") or "video") in ("playlist", "channel"):
        return {"status": "playlist", "title": info.get("title") or url}
    summary = summarize_formats(info)
    if not summary["music"] and not summary["video"]:
        return {"status": "error", "msg": "No downloadable formats found"}
    return {
        "status": "ok",
        "url": info.get("webpage_url") or url,
        **summary,
    }
