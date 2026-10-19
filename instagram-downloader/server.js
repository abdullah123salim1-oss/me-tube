'use strict';

const path = require('path');
const fs = require('fs');
const { Readable } = require('stream');
const express = require('express');
const { fetchInstagramMedia, demoPayload, ytAvailable } = require('./lib/instagram');

const app = express();
app.disable('x-powered-by');
app.set('trust proxy', 1);

const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, 'public');
const DEMO_DIR = path.join(PUBLIC_DIR, 'demo-media');

app.use(express.json({ limit: '64kb' }));

/* ---------------- simple per-IP rate limit for the API ---------------- */
const WINDOW_MS = 5 * 60 * 1000;
const MAX_REQ = 30;
const buckets = new Map();
setInterval(() => {
  const now = Date.now();
  for (const [k, v] of buckets) if (v.reset < now) buckets.delete(k);
}, 60_000).unref();

function rateLimit(req, res, next) {
  const key = req.ip || 'unknown';
  const now = Date.now();
  let b = buckets.get(key);
  if (!b || b.reset < now) {
    b = { count: 0, reset: now + WINDOW_MS };
    buckets.set(key, b);
  }
  b.count += 1;
  if (b.count > MAX_REQ) {
    return res.status(429).json({
      ok: false,
      error: { code: 'rate_limited', message: 'Bohat zyada requests — kuch minute ruk kar dobara try karein.' },
    });
  }
  return next();
}

/* --------------------------- helpers --------------------------------- */

function jsonError(res, status, code, message, extra = {}) {
  return res.status(status).json({ ok: false, error: { code, message, ...extra } });
}

const ALLOWED_REMOTE_HOST = (host) =>
  /(^|\.)cdninstagram\.com$/i.test(host) ||
  /(^|\.)fbcdn\.net$/i.test(host) ||
  /(^|\.)instagram\.com$/i.test(host) ||
  /(^|\.)fna\.{0,1}fbcdn\.net$/i.test(host) ||
  /scontent/i.test(host);

const EXT_TYPES = {
  mp4: 'video/mp4',
  m4v: 'video/mp4',
  webm: 'video/webm',
  mov: 'video/quicktime',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  png: 'image/png',
  webp: 'image/webp',
  heic: 'image/heic',
  svg: 'image/svg+xml',
  m4a: 'audio/mp4',
};

function safeFilename(raw, fallbackExt) {
  const base = String(raw || 'instagram-media')
    .replace(/\.[A-Za-z0-9]{2,5}$/, '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'instagram-media';
  const ext = String(fallbackExt || 'mp4').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 5) || 'mp4';
  return { base, ext, full: `${base}.${ext}` };
}

/* ------------------------------ API ---------------------------------- */

app.get('/api/health', (req, res) => {
  res.json({ ok: true, uptime: process.uptime(), ytDlp: ytAvailable(), time: new Date().toISOString() });
});

app.post('/api/fetch', rateLimit, async (req, res) => {
  const url = req.body && typeof req.body.url === 'string' ? req.body.url.trim() : '';
  if (!url) {
    return jsonError(res, 400, 'invalid_url', 'URL khaali hai — Instagram ka link paste karein.');
  }
  if (url.length > 2000) {
    return jsonError(res, 400, 'invalid_url', 'Link bohat lamba hai.');
  }

  // offline demo mode — exercised by the "Try demo" button in the UI
  if (/^demo(:|\b)/i.test(url) || /demo\.local/i.test(url)) {
    return res.json({ ok: true, data: demoPayload() });
  }

  try {
    const data = await fetchInstagramMedia(url);
    return res.json({ ok: true, data });
  } catch (e) {
    const status = e.httpStatus || 502;
    return jsonError(res, status, e.code || 'fetch_failed', e.publicMessage || e.message || 'Fetch fail ho gaya.', {
      hint: e.hint,
      tried: e.tried,
    });
  }
});

/**
 * Download proxy — streams the Instagram CDN file back to the browser with
 * an attachment header so it saves instead of navigating (and avoids CORS).
 * Allowed targets: Instagram/Facebook CDN hosts, plus local /demo-media files.
 */
