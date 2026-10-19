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
| Queue / done / subscriptions / cookies / proxy settings | Supabase Postgres (`metube_items`, `metube_subscriptions`, `metube_config`) |
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
* **Proxy support for YouTube's bot check.** Vercel's datacenter IPs are
  routinely answered with `Sign in to confirm you're not a bot`. Add one or
  more HTTP(S)/SOCKS proxies under **Settings → Tools → Proxy** (stored in
  Supabase) or via a `YTDLP_PROXY` / `HTTPS_PROXY` env var, and every yt-dlp
  call — adds, browsing, subscriptions and downloads — is routed through
  them. Blocked proxies are skipped automatically and the failure message
  tells you what to fix.
* **No ffmpeg on Vercel**, so these stock MeTube features are unavailable:
  merged video+audio above progressive quality (~720p max), MP3/WAV/FLAC
  conversion, chapter splitting, clip trimming and SponsorBlock cutting.
* A single download must finish within **5 minutes** (Vercel function limit);
  larger/slower ones are stopped with a clear message — use a lower quality.
* Supabase free plan caps stored files at **50MB**. Bigger files are still
  downloadable: `/file/...` re-fetches the source and streams it on demand.

## Deploys and the service worker

The UI is an Angular PWA, so a stale service worker can pair an old
`index.html` with a hashed JS bundle that the new deployment no longer
serves — the page then renders blank until the site data is cleared. Three
things keep that from happening now:

* `ngsw-config.json` uses `"navigationRequestStrategy": "freshness"`, so
  navigations are fetched from the network first (the cache is the offline
  fallback).
* `index.html`, `ngsw.json`, the service worker files and the manifest are
  served/stamped `Cache-Control: no-store` (see `_serve_ui` in
  `api/index.py` and the `headers` block in `vercel.json`); hashed bundles
  are `immutable` for a year.
* The UI checks for a waiting service worker on startup and whenever the tab
  regains focus, then activates it and reloads (`setupServiceWorkerUpdates`
  in `app.ts`).

If a browser is still stuck on a build from before this change, clear the
site data once (DevTools → Application → Storage → Clear site data); after
that, updates apply themselves.

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
| `YTDLP_PROXY` | Optional proxy used when none is saved in Settings, e.g. `socks5://user:pass@host:1080` (one or comma/newline separated list). `METUBE_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY` are also honoured, in that order. |
| `YTDLP_IMPERSONATE` | Optional browser TLS impersonation target for curl-cffi, e.g. `chrome`. Helps some extractors; leave unset if unsure. |
| `YTDLP_PLAYER_CLIENTS` | Optional YouTube player-client strategy list, e.g. `default,tv;android_vr`. Overrides the built-in rotation. |

## Fixing "Sign in to confirm you're not a bot"

YouTube blocks the shared datacenter IP that Vercel functions run on, so a
plain deployment fails on most videos. Two things help, and a proxy is the
reliable one:

1. **Proxy (recommended).** Open **Settings → Tools → Proxy** and paste a
   working proxy, one per line:

   ```
   http://user:pass@proxy-host:8080
   socks5://user:pass@another-proxy:1080
   ```

   The list is validated, saved in Supabase `metube_config` and never sent
   back to the browser unmasked. When several are configured they are tried
   in order until one gets past the block. Use a residential/mobile proxy:
   free public proxies and other datacenter proxies are usually blocked too.
2. **Automatic player-client rotation (tried first).** Before you even need
   a proxy, a blocked YouTube request is retried through yt-dlp's other
   Innertube clients (`default + tv`, `android_vr`, `default + tv_simply`).
   Which client gets past the check depends on the IP, so this alone fixes
   the error on some deployments. The list is capped and only runs after a
   real block, so normal downloads are unaffected. Tune it with
   `YTDLP_PLAYER_CLIENTS` (comma = clients of one strategy, `;` = separate
   strategies, e.g. `default,tv;android_vr`).
3. **Cookies (secondary).** Export a `cookies.txt` from a logged-in browser
   and upload it under **Settings → Tools → Cookies**. Cookies alone often do
   not clear the bot check when the IP is flagged, but they help with
   age-restricted and private videos — especially together with a proxy.

SOCKS proxies need `PySocks`, which is included in `requirements.txt`.

## Local development

```bash
# UI (npm or pnpm both work; Vercel uses npm)
cd metube-master/ui && npm install && npm run build     # or: npm start (dev server)

# API (needs SUPABASE_URL / SUPABASE_KEY in the environment)
pip install -r requirements.txt
cd api && python -c "import uvicorn, index; uvicorn.run(index.app, port=3000)"
```
