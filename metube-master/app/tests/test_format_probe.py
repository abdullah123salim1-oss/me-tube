"""Unit tests for the format picker's probe summaries (no network: pure dict in, rows out)."""

import format_probe


def _fmt(**kw):
    base = {"format_id": "x", "ext": "mp4", "vcodec": "none", "acodec": "none"}
    base.update(kw)
    return base


def test_music_rows_label_dedupe_and_sort():
    s = format_probe.summarize_formats({
        "title": "T",
        "duration": 595,
        "thumbnail": "https://img/x.jpg",
        "formats": [
            _fmt(format_id="140", ext="m4a", acodec="mp4a.40.2", abr=128.3, filesize=3_600_000),
            _fmt(format_id="139", ext="m4a", acodec="mp4a.40.2", abr=48, filesize=1_200_000),
            _fmt(format_id="249", ext="webm", acodec="opus", abr=48, filesize=1_000_000),
            _fmt(format_id="251", ext="webm", acodec="opus", abr=128.3, filesize=3_900_000),
        ],
    })
    music = s["music"]
    assert s["title"] == "T"
    assert s["thumbnail"] == "https://img/x.jpg"
    assert s["duration"] == 595
    # Lowest bitrate first, one row per bitrate, larger stream wins a bucket.
    assert [r["label"] for r in music] == ["48KBPS", "128KBPS"]
    assert music[0]["id"] == "139"
    assert music[1]["id"] == "251"
    for row in music:
        assert row["kind"] == "audio"
        assert row["badge"] == "MP3"
        assert row["fast"] is True
        assert row["selector"] == row["id"]  # audio rows download the exact stream


def test_video_rows_prefer_progressive_and_sort_desc():
    s = format_probe.summarize_formats({
        "formats": [
            _fmt(format_id="137", vcodec="avc1", height=1080, filesize=80_000_000, tbr=2500),
            _fmt(format_id="18", vcodec="avc1", acodec="mp4a", height=360, filesize=44_000_000, tbr=500),
            _fmt(format_id="160", vcodec="avc1", height=360, filesize=20_000_000, tbr=300),
            _fmt(format_id="231", vcodec="vp09", height=360, ext="webm", filesize=21_000_000, tbr=310),
            _fmt(format_id="sb0", ext="mhtml", format_note="storyboard"),
            _fmt(format_id="audioonly", acodec="mp4a"),
        ],
    })
    video = s["video"]
    # Highest resolution first; storyboards and audio-only streams are skipped.
    assert [r["label"] for r in video] == ["1080P", "360P"]
    assert video[0]["id"] == "137"
    assert video[0]["selector"] == "137+bestaudio/137"
    assert video[0]["fast"] is False
    assert video[0]["badge"] == "MP4"
    # The progressive stream wins the 360p bucket and needs no merge.
    assert video[1]["id"] == "18"
    assert video[1]["selector"] == "18"
    assert video[1]["fast"] is True
    assert video[1]["height"] == 360


def test_video_rows_label_fallbacks_and_ext_badge():
    s = format_probe.summarize_formats({
        "formats": [
            _fmt(format_id="99", vcodec="avc1", format_note="HD720"),
            _fmt(format_id="231", vcodec="vp09", ext="webm", height=480, acodec="opus"),
        ],
    })
    by_id = {r["id"]: r for r in s["video"]}
    assert by_id["99"]["label"] == "HD720"
    assert by_id["99"]["height"] is None
    assert by_id["231"]["badge"] == "WEBM"
    assert by_id["231"]["fast"] is True  # both codecs present: nothing to merge


def test_missing_sizes_and_thumbnail_fallback():
    s = format_probe.summarize_formats({
        "title": "No size",
        "thumbnails": [{"url": "https://img/a.jpg"}, {"url": "https://img/b.jpg"}],
        "formats": [
            _fmt(format_id="18", vcodec="avc1", acodec="mp4a", height=360),
            _fmt(format_id="140", acodec="mp4a"),  # no abr: LOW row
        ],
    })
    assert s["thumbnail"] == "https://img/b.jpg"
    assert s["duration"] is None
    assert s["music"][0]["label"] == "LOW"
    assert s["music"][0]["filesize"] is None
    assert s["video"][0]["filesize"] is None


def test_empty_formats_yield_no_rows():
    s = format_probe.summarize_formats({"title": "T", "formats": []})
    assert s["music"] == []
    assert s["video"] == []
    s2 = format_probe.summarize_formats({})
    assert s2["music"] == []
    assert s2["video"] == []
