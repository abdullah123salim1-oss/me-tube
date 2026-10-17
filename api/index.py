"""MeTube backend for Vercel serverless + Supabase.

This is a cloud adaptation of MeTube (github.com/alexta69/metube): the same
UI-facing REST API and state model, reworked so that every piece of state
lives in Supabase (Postgres + Storage) and downloads run inside the request
that starts them (Vercel has no long-lived worker processes).

How work happens on Vercel:
  * POST /add, /retry, /start only record work in Supabase (fast responses).
  * POST /tick is the worker: the browser UI fires it whenever the queue has
    work, and it runs the yt-dlp downloads inline (bounded by the platform's
    function time limit), streaming progress into Supabase.
  * GET /snapshot is polled by the UI shim and diffs the state into the same
    events the stock MeTube Socket.IO layer emitted (added/updated/completed/
    canceled/cleared).
  * Finished files are uploaded to Supabase Storage; GET /file/... resolves a
    finished item's path to a signed Storage URL.

Feature note: Vercel functions have no ffmpeg, so merged/converted outputs
(bestvideo+bestaudio merges, mp3/wav/flac conversion, chapter splitting,
clipping, sponsor cutting) are not offered. The format catalog below mirrors
MeTube's shape minus those options.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from typing import Any, Optional
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
log = logging.getLogger("metube")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = "metube-files"
TMP_ROOT = "/tmp/metube"

# Mirrors MeTube's DOWNLOAD_QUEUE / UI contract, trimmed for no-ffmpeg hosts.
DOWNLOAD_TIME_BUDGET = 200          # seconds of yt-dlp work per /tick call
TICK_LEASE_SECONDS = 240            # a lease older than this is considered stale
STALE_ACTIVE_SECONDS = 360          # an active row quiet this long is marked failed
MAX_PLAYLIST_ITEMS = 25
MAX_COOKIES_BYTES = 1_000_000

_http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))


# ---------------------------------------------------------------------------
# Format catalog (same shape as MeTube's app/format_catalog.py, minus options
# that require ffmpeg) + the yt-dlp selector mapping (dl_formats.get_format).
# ---------------------------------------------------------------------------

import copy  # noqa: E402


def _choice(default: str, options: list) -> dict:
    return {"default": default, "options": options}


def _options(*pairs) -> list:
    return [{"id": id_, "text": text} for id_, text in pairs]


_VIDEO_QUALITY = _choice("best", _options(
    ("best", "Best"),
    ("1080", "1080p"),
    ("720", "720p"),
    ("480", "480p"),
    ("360", "360p"),
    ("240", "240p"),
    ("worst", "Worst"),
))


def _video_format(id_: str, text: str) -> dict:
    return {"id": id_, "text": text, "quality": _VIDEO_QUALITY}


def _audio_format(id_: str, text: str, *bitrates: str, tags: bool = True) -> dict:
    quality = _choice("best", _options(("best", "Best"), *((b, f"{b} kbps") for b in bitrates)))
    return {"id": id_, "text": text, "quality": quality, "tags": tags}


CATALOG: dict = {
    "download_type": _choice("video", [
        {
            "id": "video",
            "text": "Video",
            "codec": _choice("auto", _options(
                ("auto", "Auto"),
                ("h264", "H.264"),
                ("h265", "H.265 (HEVC)"),
                ("av1", "AV1"),
                ("vp9", "VP9"),
            )),
            "format": _choice("any", [
                _video_format("any", "Auto"),
                _video_format("mp4", "MP4"),
            ]),
        },
        {
            "id": "audio",
            "text": "Audio",
            "format": _choice("m4a", [
                _audio_format("auto", "Auto"),
                _audio_format("m4a", "M4A", "192", "128"),
                _audio_format("opus", "OPUS"),
            ]),
            "audio_tags": _choice("with_cover", _options(
                ("with_cover", "With cover"),
                ("no_cover", "No cover"),
                ("none", "None"),
            )),
        },
        {
            "id": "captions",
            "text": "Captions",
            "format": _choice("srt", _options(
                ("srt", "SRT"),
                ("vtt", "VTT"),
            )),
            "subtitle_mode": _choice("prefer_manual", _options(
                ("prefer_manual", "Prefer Manual"),
                ("prefer_auto", "Prefer Auto"),
                ("manual_only", "Manual Only"),
                ("auto_only", "Auto Only"),
            )),
            "subtitle_language": _choice("en", _options(
                ("en", "English"), ("ar", "Arabic"), ("bn", "Bengali"),
                ("bg", "Bulgarian"), ("ca", "Catalan"), ("cs", "Czech"),
                ("da", "Danish"), ("nl", "Dutch"), ("es", "Spanish"),
                ("et", "Estonian"), ("fi", "Finnish"), ("fr", "French"),
                ("de", "German"), ("el", "Greek"), ("he", "Hebrew"),
                ("hi", "Hindi"), ("hu", "Hungarian"), ("id", "Indonesian"),
                ("it", "Italian"), ("lt", "Lithuanian"), ("lv", "Latvian"),
                ("ms", "Malay"), ("no", "Norwegian"), ("pl", "Polish"),
                ("pt", "Portuguese"), ("pt-BR", "Portuguese (Brazil)"),
                ("ro", "Romanian"), ("ru", "Russian"), ("sk", "Slovak"),
                ("sl", "Slovenian"), ("sr", "Serbian"), ("sv", "Swedish"),
                ("ta", "Tamil"), ("te", "Telugu"), ("th", "Thai"),
                ("tr", "Turkish"), ("uk", "Ukrainian"), ("ur", "Urdu"),
                ("vi", "Vietnamese"), ("ja", "Japanese"), ("ko", "Korean"),
                ("zh-Hans", "Chinese (Simplified)"),
                ("zh-Hant", "Chinese (Traditional)"),
            )),
        },
        {
            "id": "thumbnail",
            "text": "Thumbnail",
            "format": _choice("jpg", _options(("jpg", "JPG"))),
        },
    ]),
}

SUBTITLE_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,34}$")


class InvalidOption(ValueError):
    pass


def public_catalog() -> dict:
    return copy.deepcopy(CATALOG)


def option_ids(choice: dict) -> list:
    return [o["id"] for o in choice["options"]]


def _find(choice: dict, id_: str):
    return next((o for o in choice["options"] if o["id"] == id_), None)


def download_type(type_id: str) -> dict:
    found = _find(CATALOG["download_type"], type_id)
    if found is None:
        raise KeyError(type_id)
    return found


def format_option(type_id: str, format_id: str):
    return _find(download_type(type_id)["format"], format_id)


def choice_default(type_id: str, field: str) -> str:
    return download_type(type_id)[field]["default"]


def _check(field: str, value: str, choice: dict, context: str = "") -> None:
    if value not in option_ids(choice):
        raise InvalidOption(f"{field} must be one of {sorted(option_ids(choice))}{context}")


def resolve_selection(download_type_id: str, codec: str, format_id: str, quality: str):
    _check("download_type", download_type_id, CATALOG["download_type"])
    type_entry = download_type(download_type_id)
    if "codec" in type_entry:
        _check("codec", codec, type_entry["codec"])
    else:
        codec = "auto"
    _check("format", format_id, type_entry["format"], f" for {download_type_id}")
    format_entry = format_option(download_type_id, format_id)
    if "quality" in format_entry:
        _check("quality", quality, format_entry["quality"], f" for format {format_id}")
    else:
        quality = "best"
    return codec, format_id, quality


def check_subtitle_mode(subtitle_mode: str) -> None:
    _check("subtitle_mode", subtitle_mode, download_type("captions")["subtitle_mode"])


def check_subtitle_language(subtitle_language: str) -> None:
    if not SUBTITLE_LANGUAGE_RE.fullmatch(subtitle_language):
        raise InvalidOption("subtitle_language must match pattern [A-Za-z0-9-] and be at most 35 characters")


def check_audio_tags(audio_tags: str) -> None:
    _check("audio_tags", audio_tags, download_type("audio")["audio_tags"])


CODEC_FILTER_MAP = {
    "h264": "[vcodec~='^(h264|avc)']",
    "h265": "[vcodec~='^(h265|hevc)']",
    "av1": "[vcodec~='^av0?1']",
    "vp9": "[vcodec~='^vp0?9']",
}


def get_format(download_type_id: str, codec: str, format_id: str, quality: str) -> str:
    """yt-dlp format selector. Progressive (single-file) streams only: Vercel
    functions ship no ffmpeg, so merged video+audio cannot be joined."""
    download_type_id = (download_type_id or "video").strip().lower()
    format_id = (format_id or "any").strip().lower()
    codec = (codec or "auto").strip().lower()
    quality = (quality or "best").strip().lower()

    if download_type_id == "thumbnail":
        return "best"
    if download_type_id == "captions":
        return "best"
    if download_type_id == "audio":
        if format_id == "m4a":
            return "bestaudio[ext=m4a]/bestaudio/best"
        if format_id == "opus":
            return "bestaudio[ext=opus]/bestaudio[ext=webm]/bestaudio/best"
        return "bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best"

    if download_type_id == "video":
        vfmt = "[ext=mp4]" if format_id == "mp4" else ""
        if quality == "worst":
            vres = ""
            prefix = "worst"
        else:
            vres = "" if quality == "best" else f"[height<={quality}]"
            prefix = "best"
        codec_filter = CODEC_FILTER_MAP.get(codec, "")
        return f"{prefix}{codec_filter}{vres}{vfmt}/{prefix}{codec_filter}{vres}/{prefix}"

    raise ValueError(f"Unknown download_type {download_type_id}")


# ---------------------------------------------------------------------------
# Supabase REST store
# ---------------------------------------------------------------------------

class StoreError(RuntimeError):
    pass


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if extra:
        h.update(extra)
    return h


def _check_resp(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        raise StoreError(f"Supabase {resp.request.method} {resp.request.url.path} -> {resp.status_code}: {resp.text[:300]}")
    return resp


def _rest(method: str, path: str, params: Optional[dict] = None, json_body: Any = None,
          headers: Optional[dict] = None, content: Optional[bytes] = None) -> httpx.Response:
    url = f"{SUPABASE_URL}{path}"
    return _check_resp(_http.request(method, url, params=params, json=json_body,
                                headers=_headers(headers), content=content))


# -- items (queue + done share one table; PK is (state, key)) ----------------

def fetch_items(state: str) -> list:
    resp = _rest("GET", "/rest/v1/metube_items",
                 params={"select": "*", "state": f"eq.{state}", "order": "created_at.asc"})
    return resp.json()


def get_item(state: str, key: str) -> Optional[dict]:
    resp = _rest("GET", "/rest/v1/metube_items",
                 params={"select": "*", "state": f"eq.{state}", "key": f"eq.{key}"})
    rows = resp.json()
    return rows[0] if rows else None


def upsert_item(state: str, key: str, info: dict, **fields) -> dict:
    row = {"state": state, "key": key, "info": info, "updated_at": _now_iso(), **fields}
    resp = _rest("POST", "/rest/v1/metube_items",
                 headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                 json_body=row)
    rows = resp.json()
    return rows[0] if rows else row


def patch_item(state: str, key: str, **fields) -> None:
    fields["updated_at"] = _now_iso()
    _rest("PATCH", "/rest/v1/metube_items",
          params={"state": f"eq.{state}", "key": f"eq.{key}"}, json_body=fields)


def delete_item(state: str, key: str) -> None:
    _rest("DELETE", "/rest/v1/metube_items",
          params={"state": f"eq.{state}", "key": f"eq.{key}"})


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# -- subscriptions ----------------------------------------------------------

def fetch_subs() -> list:
    return _rest("GET", "/rest/v1/metube_subscriptions",
                 params={"select": "*", "order": "created_at.asc"}).json()


def upsert_sub(sub_id: str, data: dict, seen_ids: list) -> None:
    _rest("POST", "/rest/v1/metube_subscriptions",
          headers={"Prefer": "resolution=merge-duplicates"},
          json_body={"id": sub_id, "data": data, "seen_ids": seen_ids[-500:], "updated_at": _now_iso()})


def delete_sub(sub_id: str) -> None:
    _rest("DELETE", "/rest/v1/metube_subscriptions", params={"id": f"eq.{sub_id}"})


# -- config (cookies, leases) ----------------------------------------------

def get_config(name: str) -> Optional[str]:
    rows = _rest("GET", "/rest/v1/metube_config",
                 params={"select": "value", "name": f"eq.{name}"}).json()
    return rows[0]["value"] if rows else None


def set_config(name: str, value: str) -> None:
    _rest("POST", "/rest/v1/metube_config",
          headers={"Prefer": "resolution=merge-duplicates"},
          json_body={"name": name, "value": value})


# -- storage ----------------------------------------------------------------

def storage_upload(path: str, data: bytes, content_type: str) -> None:
    _rest("POST", f"/storage/v1/object/{BUCKET}/{path}",
          headers={"Content-Type": content_type or "application/octet-stream", "x-upsert": "true"},
          content=data)


def storage_delete(path: str) -> None:
    _rest("DELETE", f"/storage/v1/object/{BUCKET}", json_body={"prefixes": [path]})


def storage_sign(path: str, expires: int = 3600) -> str:
    resp = _rest("POST", f"/storage/v1/object/sign/{BUCKET}/{path}",
                 json_body={"expiresIn": expires})
    signed = resp.json().get("signedURL") or resp.json().get("data", {}).get("signedURL")
    if not signed:
        raise StoreError(f"no signedURL for {path}")
    return f"{SUPABASE_URL}/storage/v1{signed}"


# ---------------------------------------------------------------------------
# Options parsing (same request contract as MeTube's parse_download_options)
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    pass


def parse_options(post: dict) -> dict:
    url = str(post.get("url") or "").strip()
    download_type_id = str(post.get("download_type") or "video").strip().lower()
    quality = str(post.get("quality") or "best").strip().lower()
    codec = str(post.get("codec") or choice_default("video", "codec")).strip().lower()
    if not url:
        raise BadRequest("missing 'url'")
    try:
        type_entry = download_type(download_type_id)
    except KeyError as exc:
        raise BadRequest(f"unknown download_type '{download_type_id}'") from exc
    format_id = str(post.get("format") or type_entry["format"]["default"]).strip().lower()

    audio_tags = str(post.get("audio_tags") or choice_default("audio", "audio_tags")).strip().lower()
    subtitle_language = str(post.get("subtitle_language") or choice_default("captions", "subtitle_language")).strip()
    subtitle_mode = str(post.get("subtitle_mode") or choice_default("captions", "subtitle_mode")).strip()

    try:
        check_audio_tags(audio_tags)
        check_subtitle_language(subtitle_language)
        check_subtitle_mode(subtitle_mode)
        codec, format_id, quality = resolve_selection(download_type_id, codec, format_id, quality)
    except InvalidOption as exc:
        raise BadRequest(str(exc)) from exc

    try:
        playlist_item_limit = int(post.get("playlist_item_limit") or 0)
    except (TypeError, ValueError) as exc:
        raise BadRequest("playlist_item_limit must be an integer") from exc

    clip_start = _opt_float(post.get("clip_start"))
    clip_end = _opt_float(post.get("clip_end"))

    return {
        "url": url,
        "download_type": download_type_id,
        "codec": codec,
        "format": format_id,
        "quality": quality,
        "folder": str(post.get("folder") or "").strip().strip("/"),
        "custom_name_prefix": str(post.get("custom_name_prefix") or ""),
        "playlist_item_limit": max(0, min(playlist_item_limit, MAX_PLAYLIST_ITEMS)),
        "auto_start": True if post.get("auto_start") is None else bool(post.get("auto_start")),
        "split_by_chapters": bool(post.get("split_by_chapters")),
        "sponsorblock": bool(post.get("sponsorblock")),
        "audio_tags": audio_tags,
        "subtitle_language": subtitle_language,
        "subtitle_mode": subtitle_mode,
        "clip_start": clip_start,
        "clip_end": clip_end,
    }


def _opt_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise BadRequest("clip values must be numbers") from exc


FFMPEG_FEATURE_MSG = (
    "Chapter splitting and clip trimming need ffmpeg, which this Vercel deployment does not include."
)


def unsupported_features(o: dict) -> Optional[str]:
    if o.get("split_by_chapters") or o.get("clip_start") is not None or o.get("clip_end") is not None:
        return FFMPEG_FEATURE_MSG
    return None


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _ydl_base() -> dict:
    opts = {
        "quiet": True,
        "no_color": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 2,
        "nocheckcertificate": False,
    }
    cookies = get_config("cookies")
    if cookies:
        path = os.path.join(TMP_ROOT, "cookies.txt")
        os.makedirs(TMP_ROOT, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cookies)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        opts["cookiefile"] = path
    return opts


def extract_entries(url: str, playlist_item_limit: int) -> list:
    """Flat-probe a URL: single video -> one entry; playlist/channel -> entries."""
    import yt_dlp

    opts = _ydl_base()
    opts.update({
        "extract_flat": "in_playlist",
        "skip_download": True,
    })
    if playlist_item_limit:
        opts["playlistend"] = playlist_item_limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("yt-dlp returned nothing for this URL")
    entries = info.get("entries")
    if entries is None:
        return [{"id": info.get("id"), "url": info.get("webpage_url") or url,
                 "title": info.get("title"), "duration": info.get("duration")}]
    out = []
    for e in entries:
        if not e:
            continue
        out.append({
            "id": e.get("id"),
            "url": e.get("webpage_url") or e.get("url") or url,
            "title": e.get("title"),
            "duration": e.get("duration"),
        })
        if playlist_item_limit and len(out) >= playlist_item_limit:
            break
    return out


class Cancelled(Exception):
    pass


class TimeBudget(Exception):
    pass


def build_info(o: dict, entry: dict, status: str, error: Optional[str] = None) -> dict:
    title = entry.get("title") or entry.get("url") or o["url"]
    custom_prefix = o.get("custom_name_prefix") or ""
    entry_id = str(entry.get("id") or hashlib.sha1(o["url"].encode()).hexdigest()[:11])
    info = {
        "id": entry_id if not custom_prefix else f"{custom_prefix}.{entry_id}",
        "title": title if not custom_prefix else f"{custom_prefix}.{title}",
        "url": entry.get("url") or o["url"],
        "download_type": o["download_type"],
        "codec": o["codec"],
        "quality": o["quality"],
        "format": o["format"],
        "folder": o["folder"],
        "custom_name_prefix": o["custom_name_prefix"],
        "playlist_item_limit": o["playlist_item_limit"],
        "split_by_chapters": False,
        "sponsorblock": o["sponsorblock"],
        "audio_tags": o["audio_tags"],
        "chapter_template": "",
        "subtitle_language": o["subtitle_language"],
        "subtitle_mode": o["subtitle_mode"],
        "ytdl_options_presets": [],
        "ytdl_options_overrides": {},
        "clip_start": None,
        "clip_end": None,
        "live_status": None,
        "live_release_timestamp": None,
        "status": status,
        "msg": None,
        "percent": None,
        "speed": None,
        "eta": None,
        "filename": None,
        "size": None,
        "error": error,
        "timestamp": time.time_ns(),
    }
    return info


def _key_hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def run_download(key: str, info: dict, deadline: float) -> str:
    """Run yt-dlp for one item. Returns 'finished', 'error' or 'canceled'.

    Progress is written into the queue row (about once a second) so the UI's
    snapshot polling shows it live; the caller moves the row to done.
    """
    import yt_dlp

    outdir = os.path.join(TMP_ROOT, "dl", _key_hash(key))
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)

    status = {"value": "preparing"}
    last_write = [0.0]
    last_cancel_check = [0.0]

    def progress_hook(st):
        now = time.monotonic()
        if now - last_cancel_check[0] > 3.0:
            last_cancel_check[0] = now
            try:
                row = get_item("queue", key)
                if row and row.get("cancel_requested"):
                    raise Cancelled()
            except StoreError:
                pass
        if now >= deadline:
            raise TimeBudget()
        if st.get("status") == "downloading":
            if now - last_write[0] < 1.0:
                return
            last_write[0] = now
            total = st.get("total_bytes") or st.get("total_bytes_estimate")
            done = st.get("downloaded_bytes")
            info["status"] = "downloading"
            info["percent"] = (done / total * 100) if (total and done) else None
            info["speed"] = st.get("speed")
            info["eta"] = st.get("eta")
            try:
                upsert_item("queue", key, info)
            except StoreError as exc:
                log.warning("progress write failed: %s", exc)
        elif st.get("status") == "finished":
            last_write[0] = now
            info["status"] = "postprocessing"
            info["percent"] = 100.0
            info["speed"] = None
            info["eta"] = None
            try:
                upsert_item("queue", key, info)
            except StoreError:
                pass

    opts = _ydl_base()
    opts.update({
        "format": get_format(info["download_type"], info.get("codec", "auto"),
                             info["format"], info["quality"]),
        "outtmpl": os.path.join(outdir, "%(title).200B.%(ext)s"),
        "progress_hooks": [progress_hook],
        "noplaylist": True,
    })

    if info["download_type"] == "captions":
        lang = info.get("subtitle_language") or "en"
        mode = info.get("subtitle_mode") or "prefer_manual"
        opts.update({
            "skip_download": True,
            "writesubtitles": mode in ("prefer_manual", "manual_only", "prefer_auto"),
            "writeautomaticsub": mode in ("prefer_auto", "auto_only", "prefer_manual"),
            "subtitleslangs": [lang, f"{lang}.*"],
            "subtitlesformat": "srt/vtt/best",
        })
    elif info["download_type"] == "thumbnail":
        opts.update({
            "skip_download": True,
            "writethumbnail": True,
        })

    class _Logger:
        def debug(self, msg):
            log.debug("%s", msg)

        def info(self, msg):
            log.info("%s", msg)

        def warning(self, msg):
            log.warning("%s", msg)

        def error(self, msg):
            log.error("%s", msg)

    opts["logger"] = _Logger()

    info["status"] = "preparing"
    upsert_item("queue", key, info)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([info["url"]])
    except Cancelled:
        shutil.rmtree(outdir, ignore_errors=True)
        return "canceled"
    except TimeBudget:
        shutil.rmtree(outdir, ignore_errors=True)
        info["status"] = "error"
        info["msg"] = "Stopped: the download exceeded the Vercel function time limit (5 minutes). Try a lower quality or audio."
        info["error"] = info["msg"]
        return "error"
    except Exception as exc:  # noqa: BLE001 - surface any yt-dlp failure to the row
        shutil.rmtree(outdir, ignore_errors=True)
        msg = str(exc).strip() or exc.__class__.__name__
        info["status"] = "error"
        info["msg"] = msg[:2000]
        info["error"] = info["msg"]
        return "error"

    produced = []
    for root, _dirs, files in os.walk(outdir):
        for name in files:
            p = os.path.join(root, name)
            try:
                produced.append((p, os.path.getsize(p)))
            except OSError:
                pass
    if not produced:
        info["status"] = "error"
        info["msg"] = "yt-dlp finished but produced no file"
        info["error"] = info["msg"]
        return "error"

    produced.sort(key=lambda t: t[1], reverse=True)
    main_path, main_size = produced[0]
    basename = os.path.basename(main_path)
    info["filename"] = basename
    info["size"] = main_size
    info["percent"] = 100.0
    info["speed"] = None
    info["eta"] = None

    # Upload to Supabase Storage (50MB file limit on the free plan).
    rel_parts = [info["folder"], basename] if info.get("folder") else [basename]
    info["file_path"] = "/".join(p for p in rel_parts if p)
    storage_key = f"{_key_hash(key)}/{basename}"
    try:
        with open(main_path, "rb") as fh:
            data = fh.read()
        storage_upload(storage_key, data, mimetypes.guess_type(basename)[0] or "application/octet-stream")
        info["storage_path"] = storage_key
    except Exception as exc:  # noqa: BLE001
        log.warning("storage upload failed for %s: %s", key, exc)
        info["storage_path"] = None
        info["msg"] = (
            "File is too large for Supabase Storage on the free plan (50MB limit); "
            "use Retry to download it again directly."
        )
    shutil.rmtree(outdir, ignore_errors=True)
    info["status"] = "finished"
    return "finished"


def move_to_done(key: str, info: dict) -> None:
    fields = {}
    if info.get("file_path"):
        fields["file_path"] = info["file_path"]
    if info.get("storage_path"):
        fields["storage_path"] = info["storage_path"]
    upsert_item("done", key, info, **fields)
    delete_item("queue", key)


# ---------------------------------------------------------------------------
# Worker: process queued items inline (the '/tick' job)
# ---------------------------------------------------------------------------

def process_queue(time_budget: float = DOWNLOAD_TIME_BUDGET) -> dict:
    started = time.monotonic()
    deadline = started + time_budget
    ran = 0

    # Self-heal rows abandoned by a previous timed-out invocation.
    now_iso = time.time()
    for row in fetch_items("queue"):
        info = row["info"]
        if info.get("status") in ("preparing", "downloading", "postprocessing"):
            updated = row.get("updated_at") or ""
            age = now_iso - _iso_epoch(updated)
            if age > STALE_ACTIVE_SECONDS:
                info["status"] = "error"
                info["msg"] = "Download stopped: the serverless function running it timed out."
                info["error"] = info["msg"]
                move_to_done(row["key"], info)
                ran += 1

    pending = [r for r in fetch_items("queue") if r["info"].get("status") in ("queued", "preparing", "downloading", "postprocessing")]
    for row in pending:
        if time.monotonic() > deadline:
            break
        key = row["key"]
        info = row["info"]
        if row.get("cancel_requested"):
            delete_item("queue", key)
            continue
        if info.get("status") in ("preparing", "downloading", "postprocessing"):
            continue  # handled above as stale, or genuinely still active elsewhere
        result = run_download(key, info, deadline)
        if result == "canceled":
            delete_item("queue", key)
        else:
            move_to_done(key, info)
        ran += 1
    return {"status": "ok", "processed": ran}


def _iso_epoch(iso: str) -> float:
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, IndexError):
        return 0.0


def tick() -> dict:
    lease_raw = None
    try:
        lease_raw = get_config("tick_lease")
    except StoreError:
        pass
    if lease_raw:
        try:
            if time.time() - float(lease_raw) < TICK_LEASE_SECONDS:
                return {"status": "ok", "busy": True}
        except ValueError:
            pass
    set_config("tick_lease", str(time.time()))
    try:
        return process_queue()
    finally:
        try:
            set_config("tick_lease", "0")
        except StoreError:
            pass


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

def _info_key(info: dict) -> str:
    return info.get("url") or ""


def handle_snapshot() -> dict:
    queue = [[r["key"], r["info"]] for r in fetch_items("queue")]
    done = [[r["key"], r["info"]] for r in fetch_items("done")]
    subs = []
    for row in fetch_subs():
        data = dict(row.get("data") or {})
        data["id"] = row["id"]
        data["last_checked"] = data.get("last_checked")
        data["seen_count"] = len(row.get("seen_ids") or [])
        subs.append(data)
    return {
        "queue": queue,
        "done": done,
        "formats": public_catalog(),
        "configuration": {
            "CUSTOM_DIRS": True,
            "CREATE_CUSTOM_DIRS": True,
            "DEFAULT_FOLDER": "",
            "OUTPUT_TEMPLATE_CHAPTER": "",
            "PUBLIC_HOST_URL": "/file/",
            "PUBLIC_HOST_AUDIO_URL": "/file/",
            "DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT": "0",
            "SUBSCRIPTION_DEFAULT_CHECK_INTERVAL": "60",
            "ALLOW_YTDL_OPTIONS_OVERRIDES": False,
            "DELETE_FILE_ON_TRASHCAN": "false",
        },
        "custom_dirs": {"download_dir": [""], "audio_download_dir": [""]},
        "subscriptions": subs,
    }


def handle_add(post: dict) -> dict:
    try:
        o = parse_options(post)
    except BadRequest as exc:
        return {"status": "error", "msg": str(exc)}
    problem = unsupported_features(o)
    if problem:
        return {"status": "error", "msg": problem}

    if get_config("cancel_add"):
        try:
            set_config("cancel_add", "")
        except StoreError:
            pass

    try:
        entries = extract_entries(o["url"], o["playlist_item_limit"])
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "msg": f"Could not read this URL: {str(exc)[:400]}"}

    if not entries:
        return {"status": "error", "msg": "yt-dlp returned nothing for this URL"}

    added = 0
    skipped = []
    for entry in entries:
        if get_config("cancel_add"):
            try:
                set_config("cancel_add", "")
            except StoreError:
                pass
            break
        key = entry["url"]
        existing = get_item("queue", key)
        if existing and existing["info"].get("status") not in ("error",):
            skipped.append(entry.get("title") or key)
            continue
        status = "queued" if o["auto_start"] else "pending"
        upsert_item("queue", key, build_info(o, entry, status))
        added += 1

    msg = None
    if skipped and not added:
        msg = f"Already in queue: {skipped[0]}"
    return {"status": "ok", "msg": msg}


def handle_retry(post: dict) -> dict:
    key = str(post.get("id") or "")
    if not key:
        return {"status": "error", "msg": "'id' must be a non-empty string"}
    row = get_item("done", key)
    if not row:
        return {"status": "error", "msg": "Failed download no longer exists."}
    info = row["info"]
    if info.get("status") != "error":
        return {"status": "error", "msg": "Only failed downloads can be retried."}
    o = {
        "url": info["url"],
        "download_type": info["download_type"],
        "codec": info.get("codec") or "auto",
        "format": info["format"],
        "quality": info["quality"],
        "folder": info.get("folder") or "",
        "custom_name_prefix": info.get("custom_name_prefix") or "",
        "playlist_item_limit": info.get("playlist_item_limit") or 0,
        "auto_start": True,
        "split_by_chapters": False,
        "sponsorblock": bool(info.get("sponsorblock")),
        "audio_tags": info.get("audio_tags") or "with_cover",
        "subtitle_language": info.get("subtitle_language") or "en",
        "subtitle_mode": info.get("subtitle_mode") or "prefer_manual",
        "clip_start": None,
        "clip_end": None,
    }
    try:
        entries = extract_entries(info["url"], 1)
        entry = entries[0] if entries else {"url": info["url"], "title": info.get("title")}
    except Exception:  # noqa: BLE001
        entry = {"url": info["url"], "title": info.get("title")}
    upsert_item("queue", info["url"], build_info(o, entry, "queued"))
    return {"status": "ok"}


def handle_start(post: dict) -> dict:
    ids = post.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return {"status": "error", "msg": "'ids' must be a non-empty list of strings"}
    for key in ids:
        row = get_item("queue", key)
        if not row:
            continue
        if row["info"].get("status") in ("pending", "error"):
            patch_item("queue", key, info=_merge_info(row["info"], {"status": "queued"}))
    return {"status": "ok"}


def _merge_info(info: dict, changes: dict) -> dict:
    merged = dict(info)
    merged.update(changes)
    return merged


def handle_delete(post: dict) -> dict:
    ids = post.get("ids")
    where = post.get("where")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return {"status": "error", "msg": "'ids' must be a non-empty list of strings"}
    if where not in ("queue", "done"):
        return {"status": "error", "msg": "incorrect 'where' value"}
    delete_files = bool(post.get("delete_files"))
    for key in ids:
        if where == "queue":
            row = get_item("queue", key)
            if not row:
                continue
            status = row["info"].get("status")
            if status in ("preparing", "downloading", "postprocessing"):
                patch_item("queue", key, cancel_requested=True)
            else:
                delete_item("queue", key)
        else:
            row = get_item("done", key)
            if not row:
                continue
            if delete_files and row.get("storage_path"):
                try:
                    storage_delete(row["storage_path"])
                except StoreError as exc:
                    log.warning("storage delete failed: %s", exc)
            delete_item("done", key)
    return {"status": "ok"}


def handle_cancel_add() -> dict:
    set_config("cancel_add", str(time.time()))
    return {"status": "ok"}


def handle_browse(post: dict) -> dict:
    url = str(post.get("url") or "").strip()
    if not url:
        return {"status": "error", "msg": "missing 'url'"}
    try:
        limit = int(post.get("playlist_item_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    try:
        entries = extract_entries(url, min(limit, MAX_PLAYLIST_ITEMS) if limit else MAX_PLAYLIST_ITEMS)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "msg": f"Could not read this URL: {str(exc)[:400]}"}
    if not entries:
        return {"status": "error", "msg": "yt-dlp returned nothing for this URL"}
    if len(entries) == 1 and (entries[0]["url"] == url or _is_same_video(entries[0]["url"], url)):
        return {"status": "ok", "title": entries[0]["title"]}
    return {
        "status": "ok",
        "title": entries[0].get("title"),
        "entries": [{"url": e["url"], "title": e["title"], "duration": e.get("duration")} for e in entries],
    }


def _is_same_video(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        return re.sub(r"^https?://(www\.)?(m\.)?", "", (u or "").split("&")[0].split("?")[0].rstrip("/"))
    return norm(a) == norm(b) or (a and b and (a in b or b in a))


def handle_history() -> dict:
    return {
        "queue": [r["info"] for r in fetch_items("queue")],
        "done": [r["info"] for r in fetch_items("done")],
        "pending": [],
    }


def handle_cookie_status() -> dict:
    has = bool(get_config("cookies"))
    return {"status": "ok", "has_cookies": has}


def handle_upload_cookies(content: bytes) -> dict:
    if len(content) > MAX_COOKIES_BYTES:
        return {"status": "error", "msg": "Cookie file too large (max 1MB)"}
    if not content.strip():
        return {"status": "error", "msg": "No cookies file provided"}
    set_config("cookies", content.decode("utf-8", "replace"))
    return {"status": "ok", "msg": f"Cookies uploaded ({len(content)} bytes)"}


def handle_delete_cookies() -> dict:
    if not get_config("cookies"):
        return {"status": "error", "msg": "No uploaded cookies to delete"}
    set_config("cookies", "")
    return {"status": "ok"}


# -- subscriptions ----------------------------------------------------------

SUB_FIELDS = (
    "check_interval_minutes", "download_type", "codec", "format", "quality",
    "folder", "title_regex", "skip_subscriber_only", "clip_start", "clip_end",
    "name", "url", "enabled",
)


def handle_subscribe(post: dict) -> dict:
    try:
        o = parse_options(post)
    except BadRequest as exc:
        return {"status": "error", "msg": str(exc)}
    problem = unsupported_features(o)
    if problem:
        return {"status": "error", "msg": problem}
    try:
        interval = int(post.get("check_interval_minutes") or 60)
    except (TypeError, ValueError):
        return {"status": "error", "msg": "check_interval_minutes must be an integer"}
    if interval < 1:
        return {"status": "error", "msg": "check_interval_minutes must be at least 1"}

    name = None
    try:
        entries = extract_entries(o["url"], 1)
        if entries:
            name = entries[0].get("title")
    except Exception:  # noqa: BLE001
        pass
    sub_id = hashlib.sha1(f"{o['url']}|{time.time()}".encode()).hexdigest()[:12]
    data = {
        "id": sub_id,
        "name": name or o["url"],
        "url": o["url"],
        "enabled": True,
        "check_interval_minutes": interval,
        "download_type": o["download_type"],
        "codec": o["codec"],
        "format": o["format"],
        "quality": o["quality"],
        "folder": o["folder"],
        "title_regex": str(post.get("title_regex") or ""),
        "skip_subscriber_only": bool(post.get("skip_subscriber_only")),
        "clip_start": None,
        "clip_end": None,
        "last_checked": None,
        "seen_count": 0,
        "error": None,
        "_options": {k: o[k] for k in ("custom_name_prefix", "audio_tags", "subtitle_language",
                                       "subtitle_mode", "playlist_item_limit")},
    }
    upsert_sub(sub_id, data, [])
    return {"status": "ok"}


def handle_subscriptions_list() -> list:
    out = []
    for row in fetch_subs():
        data = dict(row.get("data") or {})
        data["id"] = row["id"]
        data["seen_count"] = len(row.get("seen_ids") or [])
        data.pop("_options", None)
        out.append(data)
    return out


def handle_subscriptions_update(post: dict) -> dict:
    sub_id = str(post.get("id") or "")
    row = next((r for r in fetch_subs() if r["id"] == sub_id), None)
    if not row:
        return {"status": "error", "msg": "Subscription not found"}
    data = dict(row.get("data") or {})
    for field in ("enabled", "check_interval_minutes", "name", "title_regex", "skip_subscriber_only"):
        if field in post:
            data[field] = post[field]
    upsert_sub(sub_id, data, row.get("seen_ids") or [])
    return {"status": "ok"}


def handle_subscriptions_delete(post: dict) -> dict:
    ids = post.get("ids") or []
    for sub_id in ids:
        delete_sub(str(sub_id))
    return {"status": "ok"}


def handle_subscriptions_check(post: dict) -> dict:
    ids = post.get("ids")
    rows = fetch_subs()
    if isinstance(ids, list) and ids:
        rows = [r for r in rows if r["id"] in set(ids)]
    checked = 0
    for row in rows:
        data = dict(row.get("data") or {})
        if not data.get("enabled", True):
            continue
        seen = list(row.get("seen_ids") or [])
        sub_opts = data.get("_options") or {}
        o = {
            "url": data["url"],
            "download_type": data.get("download_type") or "video",
            "codec": data.get("codec") or "auto",
            "format": data.get("format") or "any",
            "quality": data.get("quality") or "best",
            "folder": data.get("folder") or "",
            "custom_name_prefix": sub_opts.get("custom_name_prefix", ""),
            "playlist_item_limit": data.get("playlist_item_limit") or 0,
            "auto_start": True,
            "split_by_chapters": False,
            "sponsorblock": False,
            "audio_tags": sub_opts.get("audio_tags") or "with_cover",
            "subtitle_language": sub_opts.get("subtitle_language") or "en",
            "subtitle_mode": sub_opts.get("subtitle_mode") or "prefer_manual",
            "clip_start": None,
            "clip_end": None,
        }
        try:
            entries = extract_entries(data["url"], 50)
        except Exception as exc:  # noqa: BLE001
            data["error"] = str(exc)[:400]
            upsert_sub(row["id"], data, seen)
            continue
        title_re = (data.get("title_regex") or "").strip()
        pattern = re.compile(title_re) if title_re else None
        for entry in entries:
            eid = entry.get("id") or entry["url"]
            if eid in seen:
                continue
            if pattern and entry.get("title") and not pattern.search(entry["title"]):
                seen.append(eid)
                continue
            seen.append(eid)
            upsert_item("queue", entry["url"], build_info(o, entry, "queued"))
        data["last_checked"] = int(time.time())
        data["error"] = None
        upsert_sub(row["id"], data, seen)
        checked += 1
    return {"status": "ok", "checked": checked}


# -- file serving -----------------------------------------------------------

def handle_file(rel_path: str):
    rel = unquote(rel_path).lstrip("/")
    match = None
    for state in ("done",):
        for row in fetch_items(state):
            info = row["info"]
            if info.get("file_path") == rel or (
                not info.get("file_path") and info.get("filename") == rel
            ):
                match = row
                break
        if match:
            break
    if not match:
        return JSONResponse({"status": "error", "msg": "File not found"}, status_code=404)

    if match.get("storage_path"):
        try:
            url = storage_sign(match["storage_path"])
            filename = os.path.basename(rel)
            return RedirectResponse(f"{url}&download={filename}", status_code=302)
        except StoreError as exc:
            log.warning("sign failed: %s", exc)

    # Fallback (file too big for Storage): re-fetch the source and stream it.
    info = match["info"]
    return _stream_from_source(info)


def _stream_from_source(info: dict):
    import yt_dlp

    outdir = os.path.join(TMP_ROOT, "stream", _key_hash(info["url"] + str(time.time())))
    os.makedirs(outdir, exist_ok=True)
    opts = _ydl_base()
    opts.update({
        "format": get_format(info["download_type"], info.get("codec", "auto"),
                             info["format"], info["quality"]),
        "outtmpl": os.path.join(outdir, "%(title).200B.%(ext)s"),
        "noplaylist": True,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([info["url"]])
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(outdir, ignore_errors=True)
        return JSONResponse({"status": "error", "msg": f"Re-download failed: {str(exc)[:300]}"}, status_code=502)

    files = []
    for root, _dirs, names in os.walk(outdir):
        for name in names:
            files.append(os.path.join(root, name))
    if not files:
        shutil.rmtree(outdir, ignore_errors=True)
        return JSONResponse({"status": "error", "msg": "Re-download produced no file"}, status_code=502)
    files.sort(key=lambda p: os.path.getsize(p), reverse=True)
    path = files[0]
    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
        background=BackgroundTask(shutil.rmtree, outdir, ignore_errors=True),
    )


# ---------------------------------------------------------------------------
# ASGI routing (tolerates the several shapes Vercel can hand us)
# ---------------------------------------------------------------------------

app = FastAPI(title="MeTube (Vercel + Supabase)")

KNOWN_GET = {"formats", "presets", "history", "cookie-status", "version", "snapshot", "subscriptions", "tick", "health"}
KNOWN_POST = {"add", "browse", "cancel-add", "retry", "start", "delete", "subscribe",
              "subscriptions/update", "subscriptions/delete", "subscriptions/check",
              "upload-cookies", "delete-cookies", "tick"}

_PATH_PREFIXES = ("/api/index.py", "/api/index", "/api")


def resolve_path(request: Request) -> str:
    candidates = []
    for header in ("x-vercel-original-path", "x-invoke-path", "x-matched-path", "x-real-path"):
        value = request.headers.get(header)
        if value:
            candidates.append(value)
    candidates.append(request.url.path)

    fallback = "/" + (request.url.path or "").strip("/")
    for cand in candidates:
        p = unquote((cand or "").split("?")[0])
        stripped = False
        for pref in _PATH_PREFIXES:
            if p == pref:
                p = "/"
                stripped = True
                break
            if p.startswith(pref + "/"):
                p = p[len(pref):]
                stripped = True
                break
        if not stripped and p != "/" and not p.startswith("/"):
            p = "/" + p
        if p != "/":
            p = "/" + p.strip("/")
        rest = p[1:]
        if rest in KNOWN_GET or rest in KNOWN_POST:
            return p
        if rest.startswith("file/"):
            return p
        if rest == "" and stripped:
            return "/health"
    return fallback if fallback.startswith("/") else "/health"


async def dispatch(request: Request):
    method = request.method.upper()
    if method == "OPTIONS":
        return Response(status_code=204)
    path = resolve_path(request)
    rest = path[1:]

    if method == "GET" and rest == "health":
        return JSONResponse({"status": "ok", "service": "metube"})

    if method == "GET" and rest == "snapshot":
        return JSONResponse(await asyncio.to_thread(handle_snapshot))

    if method == "GET" and rest == "formats":
        return JSONResponse(public_catalog())

    if method == "GET" and rest == "presets":
        return JSONResponse({"presets": []})

    if method == "GET" and rest == "history":
        return JSONResponse(await asyncio.to_thread(handle_history))

    if method == "GET" and rest == "cookie-status":
        return JSONResponse(await asyncio.to_thread(handle_cookie_status))

    if method == "GET" and rest == "version":
        import yt_dlp
        return JSONResponse({
            "yt-dlp": yt_dlp.version.__version__,
            "version": os.getenv("METUBE_VERSION", "vercel"),
        })

    if method == "GET" and rest == "subscriptions":
        return JSONResponse(await asyncio.to_thread(handle_subscriptions_list))

    if rest.startswith("file/") and method in ("GET", "HEAD"):
        rel = path[len("/file/"):]
        return await asyncio.to_thread(handle_file, rel)

    if method == "GET" and rest == "tick":
        return JSONResponse(await asyncio.to_thread(tick))

    if method != "POST":
        return JSONResponse({"status": "error", "msg": "not found"}, status_code=404)

    if rest == "upload-cookies":
        form = await request.form()
        upload = form.get("cookies")
        if upload is None:
            return JSONResponse({"status": "error", "msg": "No cookies file provided"}, status_code=400)
        content = await upload.read() if hasattr(upload, "read") else (upload or b"")
        return JSONResponse(await asyncio.to_thread(handle_upload_cookies, content))

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:  # noqa: BLE001
        body = {}

    if rest == "add":
        return JSONResponse(await asyncio.to_thread(handle_add, body))
    if rest == "browse":
        return JSONResponse(await asyncio.to_thread(handle_browse, body))
    if rest == "retry":
        return JSONResponse(await asyncio.to_thread(handle_retry, body))
    if rest == "start":
        return JSONResponse(await asyncio.to_thread(handle_start, body))
    if rest == "delete":
        return JSONResponse(await asyncio.to_thread(handle_delete, body))
    if rest == "cancel-add":
        return JSONResponse(await asyncio.to_thread(handle_cancel_add))
    if rest == "tick":
        return JSONResponse(await asyncio.to_thread(tick))
    if rest == "subscribe":
        return JSONResponse(await asyncio.to_thread(handle_subscribe, body))
    if rest == "subscriptions/update":
        return JSONResponse(await asyncio.to_thread(handle_subscriptions_update, body))
    if rest == "subscriptions/delete":
        return JSONResponse(await asyncio.to_thread(handle_subscriptions_delete, body))
    if rest == "subscriptions/check":
        return JSONResponse(await asyncio.to_thread(handle_subscriptions_check, body))
    if rest == "delete-cookies":
        return JSONResponse(await asyncio.to_thread(handle_delete_cookies))

    return JSONResponse({"status": "error", "msg": "not found"}, status_code=404)


for _method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
    app.add_api_route("/", dispatch, methods=[_method], include_in_schema=False)
    app.add_api_route("/{full_path:path}", dispatch, methods=[_method], include_in_schema=False)
