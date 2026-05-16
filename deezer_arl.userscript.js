// ==UserScript==
// @name         Streamrippa Deezer ARL Extractor
// @namespace    local.streamrippa.deezer-arl
// @version      1.0.0
// @description  Locally displays the Deezer arl cookie and Streamrip-ready config. No network upload, no external dependencies. You must enable 'all cookies' (temporarily) in tampermonkey, for this to work.
// @author       local
// @match        https://www.deezer.com/*
// @match        https://deezer.com/*
// @match        https://*.deezer.com/*
// @run-at       document-start
// @grant        GM_cookie
// @grant        GM.cookie
// @grant        GM_setClipboard
// @grant        GM.setClipboard
// @grant        GM_registerMenuCommand
// ==/UserScript==

(() => {
  'use strict';

  const SCRIPT_NAME = 'Streamrippa Deezer ARL Extractor';
  const LOG_PREFIX = '[Streamrippa/Deezer ARL]';
  const BANNER_ID = 'streamrippa-deezer-arl-banner';
  const STYLE_ID = 'streamrippa-deezer-arl-style';

  let lastArl = '';
  let lastSource = '';
  let scanTimer = null;
  let bannerVisible = true;

  const safeLog = (...args) => {
    try { console.log(LOG_PREFIX, ...args); } catch (_) {}
  };

  const mask = (value) => {
    if (!value) return '';
    if (value.length <= 20) return `${value.slice(0, 4)}…${value.slice(-4)}`;
    return `${value.slice(0, 10)}…${value.slice(-10)}`;
  };

  const looksLikeArl = (value) => {
    if (typeof value !== 'string') return false;
    const v = value.trim();
    // Common ARLs are roughly 192 chars, but allow some drift.
    return /^[A-Za-z0-9_-]{80,260}$/.test(v);
  };

  const escapeHtml = (text) => String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  const tomlFor = (arl) => `[deezer]\narl = "${String(arl).replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"\n`;

  const parseCookieString = (cookieString) => {
    const out = new Map();
    if (!cookieString || typeof cookieString !== 'string') return out;
    for (const part of cookieString.split(';')) {
      const idx = part.indexOf('=');
      if (idx < 0) continue;
      const key = part.slice(0, idx).trim();
      const val = part.slice(idx + 1).trim();
      if (!key) continue;
      try {
        out.set(key, decodeURIComponent(val));
      } catch (_) {
        out.set(key, val);
      }
    }
    return out;
  };

  const readFromDocumentCookie = () => {
    try {
      const cookies = parseCookieString(document.cookie || '');
      const value = cookies.get('arl') || '';
      return looksLikeArl(value) ? { value, source: 'document.cookie' } : null;
    } catch (err) {
      safeLog('document.cookie scan failed:', err);
      return null;
    }
  };

  const gmCookieListViaPromise = async (details) => {
    if (typeof GM !== 'undefined' && GM.cookie && typeof GM.cookie.list === 'function') {
      return await GM.cookie.list(details);
    }
    return null;
  };

  const gmCookieListViaCallback = (details) => new Promise((resolve) => {
    try {
      if (typeof GM_cookie === 'undefined' || !GM_cookie) return resolve(null);

      // Modern Tampermonkey: GM_cookie.list(details, cb)
      if (typeof GM_cookie.list === 'function') {
        GM_cookie.list(details, (cookies, error) => {
          if (error) {
            safeLog('GM_cookie.list error:', error);
            resolve(null);
          } else {
            resolve(cookies || []);
          }
        });
        return;
      }

      // Older compatibility style: GM_cookie('list', details, cb)
      if (typeof GM_cookie === 'function') {
        GM_cookie('list', details, (cookies, error) => {
          if (error) {
            safeLog('GM_cookie(list) error:', error);
            resolve(null);
          } else {
            resolve(cookies || []);
          }
        });
        return;
      }
    } catch (err) {
      safeLog('GM_cookie callback scan failed:', err);
    }
    resolve(null);
  });

  const cookieCandidateUrls = () => {
    const urls = new Set();
    urls.add(window.location.href);
    urls.add('https://www.deezer.com/');
    urls.add('https://deezer.com/');
    urls.add('https://www.deezer.com/us/');
    urls.add('https://www.deezer.com/en/');
    return [...urls];
  };

  const readFromGmCookie = async () => {
    const detailSets = [];
    for (const url of cookieCandidateUrls()) {
      detailSets.push({ url, name: 'arl' });
      detailSets.push({ url, name: 'arl', partitionKey: {} });
    }
    detailSets.push({ name: 'arl' });

    for (const details of detailSets) {
      try {
        let cookies = await gmCookieListViaPromise(details);
        if (!Array.isArray(cookies)) cookies = await gmCookieListViaCallback(details);
        if (!Array.isArray(cookies)) continue;

        const match = cookies.find((c) => c && c.name === 'arl' && looksLikeArl(c.value));
        if (match) {
          const attrs = [];
          if (match.httpOnly) attrs.push('HttpOnly');
          if (match.secure) attrs.push('Secure');
          if (match.domain) attrs.push(match.domain);
          return {
            value: match.value,
            source: `GM_cookie${attrs.length ? ` (${attrs.join(', ')})` : ''}`,
          };
        }
      } catch (err) {
        safeLog('GM_cookie scan failed for', details, err);
      }
    }
    return null;
  };

  const readFromPageState = () => {
    // Low priority fallback. Some Deezer pages have bootstrapped account data, but not always ARL.
    try {
      const haystacks = [];
      for (const storage of [window.localStorage, window.sessionStorage]) {
        if (!storage) continue;
        for (let i = 0; i < storage.length; i += 1) {
          const key = storage.key(i);
          if (!key) continue;
          const val = storage.getItem(key);
          if (key.toLowerCase() === 'arl' && looksLikeArl(val)) {
            return { value: val.trim(), source: `${storage === localStorage ? 'localStorage' : 'sessionStorage'}.arl` };
          }
          haystacks.push(`${key}=${val}`);
        }
      }
      for (const text of haystacks) {
        const match = /(?:^|[^A-Za-z0-9_-])arl["'\s:=]+([A-Za-z0-9_-]{80,260})/i.exec(text);
        if (match && looksLikeArl(match[1])) {
          return { value: match[1], source: 'browser storage fallback' };
        }
      }
    } catch (err) {
      safeLog('page-state scan failed:', err);
    }
    return null;
  };

  const scanForArl = async () => {
    const found = readFromDocumentCookie() || await readFromGmCookie() || readFromPageState();
    if (found && found.value) {
      lastArl = found.value;
      lastSource = found.source;
      safeLog(`ARL found via ${found.source}:`, mask(found.value));
      updateBanner();
      return found;
    }
    updateBanner();
    return null;
  };

  const copyText = async (text) => {
    try {
      if (typeof GM !== 'undefined' && typeof GM.setClipboard === 'function') {
        await GM.setClipboard(text, 'text');
        return true;
      }
    } catch (err) {
      safeLog('GM.setClipboard failed:', err);
    }
    try {
      if (typeof GM_setClipboard === 'function') {
        GM_setClipboard(text, 'text');
        return true;
      }
    } catch (err) {
      safeLog('GM_setClipboard failed:', err);
    }
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      safeLog('navigator.clipboard failed:', err);
    }
    return false;
  };

  const injectStyle = () => {
    if (document.getElementById(STYLE_ID)) return;
    const css = `
      #${BANNER_ID} {
        position: fixed;
        z-index: 2147483647;
        left: 12px;
        bottom: 12px;
        max-width: min(760px, calc(100vw - 24px));
        box-sizing: border-box;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 13px;
        line-height: 1.35;
        color: #f7f7f7;
        background: rgba(20, 22, 26, 0.97);
        border: 1px solid rgba(255,255,255,0.18);
        border-radius: 12px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.35);
        padding: 12px;
      }
      #${BANNER_ID}[hidden] { display: none !important; }
      #${BANNER_ID} .srda-title { font-weight: 700; margin-bottom: 4px; }
      #${BANNER_ID} .srda-muted { opacity: 0.78; }
      #${BANNER_ID} .srda-token {
        margin-top: 8px;
        padding: 8px;
        border-radius: 8px;
        background: rgba(255,255,255,0.08);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        user-select: text;
      }
      #${BANNER_ID} .srda-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 10px;
      }
      #${BANNER_ID} button {
        appearance: none;
        border: 1px solid rgba(255,255,255,0.22);
        border-radius: 999px;
        background: rgba(255,255,255,0.10);
        color: #fff;
        padding: 5px 10px;
        cursor: pointer;
        font: inherit;
      }
      #${BANNER_ID} button:hover { background: rgba(255,255,255,0.18); }
    `;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  };

  const getBanner = () => document.getElementById(BANNER_ID);

  const ensureBanner = () => {
    if (!document.documentElement) return false;
    injectStyle();

    let el = getBanner();
    if (!el) {
      el = document.createElement('div');
      el.id = BANNER_ID;
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
      el.addEventListener('click', async (event) => {
        const button = event.target && event.target.closest ? event.target.closest('button[data-action]') : null;
        if (!button) return;
        const action = button.getAttribute('data-action');
        if (action === 'scan') {
          button.textContent = 'Scanning…';
          await scanForArl();
        } else if (action === 'copy-arl' && lastArl) {
          button.textContent = await copyText(lastArl) ? 'Copied ARL ✓' : 'Copy failed';
        } else if (action === 'copy-toml' && lastArl) {
          button.textContent = await copyText(tomlFor(lastArl)) ? 'Copied TOML ✓' : 'Copy failed';
        } else if (action === 'hide') {
          bannerVisible = false;
          el.hidden = true;
        }
        setTimeout(updateBanner, 900);
      });
      (document.body || document.documentElement).appendChild(el);
    }
    el.hidden = !bannerVisible;
    updateBanner();
    return true;
  };

  const updateBanner = () => {
    const el = getBanner();
    if (!el) return;

    const hasGM = (typeof GM_cookie !== 'undefined') || (typeof GM !== 'undefined' && GM.cookie && typeof GM.cookie.list === 'function');
    const hasDocCookie = (() => {
      try { return typeof document.cookie === 'string'; } catch (_) { return false; }
    })();

    if (lastArl) {
      el.innerHTML = `
        <div class="srda-title">✅ Streamrippa — Deezer ARL found</div>
        <div class="srda-muted">Source: ${escapeHtml(lastSource || 'unknown')} · Length: ${escapeHtml(String(lastArl.length))}</div>
        <div class="srda-token">${escapeHtml(tomlFor(lastArl))}</div>
        <div class="srda-muted">Masked ARL: ${escapeHtml(mask(lastArl))}</div>
        <div class="srda-actions">
          <button data-action="copy-toml" type="button">Copy Streamrip TOML</button>
          <button data-action="copy-arl" type="button">Copy ARL only</button>
          <button data-action="scan" type="button">Re-scan</button>
          <button data-action="hide" type="button">Hide</button>
        </div>
      `;
    } else {
      el.innerHTML = `
        <div class="srda-title">⏳ Streamrippa — waiting for Deezer ARL</div>
        <div class="srda-muted">Log into Deezer in this tab, then press Scan now if needed.</div>
        <div class="srda-token">document.cookie: ${hasDocCookie ? 'available' : 'unavailable'}\nGM_cookie: ${hasGM ? 'available' : 'not available/disabled'}\nExpected cookie: arl</div>
        <div class="srda-actions">
          <button data-action="scan" type="button">Scan now</button>
          <button data-action="hide" type="button">Hide</button>
        </div>
      `;
    }
  };

  const scheduleBannerUntilBodyExists = () => {
    const tryNow = () => {
      if (ensureBanner()) return true;
      return false;
    };
    if (tryNow()) return;

    const observer = new MutationObserver(() => {
      if (tryNow()) observer.disconnect();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });

    document.addEventListener('DOMContentLoaded', () => {
      tryNow();
      scanForArl();
    }, { once: true });
  };

  const keepAlive = () => {
    if (!bannerVisible) return;
    if (!getBanner()) ensureBanner();
  };

  const startScanning = () => {
    scanForArl();
    scanTimer = window.setInterval(async () => {
      keepAlive();
      if (!lastArl) await scanForArl();
    }, 2500);

    window.addEventListener('focus', () => scanForArl());
    window.addEventListener('pageshow', () => scanForArl());
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) scanForArl();
    });
  };

  try {
    if (typeof GM_registerMenuCommand === 'function') {
      GM_registerMenuCommand('Scan Deezer ARL now', () => scanForArl());
      GM_registerMenuCommand('Copy Deezer ARL', async () => {
        await scanForArl();
        if (lastArl) await copyText(lastArl);
      });
      GM_registerMenuCommand('Copy Streamrip Deezer TOML', async () => {
        await scanForArl();
        if (lastArl) await copyText(tomlFor(lastArl));
      });
      GM_registerMenuCommand('Show Deezer ARL banner', () => {
        bannerVisible = true;
        ensureBanner();
        scanForArl();
      });
    }
  } catch (err) {
    safeLog('menu registration failed:', err);
  }

  safeLog(`${SCRIPT_NAME} loaded on ${location.href}`);
  scheduleBannerUntilBodyExists();
  startScanning();

  window.addEventListener('beforeunload', () => {
    if (scanTimer) window.clearInterval(scanTimer);
  });
})();
