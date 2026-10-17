# me-tube

[MeTube](https://github.com/alexta69/metube) — a web UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp) —
deployed on **Vercel** with **Supabase** as its backend.

## How this deployment works

The stock MeTube is a self-hosted Docker app (long-running aiohttp server,
Socket.IO, background worker processes, local disk). Vercel serverless has none
of those, so this repo keeps the full MeTube Angular UI (`metube-master/ui`)
and serves it with a cloud-shaped backend (`api/index.py`):

| Piece | Where it runs |
| --- | --- |
| Angular UI (the stock MeTube front-end) | Vercel static hosting (`metube-master/ui`) |
| REST API + yt-dlp downloads | Vercel Python function (`api/index.py`) |
| Queue / done / subscriptions / cookies state | Supabase Postgres (`metube_items`, `metube_subscriptions`, `metube_config`) |
| Finished files | Supabase Storage bucket `metube-files` (signed URLs via `/file/...`) |

Because serverless functions cannot hold WebSockets, the UI's Socket.IO service
was replaced by a drop-in polling shim
(`metube-master/ui/src/app/services/metube-socket.service.ts`) that polls
`GET /api/snapshot` and replays the exact same events the Socket.IO server
emitted (`all`, `added`, `updated`, `completed`, `canceled`, `cleared`, …).

Downloads run inside `POST /api/tick` — the browser fires it whenever the
queue holds work, and it executes yt-dlp inline (bounded by Vercel's 5-minute
function limit) while streaming progress into Supabase.

## Features & platform limits

* Video (progressive MP4/auto), audio (M4A/OPUS), captions, thumbnails.
* Playlists/channels (up to 25 items per add), queue with live progress,
  retry, cancel, history, cookie upload, subscriptions (manual check).
* **No ffmpeg on Vercel**, so these stock MeTube features are unavailable:
  merged video+audio above progressive quality (~720p max), MP3/WAV/FLAC
  conversion, chapter splitting, clip trimming and SponsorBlock cutting.
* A single download must finish within **5 minutes** (Vercel function limit);
  larger/slower ones are stopped with a clear message — use a lower quality.
* Supabase free plan caps stored files at **50MB**. Bigger files are still
  downloadable: `/file/...` re-fetches the source and streams it on demand.

## Repository layout

* `api/index.py` — the serverless backend (FastAPI): REST API, yt-dlp runner,
  Supabase Postgres + Storage access. `api/requirements.txt` / `requirements.txt`
  — Python dependencies for the Vercel function.
* `vercel.json` — Vercel build config (UI build + Python function + URL rewrites).
* `metube-master/` — the unmodified upstream MeTube sources, with the UI's
  socket service swapped for the polling shim.

## Configuration (Vercel project env)

| Variable | Purpose |
| --- | --- |
| `SUPABASE_URL` | Supabase project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_KEY` | Supabase **service role** key (server-side only) |

## Local development

```bash
# UI
cd metube-master/ui && pnpm install && pnpm build      # or: pnpm start (dev server)

# API (needs SUPABASE_URL / SUPABASE_KEY in the environment)
pip install -r requirements.txt
cd api && python -c "import uvicorn, index; uvicorn.run(index.app, port=3000)"
```
