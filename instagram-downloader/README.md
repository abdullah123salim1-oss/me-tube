# InstaSaver — Instagram Video Downloader (Node.js)

A self-hosted Instagram Reels / video / photo / carousel downloader with a
modern dark UI. Paste a link → live preview → save in original quality. No
login, no ads.

![Node](https://img.shields.io/badge/node-%3E%3D18.17-339933) ![Express](https://img.shields.io/badge/express-4-000000)

## Quick start

```bash
cd instagram-downloader
npm install        # also fetches the yt-dlp binary (optional but recommended)
npm start          # → http://localhost:3000
```

Dev mode with auto-reload: `npm run dev`.

## How it finds the video

`POST /api/fetch` tries several layers and returns the first that works:

| # | Layer | Notes |
|---|-------|-------|
| 1 | **yt-dlp** (optional `youtube-dl-exec` npm package) | Most reliable; the binary downloads from GitHub during `npm install`. If it fails, install is skipped silently and the scraper layers still work. |
| 2 | **Post page JSON blobs** | Parses `xdt_shortcode_media` / `video_versions` embedded in the public page HTML, plus `window._sharedData` and a raw `.mp4` scan fallback. |
| 3 | **graphql/query** (`query_hash`) | Legacy public GraphQL endpoint for shortcode media. |
| 4 | **`?__a=1&__d=dis`** | Instagram's JSON endpoint. |
| 5 | **oEmbed** | Metadata only (author / thumbnail) — used to fill gaps. |

Supported links: `/reel/`, `/reels/`, `/p/`, `/tv/`, `/share/…`. Private
posts, stories and anything behind login are **not** supported.

Downloading goes through `GET /api/download?u=…&n=…`, which streams the file
from Instagram's CDN with an `attachment` header (proper filename, no CORS,
no expired-URL-in-the-address-bar). Only Instagram/Facebook CDN hosts are
proxied.

## API

### `POST /api/fetch`
```json
{ "url": "https://www.instagram.com/reel/CODE/" }
```
→
```json
{
  "ok": true,
  "data": {
    "shortcode": "CODE",
    "source": "yt-dlp",
    "author": { "username": "creator" },
    "caption": "…",
    "isVideo": true,
    "media": [
      { "type": "video", "url": "https://…cdninstagram…", "thumbnail": "https://…",
        "width": 1080, "height": 1920,
        "qualities": [{ "label": "1080p", "url": "https://…", "height": 1080 }] }
    ]
  }
}
```
Errors are `{ "ok": false, "error": { "code", "message", "hint?", "tried?" } }`.

Other routes: `GET /api/health`, `GET /api/download`, plus the static UI in `public/`.

## Demo mode (works fully offline)

Type **`demo`** in the input (or press **Live demo**) — the UI renders a
3-photo sample carousel served from `/demo-media/`, so you can exercise the
whole fetch → preview → download-proxy flow without reaching Instagram. Handy
for restricted sandboxes and CI previews where outbound instagram.com is
blocked.

## Deployment

Any Node 18+ host works (Render, Railway, Fly, a VPS…): `npm ci && npm start`,
set `PORT` if needed. A few notes:

- **Datacenter IPs get blocked by Instagram** far more often than home/mobile
  IPs. If your hosted instance starts failing with `login_required`, that's
  why — the yt-dlp layer and cookies help, but reliability is best on a
  residential connection.
- Rate limiting: 30 API requests / 5 minutes / IP (in-memory).
- To deploy on Vercel as a serverless function you'd need to export the
  Express app instead of listening and keep responses under the function
  time limit; the streaming proxy is the main thing to watch.

## Legal

Download only content you own or have permission to save. Respect copyright
and Instagram's Terms of Use. This project is for personal use and is not
affiliated with Instagram or Meta.
