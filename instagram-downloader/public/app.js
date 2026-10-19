'use strict';

/* global document, window, navigator, localStorage, fetch, URL, setTimeout, AbortController */

const $ = (sel) => document.querySelector(sel);

const form = $('#fetchForm');
const urlInput = $('#urlInput');
const fetchBtn = $('#fetchBtn');
const btnLabel = fetchBtn.querySelector('.btn-label');
const spinner = fetchBtn.querySelector('.spinner');
const pasteBtn = $('#pasteBtn');
const clearBtn = $('#clearBtn');
const resultSection = $('#resultSection');
const skeleton = $('#skeleton');
const resultCard = $('#resultCard');
const errorBox = $('#errorBox');
const errorMsg = $('#errorMsg');
const errorHint = $('#errorHint');
const errorDetails = $('#errorDetails');
const errorTried = $('#errorTried');
const historySection = $('#historySection');
const historyList = $('#historyList');
const toastRoot = $('#toastRoot');

const HISTORY_KEY = 'instasaver_history_v1';
let currentAbort = null;

/* ----------------------------- toasts ----------------------------- */
function toast(message, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  const icon = kind === 'error' ? '⚠️' : kind === 'ok' ? '✅' : 'ℹ️';
  el.textContent = `${icon}  ${message}`;
  toastRoot.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 260);
  }, 4200);
}

/* ---------------------------- UI state ---------------------------- */
function setLoading(on) {
  fetchBtn.disabled = on;
  spinner.hidden = !on;
  btnLabel.textContent = on ? 'Fetching…' : 'Fetch video';
  resultSection.hidden = !on && resultCard.hidden ? true : false;
  skeleton.hidden = !on;
  if (on) {
    resultCard.hidden = true;
    resultCard.innerHTML = '';
    hideError();
  } else {
    skeleton.hidden = true;
  }
}

function hideError() {
  errorBox.hidden = true;
  errorHint.hidden = true;
  errorDetails.hidden = true;
  errorTried.innerHTML = '';
}

function showError(err) {
  errorBox.hidden = false;
  errorMsg.textContent = err.message || 'Kuch ghalat ho gaya.';
  if (err.hint) {
    errorHint.hidden = false;
    errorHint.textContent = err.hint;
  }
  if (Array.isArray(err.tried) && err.tried.length) {
    errorDetails.hidden = false;
    err.tried.forEach((t) => {
      const li = document.createElement('li');
      li.textContent = t;
      errorTried.appendChild(li);
    });
  }
  errorBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* --------------------------- history ------------------------------ */
function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function saveHistory(items) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 8)));
  } catch { /* storage full/blocked — ignore */ }
}

