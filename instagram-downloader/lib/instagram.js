'use strict';

/**
 * Instagram media extractor.
 *
 * Layered strategy — the first layer that yields usable media wins:
 *   1. yt-dlp (via the optional `youtube-dl-exec` package, which bundles the
 *      yt-dlp binary from GitHub). Most reliable when available.
 *   2. Public post page HTML — parses the JSON blobs Instagram embeds in
 *      <script type="application/json"> tags (xdt_shortcode_media /
 *      video_versions), window._sharedData, and a raw .mp4 URL fallback.
 *   3. The legacy graphql/query endpoint (query_hash shortcode_media).
 *   4. The `?__a=1&__d=dis` JSON endpoint.
 *   5. oEmbed — metadata only (author / thumbnail), used to fill gaps.
 */

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';
const IG_APP_ID = '936619743392459';
const GRAPHQL_QUERY_HASH = '9f8827793ef34641b2fb195d4d41151c';
const NETWORK_CODES = new Set([
  'ENOTFOUND', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'EAI_AGAIN',
  'ENETUNREACH', 'EHOSTUNREACH', 'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_SOCKET',
]);

class IgError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = 'IgError';
    this.code = code;
    this.hint = extra.hint;
    this.httpStatus = extra.httpStatus || 502;
    this.fatal = extra.fatal || false; // stop trying further layers
    this.tried = extra.tried;
  }
}

const invalid = (msg) =>
  new IgError('invalid_url', msg || 'Yeh Instagram ka reel/post link nahi lag raha.', {
    hint: 'Link aisa hona chahiye: https://www.instagram.com/reel/SHORTCODE/ ya .../p/SHORTCODE/',
    httpStatus: 400,
  });

/* ------------------------------------------------------------------ */
/* URL helpers                                                        */
/* ------------------------------------------------------------------ */

function parseInstagramUrl(input) {
  let s = String(input || '').trim();
  if (!s) throw invalid('Link khaali hai — Instagram ka URL paste karein.');
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;

  let u;
  try {
    u = new URL(s);
  } catch {
    throw invalid();
  }

  const host = u.hostname.toLowerCase().replace(/^(www\.|m\.)/, '');
  if (host !== 'instagram.com' && host !== 'instagr.am') {
    throw invalid('Sirf instagram.com ke links support hote hain.');
  }

  const m =
    u.pathname.match(/^\/(p|reel|reels|tv)\/([A-Za-z0-9_-]{5,30})/) ||
    u.pathname.match(/^\/share\/(p|reel|reels|tv)\/([A-Za-z0-9_-]{5,30})/);
  if (!m) {
    throw invalid('Is link mein koi reel/post shortcode nahi mila.');
  }

  const kind = m[1] === 'reels' ? 'reel' : m[1];
  const shortcode = m[2];
  return {
    shortcode,
    kind,
    pageUrl: `https://www.instagram.com/${kind}/${shortcode}/`,
    originalUrl: s,
  };
}

/* ------------------------------------------------------------------ */
/* HTTP helpers                                                       */
/* ------------------------------------------------------------------ */

function isNetworkError(e) {
  const code = e && (e.code || (e.cause && e.cause.code));
  return (
    NETWORK_CODES.has(code) ||
    e.name === 'AbortError' ||
    e.name === 'TimeoutError' ||
    (typeof e.message === 'string' && /fetch failed/i.test(e.message))
  );
}