app.get('/api/download', rateLimit, async (req, res) => {
  const u = typeof req.query.u === 'string' ? req.query.u : '';
  if (!u) return jsonError(res, 400, 'invalid_url', 'Download URL missing.');

  const nameParts = safeFilename(req.query.n, req.query.ext || 'mp4');

  // --- local demo media ---
  if (u.startsWith('/')) {
    const rel = path.normalize(u).replace(/^([/\\])+/, '');
    if (!rel.startsWith(`demo-media${path.sep}`)) {
      return jsonError(res, 403, 'forbidden', 'Sirf demo files local download allowed hain.');
    }
    const file = path.join(PUBLIC_DIR, rel);
    if (!file.startsWith(DEMO_DIR) || !fs.existsSync(file)) {
      return jsonError(res, 404, 'not_found', 'Demo file nahi mili.');
    }
    const ext = path.extname(file).slice(1).toLowerCase();
    res.setHeader('Content-Type', EXT_TYPES[ext] || 'application/octet-stream');
    res.setHeader('Content-Disposition', `attachment; filename="${nameParts.base}.${ext}"`);
    return fs.createReadStream(file).pipe(res);
  }

  // --- remote CDN media ---
  let target;
  try {
    target = new URL(u);
  } catch {
    return jsonError(res, 400, 'invalid_url', 'Download URL galat hai.');
  }
  if (target.protocol !== 'https:') {
    return jsonError(res, 400, 'invalid_url', 'Sirf https URLs allowed hain.');
  }
  if (!ALLOWED_REMOTE_HOST(target.hostname)) {
    return jsonError(res, 403, 'forbidden', 'Sirf Instagram CDN links proxy ho sakte hain.');
  }

  const ctrl = new AbortController();
  res.on('close', () => ctrl.abort());
  try {
    const upstream = await fetch(target.toString(), {
      headers: {
        'User-Agent':
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        Referer: 'https://www.instagram.com/',
        Accept: '*/*',
      },
      redirect: 'follow',
      signal: ctrl.signal,
    });
    if (!upstream.ok || !upstream.body) {
      return jsonError(res, 502, 'upstream_failed', `CDN ne ${upstream.status} diya — link expire ho gaya ho sakta hai. Dobara fetch kar ke try karein.`);
    }
    const extFromType = (() => {
      const ct = upstream.headers.get('content-type') || '';
      if (ct.includes('mp4')) return 'mp4';
      if (ct.includes('webm')) return 'webm';
      if (ct.includes('jpeg')) return 'jpg';
      if (ct.includes('png')) return 'png';
      if (ct.includes('webp')) return 'webp';
      return nameParts.ext;
    })();

    const len = upstream.headers.get('content-length');
    if (len) res.setHeader('Content-Length', len);
    res.setHeader('Content-Type', upstream.headers.get('content-type') || EXT_TYPES[extFromType] || 'application/octet-stream');
    res.setHeader('Content-Disposition', `attachment; filename="${nameParts.base}.${extFromType}"`);
    res.setHeader('Cache-Control', 'no-store');
    return Readable.fromWeb(upstream.body).pipe(res);
  } catch (e) {
    if (!res.headersSent) {
      return jsonError(res, 502, 'proxy_failed', 'File stream nahi ho saki — network issue ya link expire.');
    }
    return undefined;
  }
});

/* ---------------------------- static UI ------------------------------ */

app.use(
  express.static(PUBLIC_DIR, {
    index: 'index.html',
    maxAge: '1h',
    setHeaders: (res, filePath) => {
      if (filePath.endsWith('.html')) {
        res.setHeader('Cache-Control', 'no-store');
      }
    },
  })
);

app.use((req, res) => {
  if (req.path.startsWith('/api/')) {
    return jsonError(res, 404, 'not_found', 'API endpoint nahi mila.');
  }
  return res.status(404).sendFile(path.join(PUBLIC_DIR, 'index.html'));
});

app.use((err, req, res, next) => {
  // eslint-disable-line no-unused-vars
  if (err && err.type === 'entity.parse.failed') {
    return jsonError(res, 400, 'bad_json', 'Request body JSON nahi hai.');
  }
  return jsonError(res, 500, 'server_error', 'Unexpected server error.');
});

app.listen(PORT, '0.0.0.0', () => {
  // eslint-disable-next-line no-console
  console.log(`InstaSaver listening on http://0.0.0.0:${PORT} (yt-dlp: ${ytAvailable() ? 'yes' : 'no'})`);
});