function timeAgo(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function renderHistory() {
  const items = loadHistory();
  historySection.hidden = items.length === 0;
  historyList.innerHTML = '';
  items.forEach((item) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'history-chip';
    btn.title = item.url;

    const av = document.createElement('span');
    av.className = 'h-avatar';
    av.textContent = (item.author || item.shortcode || 'i').slice(0, 1).toUpperCase();
    btn.appendChild(av);

    const label = document.createElement('span');
    label.textContent = item.author ? `@${item.author}` : item.shortcode;
    btn.appendChild(label);

    const time = document.createElement('span');
    time.className = 'history-time';
    time.textContent = timeAgo(item.ts);
    btn.appendChild(time);

    btn.addEventListener('click', () => {
      urlInput.value = item.url;
      onInputChange();
      urlInput.focus();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
    historyList.appendChild(btn);
  });
}

function pushHistory(data, originalUrl) {
  const items = loadHistory();
  const entry = {
    url: originalUrl,
    shortcode: data.shortcode || 'media',
    author: data.author && data.author.username,
    ts: Date.now(),
  };
  const next = [entry, ...items.filter((i) => i.url !== entry.url)];
  saveHistory(next);
  renderHistory();
}

$('#clearHistoryBtn').addEventListener('click', () => {
  saveHistory([]);
  renderHistory();
  toast('History clear ho gayi.', 'ok');
});

/* --------------------------- result UI ---------------------------- */
function proxyUrl(mediaUrl, name, index, ext) {
  const p = new URL('/api/download', window.location.origin);
  p.searchParams.set('u', mediaUrl);
  p.searchParams.set('n', name);
  if (index != null) p.searchParams.set('i', String(index));
  if (ext) p.searchParams.set('ext', ext);
  return p.pathname + p.search;
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

function buildMediaItem(m, data, index, total) {
  const wrap = el('article', 'media-item');

  const frame = el('div', 'media-frame');
  if (m.type === 'video') {
    const v = document.createElement('video');
    v.controls = true;
    v.preload = 'metadata';
    v.playsInline = true;
    if (m.thumbnail) v.poster = m.thumbnail;
    const src = document.createElement('source');
    src.src = m.url;
    src.type = 'video/mp4';
    v.appendChild(src);
    frame.appendChild(v);
  } else {
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = m.url;
    img.alt = data.caption ? data.caption.slice(0, 120) : 'Instagram media';
    frame.appendChild(img);
  }
  frame.appendChild(el('span', 'media-type-badge', m.type === 'video' ? '▶ Video' : '📷 Photo'));
  wrap.appendChild(frame);

  const controls = el('div', 'media-controls');
  if (total > 1) controls.appendChild(el('span', 'media-index', `${index + 1} / ${total}`));

  let chosenUrl = m.url;
  let ext = m.type === 'video' ? 'mp4' : 'jpg';
  if (Array.isArray(m.qualities) && m.qualities.length > 1) {
    const select = el('select', 'quality-select');
    select.setAttribute('aria-label', 'Quality');
    m.qualities.forEach((q, qi) => {
      const opt = document.createElement('option');
      opt.value = String(qi);
      opt.textContent = q.height ? `${q.label} · HD${qi === 0 ? ' (best)' : ''}` : q.label;
      select.appendChild(opt);
    });
    select.addEventListener('change', () => {
      const q = m.qualities[Number(select.value)];
      if (q) {
        chosenUrl = q.url;
        dl.href = proxyUrl(chosenUrl, fileBase(), index, ext);
        open.href = chosenUrl;
      }
    });
    controls.appendChild(select);
  }

  const fileBase = () => {
    const who = data.author && data.author.username ? data.author.username : 'instagram';
    const suffix = total > 1 ? `-${index + 1}` : '';
    return `${who}-${data.shortcode || 'media'}${suffix}`;
  };

  const dl = el('a', 'btn btn-download', '⬇ Download');
  dl.href = proxyUrl(chosenUrl, fileBase(), index, ext);
  dl.setAttribute('download', '');
  controls.appendChild(dl);

  const open = el('a', 'open-link', 'Open ↗');
  open.href = chosenUrl;
  open.target = '_blank';
  open.rel = 'noopener noreferrer';
  controls.appendChild(open);

  wrap.appendChild(controls);
  return wrap;
}

function renderResult(data, originalUrl) {
  resultCard.innerHTML = '';

  const head = el('div', 'post-head');
  const avatar = el('div', 'avatar', (data.author && data.author.username ? data.author.username : 'I').slice(0, 1).toUpperCase());
  head.appendChild(avatar);

  const meta = el('div', 'post-meta');
  const author = el('div', 'post-author');
  if (data.author && data.author.username) {
    const a = el('a', null, `@${data.author.username}`);
    a.href = `https://www.instagram.com/${encodeURIComponent(data.author.username)}/`;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    author.appendChild(a);
  } else {
    author.textContent = 'Instagram post';
  }
  meta.appendChild(author);

  const sourceRow = el('div', 'post-source');
  sourceRow.appendChild(el('span', 'chip', data.source || 'scraper'));
  if (data.demo) sourceRow.appendChild(el('span', 'chip demo', 'demo'));
  if (data.isVideo) sourceRow.appendChild(el('span', 'chip video-chip', 'video'));
  sourceRow.appendChild(el('span', null, `${data.media.length} file${data.media.length > 1 ? 's' : ''}`));
  meta.appendChild(sourceRow);
  head.appendChild(meta);
  resultCard.appendChild(head);

  if (data.caption) {
    const cap = el('p', 'caption clamped', data.caption);
    resultCard.appendChild(cap);
    if (data.caption.length > 140) {
      const toggle = el('button', 'link-btn caption-toggle', 'Read more');
      toggle.type = 'button';
      toggle.addEventListener('click', () => {
        const clamped = cap.classList.toggle('clamped');
        toggle.textContent = clamped ? 'Read more' : 'Show less';
      });
      resultCard.appendChild(toggle);
    }
  }

  const grid = el('div', `media-grid${data.media.length > 1 ? ' multi' : ''}`);
  data.media.forEach((m, i) => grid.appendChild(buildMediaItem(m, data, i, data.media.length)));
  resultCard.appendChild(grid);

  if (data.media.length > 1) {
    const actions = el('div', 'result-actions');
    const all = el('button', 'btn btn-ghost btn-sm', `⬇ Download all ${data.media.length} files`);
    all.type = 'button';
    all.addEventListener('click', () => {
      data.media.forEach((m, i) => {
        setTimeout(() => {
          const a = document.createElement('a');
          a.href = proxyUrl(m.url, `${data.author && data.author.username ? data.author.username : 'instagram'}-${data.shortcode}-${i + 1}`, i, m.type === 'video' ? 'mp4' : 'jpg');
          a.setAttribute('download', '');
          document.body.appendChild(a);
          a.click();
          a.remove();
        }, i * 600);
      });
      toast('Downloads shuru ho gaye — browser multiple downloads allow kare.', 'ok');
    });
    actions.appendChild(all);
    resultCard.appendChild(actions);
  }

  resultSection.hidden = false;
  resultCard.hidden = false;
  resultCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  pushHistory(data, originalUrl);
}

/* ---------------------------- fetching ---------------------------- */
async function doFetch(url) {
  if (currentAbort) currentAbort.abort();
  currentAbort = new AbortController();
  setLoading(true);

  try {
    const res = await fetch('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
      signal: currentAbort.signal,
    });
    const json = await res.json().catch(() => null);
    if (!res.ok || !json || !json.ok) {
      const err = (json && json.error) || { message: `Server ne ${res.status} diya.` };
      throw err;
    }
    renderResult(json.data, url);
    if (json.data.demo) {
      toast('Demo data dikhaya ja raha hai (offline preview).', 'ok');
    } else {
      toast('Media mil gaya — download ke liye tayar!', 'ok');
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    showError(e || { message: 'Network error.' });
  } finally {
    setLoading(false);
  }
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const url = urlInput.value.trim();
  if (!url) {
    toast('Pehle Instagram ka link paste karein.', 'error');
    urlInput.focus();
    return;
  }
  if (/^demo\b/i.test(url) || url.includes('instagram.com') || url.includes('instagr.am')) {
    doFetch(url);
  } else {
    toast('Yeh Instagram ka link nahi lag raha.', 'error');
    showError({
      message: 'Sirf instagram.com ke links support hote hain.',
      hint: 'Instagram app mein post pe ⋯ → “Link” copy karein, ya browser se reel ka URL copy karein.',
    });
  }
});

/* --------------------------- input row ---------------------------- */
function onInputChange() {
  clearBtn.hidden = urlInput.value.length === 0;
}
urlInput.addEventListener('input', onInputChange);

clearBtn.addEventListener('click', () => {
  urlInput.value = '';
  onInputChange();
  urlInput.focus();
});

pasteBtn.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text) {
      toast('Clipboard khaali hai.', 'error');
      return;
    }
    urlInput.value = text.trim();
    onInputChange();
    toast('Link paste ho gaya.', 'ok');
    if (urlInput.value.includes('instagram.com') || /^demo/i.test(urlInput.value)) {
      form.requestSubmit();
    }
  } catch {
    toast('Clipboard access nahi mila — khud paste karein (Ctrl+V).', 'error');
    urlInput.focus();
  }
});

/* --------------------------- demo mode ---------------------------- */
function runDemo() {
  urlInput.value = 'demo';
  onInputChange();
  doFetch('demo');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
$('#demoHeaderBtn').addEventListener('click', runDemo);
$('#demoHeroLink').addEventListener('click', runDemo);
$('#demoErrorBtn').addEventListener('click', runDemo);

/* ----------------------------- init ------------------------------- */
renderHistory();

// health check → badge shows engine capability
fetch('/api/health')
  .then((r) => r.json())
  .then((h) => {
    if (h && h.ytDlp) {
      $('#engineBadge').innerHTML = '<span class="pulse-dot"></span> HD engine ready · Reels · Videos · Photos · Carousels';
    }
  })
  .catch(() => { /* badge stays as-is */ });