async function httpText(url, { headers = {}, timeoutMs = 15000, maxBytes = 8_000_000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers: {
        'User-Agent': UA,
        'Accept-Language': 'en-US,en;q=0.9',
        ...headers,
      },
      redirect: 'follow',
      signal: ctrl.signal,
    });
    const finalUrl = res.url;
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status}`);
      err.status = res.status;
      err.finalUrl = finalUrl;
      throw err;
    }
    // stream the body but stop reading once we have enough
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
      if (total >= maxBytes) {
        try { await reader.cancel(); } catch { /* noop */ }
        break;
      }
    }
    const buf = Buffer.concat(chunks.map((c) => Buffer.from(c)));
    return { text: buf.toString('utf8'), finalUrl };
  } finally {
    clearTimeout(t);
  }
}

async function httpJson(url, opts = {}) {
  const { text, finalUrl } = await httpText(url, {
    ...opts,
    headers: { Accept: 'application/json,*/*;q=0.8', ...(opts.headers || {}) },
  });
  return { data: JSON.parse(text), finalUrl };
}

/* ------------------------------------------------------------------ */
/* Media mapping (shared shape)                                       */
/* ------------------------------------------------------------------ */

function* walk(obj, limit = { n: 0 }) {
  if (!obj || typeof obj !== 'object' || limit.n > 20000) return;
  limit.n += 1;
  yield obj;
  for (const v of Object.values(obj)) {
    if (v && typeof v === 'object') yield* walk(v, limit);
  }
}

function pickVersions(node) {
  if (!Array.isArray(node.video_versions)) return [];
  return node.video_versions
    .filter((v) => v && v.url)
    .map((v) => ({ url: v.url, width: v.width || 0, height: v.height || 0, type: v.type || 0 }))
    .sort((a, b) => b.height - a.height || b.type - a.type);
}

function isImageUrl(u) {
  return /\.(jpg|jpeg|png|webp|heic)(\?|$)/i.test(u || '');
}

function mediaFromNode(node, index = 0) {
  if (!node || typeof node !== 'object') return null;
  const versions = pickVersions(node);
  const display =
    node.display_url ||
    node.thumbnail_src ||
    (Array.isArray(node.display_resources) && node.display_resources.length
      ? node.display_resources[node.display_resources.length - 1].src
      : null) ||
    (node.image_versions2 &&
      Array.isArray(node.image_versions2.candidates) &&
      node.image_versions2.candidates.length
      ? node.image_versions2.candidates[0].url
      : null);

  const isVideo = Boolean(
    node.is_video || node.video_url || versions.length || (node.product_type === 'clips')
  );

  const url = isVideo ? (versions[0] && versions[0].url) || node.video_url : display;
  if (!url) return null;

  const dims = node.dimensions || node.original_width
    ? { width: node.original_width || (node.dimensions && node.dimensions.width) || 0,
        height: node.original_height || (node.dimensions && node.dimensions.height) || 0 }
    : { width: versions[0] ? versions[0].width : 0, height: versions[0] ? versions[0].height : 0 };

  const qualities = [];
  const seen = new Set();
  for (const v of versions) {
    const key = v.height || v.url.length;
    if (seen.has(key)) continue;
    seen.add(key);
    qualities.push({ label: v.height ? `${v.height}p` : `Option ${qualities.length + 1}`, url: v.url, height: v.height || 0 });
    if (qualities.length >= 4) break;
  }

  return {
    id: node.id || node.pk || `media-${index}`,
    type: isVideo ? 'video' : 'image',
    url,
    thumbnail: display || null,
    width: dims.width || 0,
    height: dims.height || 0,
    qualities,
  };
}

function captionText(node) {
  const t1 =
    node.edge_media_to_caption &&
    Array.isArray(node.edge_media_to_caption.edges) &&
    node.edge_media_to_caption.edges[0] &&
    node.edge_media_to_caption.edges[0].node
      ? node.edge_media_to_caption.edges[0].node.text
      : null;
  const t2 = typeof node.caption === 'string' ? node.caption : node.caption && node.caption.text;
  return (t1 || t2 || '').trim();
}

function ownerName(node) {
  const o = node.owner || node.user || {};
  return o.username || o.name || (typeof node.owner === 'string' ? node.owner : null) || null;
}

function resultFromShortcodeNode(node, parsed, source) {
  if (!node) return null;
  let childNodes = null;
  if (node.edge_sidecar_to_children && Array.isArray(node.edge_sidecar_to_children.edges)) {
    childNodes = node.edge_sidecar_to_children.edges.map((e) => e && e.node).filter(Boolean);
  } else if (Array.isArray(node.carousel_media)) {
    childNodes = node.carousel_media;
  }

  const media = [];
  if (childNodes && childNodes.length) {
    childNodes.forEach((n, i) => {
      const m = mediaFromNode(n, i);
      if (m) media.push(m);
    });
  }
  if (!media.length) {
    const single = mediaFromNode(node, 0);
    if (single) media.push(single);
  }
  if (!media.length) return null;

  return {
    shortcode: parsed.shortcode,
    postUrl: parsed.pageUrl,
    source,
    author: { username: ownerName(node) || null },
    caption: captionText(node),
    thumbnail: media[0].thumbnail || null,
    isVideo: media.some((m) => m.type === 'video'),
    media,
  };
}

/* ------------------------------------------------------------------ */
/* Layer 1: yt-dlp (optional dependency)                              */
/* ------------------------------------------------------------------ */

let ytWrap = null;
let ytChecked = false;
let ytBinPath = null;

function getYtDlp() {
  if (ytChecked) return ytWrap || ytBinPath;
  ytChecked = true;
  try {
    // optional dependency — may be absent if its postinstall failed
    // eslint-disable-next-line global-require
    ytWrap = require('youtube-dl-exec');
  } catch {
    ytWrap = null;
  }
  if (!ytWrap) {
    // fall back to a yt-dlp installed on PATH (Docker images, system packages)
    try {
      // eslint-disable-next-line global-require
      const { execFileSync } = require('child_process');
      execFileSync('yt-dlp', ['--version'], { stdio: 'pipe', timeout: 5000 });
      ytBinPath = 'yt-dlp';
    } catch {
      ytBinPath = null;
    }
  }
  return ytWrap || ytBinPath;
}

function ytAvailable() {
  if (!ytChecked) getYtDlp();
  return Boolean(ytWrap || ytBinPath);
}

function runYtDlpBinary(binPath, url) {
  // eslint-disable-next-line global-require
  const { execFile } = require('child_process');
  return new Promise((resolve, reject) => {
    execFile(
      binPath,
      ['--dump-single-json', '--no-playlist', '--no-warnings', '--add-header', `User-Agent:${UA}`, url],
      { timeout: 60000, maxBuffer: 64 * 1024 * 1024, killSignal: 'SIGKILL' },
      (err, stdout, stderr) => {
        if (err) return reject(new Error((stderr || err.message || 'yt-dlp failed').slice(0, 300)));
        try {
          return resolve(JSON.parse(stdout));
        } catch {
          return reject(new Error('yt-dlp output parse failed'));
        }
      }
    );
  });
}

async function viaYtDlp(parsed) {
  const runner = getYtDlp();
  if (!runner) throw new Error('yt-dlp binary not installed');

  let info;
  if (ytWrap) {
    info = await ytWrap(parsed.pageUrl, {
      dumpSingleJson: true,
      noPlaylist: true,
      noWarnings: true,
      addHeader: [`User-Agent:${UA}`],
    }, { timeout: 60000, maxBuffer: 64 * 1024 * 1024, killSignal: 'SIGKILL' });
  } else {
    info = await runYtDlpBinary(ytBinPath, parsed.pageUrl);
  }
  if (!info) throw new Error('yt-dlp returned no info');

  if (info.is_private || /login/i.test(info.availability || '')) {
    throw new IgError('login_required', 'Yeh private post hai — bina login ke download nahi ho sakta.', { fatal: false });
  }

  const mapEntry = (e, i) => {
    if (!e) return null;
    const formats = Array.isArray(e.formats) ? e.formats : [];
    const vids = formats
      .filter((f) => f && f.url && f.vcodec && f.vcodec !== 'none')
      .sort((a, b) => (b.height || 0) - (a.height || 0) || (b.tbr || 0) - (a.tbr || 0));
    const progressive = vids.filter((f) => f.acodec && f.acodec !== 'none' && (!f.protocol || /^https?/.test(f.protocol)));
    const chosen = progressive.length ? progressive : vids;
    const qualities = [];
    const seen = new Set();
    for (const f of chosen) {
      const key = f.height || f.format_id;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      qualities.push({ label: f.height ? `${f.height}p` : String(f.format_note || f.format_id), url: f.url, height: f.height || 0 });
      if (qualities.length >= 4) break;
    }
    let url = chosen.length ? chosen[0].url : null;
    let type = 'video';
    if (!url && e.url) {
      url = e.url;
      type = isImageUrl(url) ? 'image' : 'video';
    } else if (!url) {
      type = 'image';
      url = e.thumbnail || null;
    }
    if (!url) return null;
    return {
      id: e.id || `media-${i}`,
      type,
      url,
      thumbnail: e.thumbnail || (qualities.length ? null : url),
      width: e.width || 0,
      height: e.height || 0,
      qualities,
    };
  };

  const entries = Array.isArray(info.entries) && info.entries.length ? info.entries : [info];
  const media = entries.map(mapEntry).filter(Boolean);
  if (!media.length) throw new Error('yt-dlp found no downloadable media');

  const username =
    info.uploader_id || info.uploader || info.channel_id || info.channel || null;
  return {
    shortcode: parsed.shortcode,
    postUrl: parsed.pageUrl,
    source: 'yt-dlp',
    author: { username },
    caption: (info.description || info.title || '').trim(),
    thumbnail: info.thumbnail || media[0].thumbnail || null,
    isVideo: media.some((m) => m.type === 'video'),
    media,
  };
}

/* ------------------------------------------------------------------ */
/* Layer 2: post page HTML                                            */
/* ------------------------------------------------------------------ */

function unescapeIgUrl(u) {
  return String(u)
    .replace(/\\u0025/g, '%')
    .replace(/\\u0026/g, '&')
    .replace(/\\u003C/g, '<')
    .replace(/\\u003E/g, '>')
    .replace(/\\\//g, '/')
    .replace(/&amp;/g, '&');
}

function findShortcodeNode(json) {
  for (const o of walk(json)) {
    if (o.xdt_shortcode_media && typeof o.xdt_shortcode_media === 'object') return o.xdt_shortcode_media;
  }
  for (const o of walk(json)) {
    if (o.shortcode_media && (o.shortcode_media.is_video !== undefined || o.shortcode_media.video_url || o.shortcode_media.video_versions)) {
      return o.shortcode_media;
    }
  }
  for (const o of walk(json)) {
    if (o.video_versions && o.video_versions.length) return o;
  }
  for (const o of walk(json)) {
    if (o.graphql && o.graphql.shortcode_media) return o.graphql.shortcode_media;
  }
  return null;
}

function scrapeJsonScripts(html) {
  const re = /<script[^>]*type=["']application\/json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const body = m[1];
    if (!body || (body.indexOf('video_versions') === -1 && body.indexOf('shortcode_media') === -1 && body.indexOf('video_url') === -1)) {
      continue;
    }
    try {
      const node = findShortcodeNode(JSON.parse(body));
      if (node) return node;
    } catch { /* not pure JSON */ }
  }
  return null;
}

function scrapeLegacySharedData(html) {
  const m = html.match(/window\._sharedData\s*=\s*(\{[\s\S]*?\})\s*;\s*<\/script>/);
  if (!m) return null;
  try {
    const json = JSON.parse(m[1]);
    const pages = json.entry_data && (json.entry_data.PostPage || json.entry_data.VideoPage);
    if (pages && pages[0] && pages[0].graphql) return pages[0].graphql.shortcode_media;
  } catch { /* ignore */ }
  return null;
}

function scrapeRawMp4(html, parsed) {
  const re = /https?:\\?\/\\?\/[^"'\\\s]+?\.mp4[^"'\\\s]*/gi;
  const found = [];
  const seen = new Set();
  let m;
  while ((m = re.exec(html))) {
    const clean = unescapeIgUrl(m[0]);
    if (seen.has(clean)) continue;
    if (/profile|favicon|sprite/i.test(clean)) continue;
    seen.add(clean);
    found.push(clean);
    if (found.length >= 4) break;
  }
  if (!found.length) return null;

  const ogImage = html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']/i) ||
    html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i);
  const ogTitle = html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i) ||
    html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']/i);

  const userMatch = html.match(/"username"\s*:\s*"([A-Za-z0-9._]{1,30})"/);
  const captionMatch = html.match(/<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']{0,500})["']/i);

  return {
    shortcode: parsed.shortcode,
    postUrl: parsed.pageUrl,
    source: 'html-scan',
    author: { username: userMatch ? userMatch[1] : null },
    caption: captionMatch ? captionMatch[1] : (ogTitle ? ogTitle[1] : ''),
    thumbnail: ogImage ? unescapeIgUrl(ogImage[1]) : null,
    isVideo: true,
    media: found.slice(0, 1).map((u, i) => ({
      id: `media-${i}`,
      type: 'video',
      url: u,
      thumbnail: ogImage ? unescapeIgUrl(ogImage[1]) : null,
      width: 0,
      height: 0,
      qualities: found.map((fu, fi) => ({ label: `Option ${fi + 1}`, url: fu, height: 0 })),
    })),
  };
}

function looksLikeLoginWall(html, finalUrl) {
  if (finalUrl && /\/accounts\/login/.test(finalUrl)) return true;
  return /Login • Instagram|login_page|href="\/accounts\/login/i.test(html);
}

async function viaHtml(parsed) {
  const { text: html, finalUrl } = await httpText(parsed.pageUrl, {
    headers: {
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
      'Sec-Fetch-Site': 'none',
      'Cache-Control': 'no-cache',
    },
    timeoutMs: 15000,
  });

  // 1) embedded JSON blobs
  const node = scrapeJsonScripts(html);
  const fromJson = node && resultFromShortcodeNode(node, parsed, 'html-json');
  if (fromJson) return fromJson;

  // 2) legacy window._sharedData
  const legacy = scrapeLegacySharedData(html);
  const fromLegacy = legacy && resultFromShortcodeNode(legacy, parsed, 'shared-data');
  if (fromLegacy) return fromLegacy;

  // 3) raw mp4 scan
  const raw = scrapeRawMp4(html, parsed);
  if (raw) return raw;

  if (looksLikeLoginWall(html, finalUrl)) {
    throw new IgError('login_required', 'Instagram ne login maanga — yeh private post ho sakta hai ya IP flag hui hai.', {
      hint: 'Apne account ke public posts ke saath try karein. Agar aap server/Vercel pe hain to Instagram datacenter IPs ko block kar deta hai — local machine se try karein ya yt-dlp + cookies use karein.',
    });
  }
  throw new Error('Page mila lekin us mein koi media nahi mila (Instagram ne page structure badal diya ho sakta hai).');
}

/* ------------------------------------------------------------------ */
/* Layer 3: legacy graphql endpoint                                   */
/* ------------------------------------------------------------------ */

async function viaGraphQL(parsed) {
  const variables = JSON.stringify({
    shortcode: parsed.shortcode,
    child_comment_count: 0,
    fetch_comment_count: 0,
    parent_comment_count: 0,
    has_threaded_comments: false,
  });
  const url =
    `https://www.instagram.com/graphql/query/?query_hash=${GRAPHQL_QUERY_HASH}` +
    `&variables=${encodeURIComponent(variables)}`;
  const { data } = await httpJson(url, {
    headers: {
      'X-IG-App-ID': IG_APP_ID,
      'X-ASBD-ID': '129477',
      'X-Requested-With': 'XMLHttpRequest',
      Referer: parsed.pageUrl,
    },
    timeoutMs: 12000,
  });
  const node = data && data.data && data.data.shortcode_media;
  if (!node) {
    if (data && data.status === 'fail') throw new Error(`graphql: ${data.message || 'fail'}`);
    throw new Error('graphql returned no media');
  }
  const result = resultFromShortcodeNode(node, parsed, 'graphql');
  if (!result) throw new Error('graphql node has no media');
  return result;
}

/* ------------------------------------------------------------------ */
/* Layer 4: ?__a=1&__d=dis                                            */
/* ------------------------------------------------------------------ */

async function viaA1(parsed) {
  const url = `https://www.instagram.com/${parsed.kind}/${parsed.shortcode}/?__a=1&__d=dis`;
  const { data } = await httpJson(url, {
    headers: {
      'X-IG-App-ID': IG_APP_ID,
      'X-Requested-With': 'XMLHttpRequest',
      Referer: parsed.pageUrl,
    },
    timeoutMs: 12000,
  });
  const node =
    (data && data.graphql && data.graphql.shortcode_media) ||
    (data && Array.isArray(data.items) && data.items[0]) ||
    null;
  if (!node) throw new Error('__a=1 returned no media');
  const result = resultFromShortcodeNode(node, parsed, 'json-api');
  if (!result) throw new Error('__a=1 node has no media');
  return result;
}

/* ------------------------------------------------------------------ */
/* oEmbed metadata fill                                               */
/* ------------------------------------------------------------------ */

async function fillOembed(result, parsed) {
  if (result.author.username && result.thumbnail) return result;
  try {
    const { data } = await httpJson(
      `https://www.instagram.com/api/v1/oembed/?url=${encodeURIComponent(parsed.pageUrl)}`,
      { timeoutMs: 8000 }
    );
    if (!result.author.username && data.author_name) {
      result.author.username = String(data.author_name).replace(/^@/, '');
    }
    if (!result.thumbnail && data.thumbnail_url) result.thumbnail = data.thumbnail_url;
    if (!result.caption && data.title) result.caption = String(data.title);
  } catch { /* metadata enrichment is best-effort */ }
  return result;
}

/* ------------------------------------------------------------------ */
/* Orchestrator                                                       */
/* ------------------------------------------------------------------ */

function hasMedia(r) {
  return r && Array.isArray(r.media) && r.media.some((m) => m && m.url);
}

async function fetchInstagramMedia(input) {
  const parsed = parseInstagramUrl(input);
  const tried = [];
  let sawNetwork = false;
  let sawLogin = false;

  const layers = [];
  if (ytAvailable()) layers.push(['yt-dlp', () => viaYtDlp(parsed)]);
  layers.push(['web-page', () => viaHtml(parsed)]);
  layers.push(['graphql', () => viaGraphQL(parsed)]);
  layers.push(['json-api', () => viaA1(parsed)]);

  for (const [name, fn] of layers) {
    try {
      const result = await fn();
      if (hasMedia(result)) {
        await fillOembed(result, parsed);
        return result;
      }
      tried.push(`${name}: no media returned`);
    } catch (e) {
      if (isNetworkError(e)) sawNetwork = true;
      if (e && e.code === 'login_required') sawLogin = true;
      if (e && e.fatal) throw e;
      tried.push(`${name}: ${e.publicMessage || e.message || 'failed'}`.slice(0, 160));
    }
  }

  if (sawNetwork && tried.length >= 2) {
    throw new IgError(
      'network_blocked',
      'Server Instagram tak pahunch nahi pa raha (network blocked/timeout).',
      {
        hint: 'Agar aap isko kisi restricted sandbox/preview environment mein chala rahe hain to wahan Instagram blocked hai. Apne PC pe `npm start` kar ke http://localhost:3000 kholein — wahan ye kaam karega.',
        httpStatus: 502,
        tried,
      }
    );
  }

  if (sawLogin) {
    throw new IgError('login_required', 'Instagram ne login/rate wall dikhaya. Ye post private ho sakta hai.', {
      hint: 'Kisi public reel ke saath try karein. Datacenter IP se Instagram aksar block karta hai — apne local machine se chalana sab se reliable hai.',
      tried,
    });
  }

  throw new IgError('not_found_or_private', 'Is post ke liye koi downloadable media nahi mila.', {
    hint: 'Link sahi hai aur post public hai? Phir bhi fail ho to Instagram ne apna structure badla ho sakta hai — yt-dlp layer (npm install ke sath aata hai) akser phir bhi kaam kar jata hai.',
    tried,
  });
}

/* ------------------------------------------------------------------ */
/* Demo payload (offline preview)                                     */
/* ------------------------------------------------------------------ */

function demoPayload() {
  const item = (n, hue) => ({
    id: `demo-${n}`,
    type: 'image',
    url: `/demo-media/demo-${n}.svg`,
    thumbnail: `/demo-media/demo-${n}.svg`,
    width: 1080,
    height: 1350,
    qualities: [],
  });
  return {
    shortcode: 'DEMO123',
    postUrl: 'demo',
    source: 'demo',
    demo: true,
    author: { username: 'demo.creator' },
    caption:
      'Sample carousel post — yeh offline demo hai. Asli Instagram links us waqt kaam karte hain jab server instagram.com tak pahunch sakta ho (jaise aapke laptop ya VPS pe).',
    thumbnail: '/demo-media/demo-1.svg',
    isVideo: false,
    media: [item(1), item(2), item(3)],
  };
}

module.exports = {
  fetchInstagramMedia,
  parseInstagramUrl,
  demoPayload,
  ytAvailable,
  IgError,
};
