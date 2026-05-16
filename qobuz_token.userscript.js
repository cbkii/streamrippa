// ==UserScript==
// @name         Streamrippa — Qobuz Token Extractor
// @namespace    https://github.com/cbkii/streamrippa
// @version      2.1.0
// @description  Extracts Qobuz user id/auth token to easy modal in-page
// @author       cbkii/streamrippa
// @match        https://*.qobuz.com/*
// @match        http://*.qobuz.com/*
// @run-at       document-start
// @grant        unsafeWindow
// ==/UserScript==

(function streamrippaQobuzTokenHybrid() {
  'use strict';

  const VERSION = '2.1.0';
  const NAME = 'Streamrippa/Qobuz token';
  const BANNER_ID = 'streamrippa-qobuz-token-banner';
  const STYLE_ATTR = 'data-streamrippa-qobuz-style';
  const EVENT_TYPE = 'streamrippa-qobuz-token-event';
  const MESSAGE_SOURCE = 'streamrippa-qobuz-token-pagehook';
  const LS_PREFIX = 'streamrippa:qobuz:';
  const SAVE_KEYS = {
    userId: LS_PREFIX + 'user_id',
    token: LS_PREFIX + 'user_auth_token',
    appId: LS_PREFIX + 'app_id',
    source: LS_PREFIX + 'source',
    capturedAt: LS_PREFIX + 'captured_at',
    hidden: LS_PREFIX + 'banner_hidden',
  };

  const state = {
    startedAt: new Date().toISOString(),
    uiReady: false,
    closed: false,
    minimised: false,
    injectedHook: false,
    unsafeHook: false,
    fetchPatched: false,
    xhrPatched: false,
    requestPatched: false,
    lastScan: '',
    lastError: '',
    counters: {
      renders: 0,
      scans: 0,
      storageHits: 0,
      headerHits: 0,
      responseHits: 0,
      messages: 0,
      pageEvents: 0,
    },
  };

  function log(...args) { try { console.info('[' + NAME + ']', ...args); } catch (_) {} }
  function warn(...args) { try { console.warn('[' + NAME + ']', ...args); } catch (_) {} }

  function isQobuz() {
    return /(^|\.)qobuz\.com$/i.test(location.hostname);
  }

  function nowIso() { return new Date().toISOString(); }

  function safeLocalGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }
  function safeLocalSet(key, value) {
    try { window.localStorage.setItem(key, String(value)); return true; } catch (err) { state.lastError = 'localStorage set failed: ' + shortErr(err); return false; }
  }
  function safeLocalRemove(key) {
    try { window.localStorage.removeItem(key); } catch (_) {}
  }

  function loadSaved() {
    return {
      userId: safeLocalGet(SAVE_KEYS.userId) || '',
      token: safeLocalGet(SAVE_KEYS.token) || '',
      appId: safeLocalGet(SAVE_KEYS.appId) || '',
      source: safeLocalGet(SAVE_KEYS.source) || '',
      capturedAt: safeLocalGet(SAVE_KEYS.capturedAt) || '',
    };
  }

  function saveCapture(input) {
    const token = normaliseToken(input && input.token);
    if (!token) return false;

    const existing = loadSaved();
    const userId = normaliseUserId(input && input.userId) || existing.userId || findUserIdFromStorage() || '';
    const appId = normaliseAppId(input && input.appId) || existing.appId || '';
    const source = String((input && input.source) || 'unknown').slice(0, 180);
    const capturedAt = nowIso();

    safeLocalSet(SAVE_KEYS.token, token);
    safeLocalSet(SAVE_KEYS.userId, userId);
    safeLocalSet(SAVE_KEYS.source, source);
    safeLocalSet(SAVE_KEYS.capturedAt, capturedAt);
    if (appId) safeLocalSet(SAVE_KEYS.appId, appId);

    if (/header/i.test(source)) state.counters.headerHits += 1;
    else if (/response/i.test(source)) state.counters.responseHits += 1;
    else if (/storage|localuser|sessionStorage|localStorage/i.test(source)) state.counters.storageHits += 1;

    log('captured', { source, userId: userId || '(none)', token: redact(token), appId: appId || '(none)' });
    renderBanner(false);
    return true;
  }

  function normaliseToken(value) {
    if (value == null) return '';
    let token = String(value).trim();
    token = token.replace(/^Bearer\s+/i, '').replace(/^['"]|['"]$/g, '').trim();
    if (!token || token.length < 12 || token.length > 4096) return '';
    if (/^(null|undefined|true|false|none|anonymous)$/i.test(token)) return '';
    if (/\s/.test(token)) return '';
    if (!/^[A-Za-z0-9._~+\/=:-]+$/.test(token)) return '';
    return token;
  }

  function normaliseUserId(value) {
    if (value == null) return '';
    const id = String(value).trim().replace(/^['"]|['"]$/g, '');
    return /^\d{2,}$/.test(id) ? id : '';
  }

  function normaliseAppId(value) {
    if (value == null) return '';
    const s = String(value).trim().replace(/^['"]|['"]$/g, '');
    if (/^\d{6,}$/.test(s) || /^[A-Za-z0-9._-]{8,100}$/.test(s)) return s;
    return '';
  }

  function redact(token) {
    token = String(token || '');
    if (!token) return '';
    if (token.length < 18) return '••••';
    return token.slice(0, 8) + '…' + token.slice(-6);
  }

  function scanAll(reason) {
    state.counters.scans += 1;
    state.lastScan = nowIso() + ' · ' + (reason || 'scan');
    let found = false;
    try { found = scanStorage(reason) || found; } catch (err) { state.lastError = 'scanStorage: ' + shortErr(err); }
    try { found = scanCookies(reason) || found; } catch (_) {}
    try { found = scanPageScripts(reason) || found; } catch (_) {}
    renderBanner(false);
    return found;
  }

  function scanStorage(reason) {
    let found = false;
    for (const storageName of ['localStorage', 'sessionStorage']) {
      let store;
      try { store = window[storageName]; } catch (_) { continue; }
      if (!store) continue;

      // Fast, known-good path: Qobuz Web Player localStorage.localuser.
      for (const knownKey of ['localuser', 'localUser', 'user', 'qobuz:user', 'persist:user']) {
        try {
          const raw = store.getItem(knownKey);
          if (raw) found = inspectTextOrJson(raw, storageName + '.' + knownKey, reason) || found;
        } catch (_) {}
      }

      let len = 0;
      try { len = Math.min(store.length || 0, 1000); } catch (_) { continue; }
      for (let i = 0; i < len; i += 1) {
        let key = '', raw = '';
        try { key = store.key(i); raw = store.getItem(key); } catch (_) { continue; }
        if (!key || !raw) continue;
        if (!/token|auth|user|local|qobuz|credential|session|persist|app/i.test(key + ' ' + raw.slice(0, 500))) continue;
        found = inspectTextOrJson(raw, storageName + '.' + key, reason) || found;
      }
    }
    return found;
  }

  function scanCookies(reason) {
    let cookie = '';
    try { cookie = document.cookie || ''; } catch (_) { cookie = ''; }
    return cookie ? inspectTextOrJson(cookie, 'document.cookie', reason) : false;
  }

  function scanPageScripts(reason) {
    if (!document || !document.scripts) return false;
    const chunks = [];
    try {
      for (const script of Array.from(document.scripts).slice(0, 300)) {
        const txt = script.textContent || '';
        if (/user_auth_token|x-user-auth-token|localuser|"token"\s*:|app_id|user_id/i.test(txt)) chunks.push(txt.slice(0, 1200000));
      }
    } catch (_) {}
    return chunks.length ? inspectTextOrJson(chunks.join('\n'), 'embedded page scripts', reason) : false;
  }

  function inspectTextOrJson(raw, source, reason) {
    let found = false;
    const text = String(raw || '');
    if (!text) return false;

    const parsed = tryParseJson(text);
    if (parsed !== null) found = extractFromAny(parsed, source, reason) || found;

    found = extractByRegex(text, source, reason) || found;
    return found;
  }

  function tryParseJson(raw) {
    const s = String(raw || '').trim();
    if (!s) return null;
    const attempts = [s];
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
      attempts.push(s.slice(1, -1).replace(/\\"/g, '"').replace(/\\'/g, "'"));
    }
    for (const attempt of attempts) {
      try { return JSON.parse(attempt); } catch (_) {}
    }
    return null;
  }

  function extractFromAny(value, source, reason) {
    const seen = new WeakSet();
    const hits = [];
    let nodes = 0;
    function visit(node, path, parent) {
      if (nodes++ > 8000 || node == null) return;
      if (typeof node === 'string') {
        const s = node.trim();
        if ((s.startsWith('{') || s.startsWith('[') || s.startsWith('"{')) && s.length < 1600000) {
          const parsed = tryParseJson(s);
          if (parsed !== null) visit(parsed, path + '{json}', parent);
        }
        return;
      }
      if (typeof node !== 'object') return;
      if (seen.has(node)) return;
      seen.add(node);

      const hit = objectHit(node, path, parent);
      if (hit) hits.push(hit);

      if (Array.isArray(node)) {
        for (let i = 0; i < Math.min(node.length, 400); i += 1) visit(node[i], path + '[' + i + ']', node);
      } else {
        for (const key of Object.keys(node).slice(0, 700)) visit(node[key], path ? path + '.' + key : key, node);
      }
    }
    visit(value, '', null);

    hits.sort((a, b) => b.score - a.score);
    let saved = false;
    for (const h of hits) {
      saved = saveCapture({ token: h.token, userId: h.userId, appId: h.appId, source: source + (h.path ? ' @ ' + h.path : '') }) || saved;
      if (saved) break;
    }
    return saved;
  }

  function objectHit(obj, path, parent) {
    const keys = Object.keys(obj);
    const low = new Map(keys.map(k => [String(k).toLowerCase(), k]));
    const get = (...names) => {
      for (const name of names) {
        const k = low.get(String(name).toLowerCase());
        if (k != null) return obj[k];
      }
      return undefined;
    };

    const token = normaliseToken(get('user_auth_token', 'userAuthToken', 'auth_token', 'authToken', 'x-user-auth-token', 'xUserAuthToken', 'token'));
    if (!token) return null;

    let userId = normaliseUserId(get('id', 'user_id', 'userId', 'userid'));
    let appId = normaliseAppId(get('app_id', 'appId', 'x-app-id', 'xAppId'));

    if (!userId && obj.user && typeof obj.user === 'object') userId = normaliseUserId(obj.user.id || obj.user.user_id || obj.user.userId);
    if (!userId && obj.credential && typeof obj.credential === 'object') userId = normaliseUserId(obj.credential.id || obj.credential.user_id || obj.credential.userId);
    if (!userId && parent && typeof parent === 'object') userId = normaliseUserId(parent.id || parent.user_id || parent.userId || (parent.user && parent.user.id));
    if (!appId && parent && typeof parent === 'object') appId = normaliseAppId(parent.app_id || parent.appId || parent['x-app-id']);

    let score = 0;
    const p = String(path || '').toLowerCase();
    if (p.includes('localuser')) score += 100;
    if (p.includes('user_auth_token')) score += 80;
    if (p.endsWith('.token') || p.includes('token')) score += 50;
    if (userId) score += 50;
    if (appId) score += 10;
    if (token.length > 32) score += 10;

    return { token, userId, appId, path, score };
  }

  function extractByRegex(text, source, reason) {
    const ids = [];
    const apps = [];
    let m;
    const idRe = /["'](?:user[_-]?id|userid|id)["']\s*:\s*["']?(\d{2,})["']?/ig;
    while ((m = idRe.exec(text))) { const id = normaliseUserId(m[1]); if (id) ids.push(id); }
    const appRe = /["'](?:x-app-id|app_id|appId)["']\s*[:=]\s*["']?([^"',}\s]{6,100})["']?/ig;
    while ((m = appRe.exec(text))) { const app = normaliseAppId(m[1]); if (app) apps.push(app); }

    const tokenRes = [
      /["']user_auth_token["']\s*:\s*["']([^"']{12,4096})["']/ig,
      /["']x-user-auth-token["']\s*:\s*["']([^"']{12,4096})["']/ig,
      /["']auth[_-]?token["']\s*:\s*["']([^"']{12,4096})["']/ig,
      /["']token["']\s*:\s*["']([^"']{12,4096})["']/ig,
      /\bx-user-auth-token\b\s*[:=]\s*([^\s"'&,;<>]{12,4096})/ig,
      /\buser_auth_token\b\s*[:=]\s*([^\s"'&,;<>]{12,4096})/ig,
    ];

    let found = false;
    for (const re of tokenRes) {
      while ((m = re.exec(text))) {
        const token = normaliseToken(m[1]);
        if (!token) continue;
        found = saveCapture({ token, userId: ids[0] || findUserIdFromStorage(), appId: apps[0], source: source + ' regex' }) || found;
      }
    }
    return found;
  }

  function findUserIdFromStorage() {
    const saved = normaliseUserId(safeLocalGet(SAVE_KEYS.userId));
    if (saved) return saved;
    for (const storageName of ['localStorage', 'sessionStorage']) {
      let store;
      try { store = window[storageName]; } catch (_) { continue; }
      if (!store) continue;
      for (const key of ['localuser', 'localUser', 'user', 'qobuz:user', 'persist:user']) {
        let raw = '';
        try { raw = store.getItem(key) || ''; } catch (_) {}
        const id = raw && extractId(raw);
        if (id) return id;
      }
    }
    return '';
  }

  function extractId(raw) {
    const parsed = tryParseJson(raw);
    if (parsed && typeof parsed === 'object') {
      const found = findIdDeep(parsed);
      if (found) return found;
    }
    const m = String(raw || '').match(/["'](?:user[_-]?id|userid|id)["']\s*:\s*["']?(\d{2,})/i);
    return m ? normaliseUserId(m[1]) : '';
  }

  function findIdDeep(value) {
    const seen = new WeakSet();
    let nodes = 0;
    function visit(node) {
      if (!node || typeof node !== 'object' || nodes++ > 1500) return '';
      if (seen.has(node)) return '';
      seen.add(node);
      const id = normaliseUserId(node.id || node.user_id || node.userId || node.userid);
      if (id) return id;
      for (const k of Object.keys(node).slice(0, 250)) {
        const found = visit(node[k]);
        if (found) return found;
      }
      return '';
    }
    return visit(value);
  }

  function headersToPairs(headersLike) {
    const pairs = [];
    if (!headersLike) return pairs;
    try {
      const H = getPageWindow().Headers || window.Headers;
      if (H && headersLike instanceof H && typeof headersLike.forEach === 'function') {
        headersLike.forEach((v, k) => pairs.push([k, v]));
        return pairs;
      }
    } catch (_) {}
    if (Array.isArray(headersLike)) {
      for (const item of headersLike) if (Array.isArray(item) && item.length >= 2) pairs.push([item[0], item[1]]);
      return pairs;
    }
    if (typeof headersLike === 'object') {
      try { for (const k of Object.keys(headersLike)) pairs.push([k, headersLike[k]]); } catch (_) {}
    }
    return pairs;
  }

  function inspectHeaders(headersLike, source, detail) {
    const pairs = headersToPairs(headersLike);
    if (!pairs.length) return false;
    let token = '', userId = '', appId = '';
    for (const [k0, v0] of pairs) {
      const k = String(k0 || '').toLowerCase();
      const v = String(v0 || '').trim();
      if (!v) continue;
      if (k === 'x-user-auth-token' || k === 'user_auth_token' || k === 'user-auth-token') token = normaliseToken(v) || token;
      if (k === 'x-user-id' || k === 'user_id' || k === 'userid') userId = normaliseUserId(v) || userId;
      if (k === 'x-app-id' || k === 'app_id' || k === 'appid') appId = normaliseAppId(v) || appId;
    }
    if (token) return saveCapture({ token, userId, appId, source: source + (detail ? ' ' + detail : '') });
    if (appId) { safeLocalSet(SAVE_KEYS.appId, appId); renderBanner(false); }
    return false;
  }

  function getUrl(input) {
    try {
      if (typeof input === 'string') return input;
      if (input && typeof input.url === 'string') return input.url;
      if (input instanceof URL) return input.href;
      return String(input || '');
    } catch (_) { return ''; }
  }

  function shouldInspectUrl(url) {
    if (!url) return true;
    try {
      const u = new URL(url, location.href);
      return /(^|\.)qobuz\.com$/i.test(u.hostname) || /\/api(?:\.json)?\//i.test(u.pathname) || /user\/login|album\/get|track\/get|playlist\/get|favorite|search\/get/i.test(u.href);
    } catch (_) {
      return /qobuz|user\/login|album\/get|track\/get|playlist\/get|favorite|search\/get/i.test(String(url));
    }
  }

  function getPageWindow() {
    try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow) return unsafeWindow; } catch (_) {}
    return window;
  }

  function installUnsafeWindowHooks() {
    let page;
    try { page = getPageWindow(); } catch (_) { return false; }
    if (!page || page.__streamrippaQobuzHookInstalled) return Boolean(page && page.__streamrippaQobuzHookInstalled);

    try { patchWindowFetch(page); } catch (err) { warn('unsafeWindow fetch patch failed', err); }
    try { patchWindowXHR(page); } catch (err) { warn('unsafeWindow XHR patch failed', err); }
    try { patchWindowRequest(page); } catch (err) { warn('unsafeWindow Request patch failed', err); }

    try { page.__streamrippaQobuzHookInstalled = true; } catch (_) {}
    state.unsafeHook = true;
    return true;
  }

  function patchWindowFetch(page) {
    if (!page.fetch || page.fetch.__streamrippaPatched) { state.fetchPatched = Boolean(page.fetch && page.fetch.__streamrippaPatched); return; }
    const original = page.fetch;
    const patched = async function patchedFetch(input, init) {
      const url = getUrl(input);
      try {
        if (shouldInspectUrl(url)) {
          if (input && input.headers) inspectHeaders(input.headers, 'fetch request headers', url);
          if (init && init.headers) inspectHeaders(init.headers, 'fetch init headers', url);
        }
      } catch (_) {}
      const res = await original.apply(this, arguments);
      try {
        const rurl = url || getUrl(res && res.url);
        if (shouldInspectUrl(rurl) && res && typeof res.clone === 'function') {
          const clone = res.clone();
          const ct = String(clone.headers && clone.headers.get && clone.headers.get('content-type') || '');
          if (/json|text|javascript|html|plain/i.test(ct) || /api|login|user|album|get|track|playlist|favorite/i.test(rurl)) {
            clone.text().then(txt => {
              if (txt && txt.length < 5000000) inspectTextOrJson(txt, 'fetch response body', rurl);
            }).catch(() => {});
          }
        }
      } catch (_) {}
      return res;
    };
    try { Object.defineProperty(patched, '__streamrippaPatched', { value: true }); } catch (_) { patched.__streamrippaPatched = true; }
    page.fetch = patched;
    state.fetchPatched = page.fetch === patched || Boolean(page.fetch && page.fetch.__streamrippaPatched);
  }

  function patchWindowRequest(page) {
    if (!page.Request || page.Request.__streamrippaPatched) { state.requestPatched = Boolean(page.Request && page.Request.__streamrippaPatched); return; }
    const Original = page.Request;
    function PatchedRequest(input, init) {
      const url = getUrl(input);
      try {
        if (shouldInspectUrl(url)) {
          if (input && input.headers) inspectHeaders(input.headers, 'Request input headers', url);
          if (init && init.headers) inspectHeaders(init.headers, 'Request init headers', url);
        }
      } catch (_) {}
      return new Original(input, init);
    }
    try { PatchedRequest.prototype = Original.prototype; Object.setPrototypeOf(PatchedRequest, Original); Object.defineProperty(PatchedRequest, '__streamrippaPatched', { value: true }); } catch (_) { PatchedRequest.__streamrippaPatched = true; }
    page.Request = PatchedRequest;
    state.requestPatched = page.Request === PatchedRequest || Boolean(page.Request && page.Request.__streamrippaPatched);
  }

  function patchWindowXHR(page) {
    if (!page.XMLHttpRequest || page.XMLHttpRequest.__streamrippaPatched) { state.xhrPatched = Boolean(page.XMLHttpRequest && page.XMLHttpRequest.__streamrippaPatched); return; }
    const OriginalXHR = page.XMLHttpRequest;
    function PatchedXHR() {
      const xhr = new OriginalXHR();
      let method = '', url = '';
      const headers = [];
      const open = xhr.open;
      const setRequestHeader = xhr.setRequestHeader;
      xhr.open = function patchedOpen(m, u) { method = String(m || ''); url = getUrl(u); return open.apply(xhr, arguments); };
      xhr.setRequestHeader = function patchedSetHeader(k, v) { try { headers.push([k, v]); inspectHeaders([[k, v]], 'XHR request header', url); } catch (_) {} return setRequestHeader.apply(xhr, arguments); };
      xhr.addEventListener('readystatechange', function () {
        if (xhr.readyState !== 4 || !shouldInspectUrl(url)) return;
        try { inspectHeaders(headers, 'XHR request headers', method + ' ' + url); } catch (_) {}
        try { const txt = xhr.responseText; if (txt && txt.length < 5000000) inspectTextOrJson(txt, 'XHR response body', method + ' ' + url); } catch (_) {}
      });
      return xhr;
    }
    try { PatchedXHR.prototype = OriginalXHR.prototype; for (const k of ['UNSENT','OPENED','HEADERS_RECEIVED','LOADING','DONE']) if (k in OriginalXHR) Object.defineProperty(PatchedXHR, k, { value: OriginalXHR[k] }); Object.defineProperty(PatchedXHR, '__streamrippaPatched', { value: true }); } catch (_) { PatchedXHR.__streamrippaPatched = true; }
    page.XMLHttpRequest = PatchedXHR;
    state.xhrPatched = page.XMLHttpRequest === PatchedXHR || Boolean(page.XMLHttpRequest && page.XMLHttpRequest.__streamrippaPatched);
  }

  function installInjectedPageHook() {
    // Best-effort fallback: inject a tiny page-world hook that posts captured headers/bodies back.
    // If Qobuz CSP blocks script-tag injection, the DOM banner and unsafeWindow/storage paths still work.
    if (state.injectedHook || !document || !document.documentElement) return false;
    const code = '(' + pageHookSource.toString() + ')(' + JSON.stringify({ eventType: EVENT_TYPE, messageSource: MESSAGE_SOURCE }) + ');';
    const script = document.createElement('script');
    script.textContent = code;
    script.setAttribute('data-streamrippa-qobuz-pagehook', VERSION);
    try {
      (document.head || document.documentElement).appendChild(script);
      script.remove();
      state.injectedHook = true;
      return true;
    } catch (err) {
      state.lastError = 'page hook injection failed: ' + shortErr(err);
      return false;
    }
  }

  function pageHookSource(config) {
    if (window.__streamrippaQobuzPageHookInstalled) return;
    window.__streamrippaQobuzPageHookInstalled = true;

    function post(payload) {
      payload = payload || {};
      payload.source = config.messageSource;
      try { window.postMessage(payload, location.origin); } catch (_) {}
      try { window.dispatchEvent(new CustomEvent(config.eventType, { detail: payload })); } catch (_) {}
    }
    function urlOf(input) { try { return typeof input === 'string' ? input : (input && input.url) || String(input || ''); } catch (_) { return ''; } }
    function inspectUrl(url) { if (!url) return true; return /qobuz|user\/login|album\/get|track\/get|playlist\/get|favorite|search\/get|\/api/i.test(String(url)); }
    function pairs(headersLike) {
      const out = [];
      if (!headersLike) return out;
      try { if (headersLike instanceof Headers) { headersLike.forEach((v,k)=>out.push([k,v])); return out; } } catch (_) {}
      if (Array.isArray(headersLike)) { for (const x of headersLike) if (Array.isArray(x) && x.length >= 2) out.push([x[0], x[1]]); return out; }
      if (typeof headersLike === 'object') { try { for (const k of Object.keys(headersLike)) out.push([k, headersLike[k]]); } catch (_) {} }
      return out;
    }
    function scanHeaders(headersLike, source, url) {
      let token = '', userId = '', appId = '';
      for (const pair of pairs(headersLike)) {
        const k = String(pair[0] || '').toLowerCase();
        const v = String(pair[1] || '').trim();
        if (k === 'x-user-auth-token' || k === 'user_auth_token' || k === 'user-auth-token') token = v;
        if (k === 'x-user-id' || k === 'user_id' || k === 'userid') userId = v;
        if (k === 'x-app-id' || k === 'app_id' || k === 'appid') appId = v;
      }
      if (token) post({ kind: 'capture', token: token, userId: userId, appId: appId, captureSource: source, detail: url || '' });
      if (appId && !token) post({ kind: 'app_id', appId: appId, captureSource: source, detail: url || '' });
    }
    function scanText(text, source, url) {
      text = String(text || '');
      if (!text) return;
      const tokenMatch = text.match(/["'](?:user_auth_token|x-user-auth-token|auth[_-]?token|token)["']\s*:\s*["']([^"']{12,4096})["']/i) || text.match(/\b(?:x-user-auth-token|user_auth_token)\b\s*[:=]\s*([^\s"'&,;<>]{12,4096})/i);
      if (!tokenMatch) return;
      const idMatch = text.match(/["'](?:user[_-]?id|userid|id)["']\s*:\s*["']?(\d{2,})["']?/i);
      const appMatch = text.match(/["'](?:x-app-id|app_id|appId)["']\s*[:=]\s*["']?([^"',}\s]{6,100})["']?/i);
      post({ kind: 'capture', token: tokenMatch[1], userId: idMatch ? idMatch[1] : '', appId: appMatch ? appMatch[1] : '', captureSource: source, detail: url || '' });
    }

    try {
      const originalFetch = window.fetch;
      if (originalFetch && !originalFetch.__streamrippaPagePatched) {
        const patchedFetch = async function(input, init) {
          const url = urlOf(input);
          if (inspectUrl(url)) { try { if (input && input.headers) scanHeaders(input.headers, 'page fetch request headers', url); if (init && init.headers) scanHeaders(init.headers, 'page fetch init headers', url); } catch (_) {} }
          const res = await originalFetch.apply(this, arguments);
          try {
            const rurl = url || urlOf(res && res.url);
            if (inspectUrl(rurl) && res && typeof res.clone === 'function') {
              const c = res.clone();
              c.text().then(t => { if (t && t.length < 5000000) scanText(t, 'page fetch response body', rurl); }).catch(() => {});
            }
          } catch (_) {}
          return res;
        };
        try { Object.defineProperty(patchedFetch, '__streamrippaPagePatched', { value: true }); } catch (_) { patchedFetch.__streamrippaPagePatched = true; }
        window.fetch = patchedFetch;
        post({ kind: 'status', part: 'fetch', ok: true });
      }
    } catch (e) { post({ kind: 'status', part: 'fetch', ok: false, error: String(e && e.message || e) }); }

    try {
      const OriginalXHR = window.XMLHttpRequest;
      if (OriginalXHR && !OriginalXHR.__streamrippaPagePatched) {
        function PatchedXHR() {
          const xhr = new OriginalXHR();
          let method = '', url = '';
          const hs = [];
          const open = xhr.open;
          const setHeader = xhr.setRequestHeader;
          xhr.open = function(m,u) { method = String(m || ''); url = urlOf(u); return open.apply(xhr, arguments); };
          xhr.setRequestHeader = function(k,v) { try { hs.push([k,v]); scanHeaders([[k,v]], 'page XHR request header', url); } catch (_) {} return setHeader.apply(xhr, arguments); };
          xhr.addEventListener('readystatechange', function() {
            if (xhr.readyState !== 4 || !inspectUrl(url)) return;
            try { scanHeaders(hs, 'page XHR request headers', method + ' ' + url); } catch (_) {}
            try { const t = xhr.responseText; if (t && t.length < 5000000) scanText(t, 'page XHR response body', method + ' ' + url); } catch (_) {}
          });
          return xhr;
        }
        try { PatchedXHR.prototype = OriginalXHR.prototype; Object.defineProperty(PatchedXHR, '__streamrippaPagePatched', { value: true }); } catch (_) { PatchedXHR.__streamrippaPagePatched = true; }
        window.XMLHttpRequest = PatchedXHR;
        post({ kind: 'status', part: 'xhr', ok: true });
      }
    } catch (e) { post({ kind: 'status', part: 'xhr', ok: false, error: String(e && e.message || e) }); }

    try {
      const originalSetItem = Storage && Storage.prototype && Storage.prototype.setItem;
      if (originalSetItem && !originalSetItem.__streamrippaPagePatched) {
        Storage.prototype.setItem = function(k, v) {
          const ret = originalSetItem.apply(this, arguments);
          try { if (/localuser|token|auth|user|qobuz/i.test(String(k) + ' ' + String(v).slice(0,300))) scanText(String(v), 'page Storage.setItem ' + String(k), String(k)); } catch (_) {}
          return ret;
        };
        try { Object.defineProperty(Storage.prototype.setItem, '__streamrippaPagePatched', { value: true }); } catch (_) {}
        post({ kind: 'status', part: 'storage', ok: true });
      }
    } catch (e) { post({ kind: 'status', part: 'storage', ok: false, error: String(e && e.message || e) }); }
  }

  function onPagePayload(payload) {
    if (!payload || payload.source !== MESSAGE_SOURCE) return;
    state.counters.messages += 1;
    if (payload.kind === 'capture') {
      saveCapture({ token: payload.token, userId: payload.userId, appId: payload.appId, source: payload.captureSource || 'page hook' });
    } else if (payload.kind === 'app_id' && payload.appId) {
      safeLocalSet(SAVE_KEYS.appId, payload.appId);
      renderBanner(false);
    } else if (payload.kind === 'status') {
      if (payload.part === 'fetch') state.fetchPatched = Boolean(payload.ok) || state.fetchPatched;
      if (payload.part === 'xhr') state.xhrPatched = Boolean(payload.ok) || state.xhrPatched;
      renderBanner(false);
    }
  }

  function setupPageEventListeners() {
    window.addEventListener('message', function(ev) {
      if (ev.source !== window) return;
      onPagePayload(ev.data);
    });
    window.addEventListener(EVENT_TYPE, function(ev) {
      state.counters.pageEvents += 1;
      onPagePayload(ev.detail);
    });
  }

  function createEl(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'style') Object.assign(el.style, v);
        else el.setAttribute(k, v);
      }
    }
    if (children) for (const child of children) if (child) el.appendChild(child);
    return el;
  }

  function st(el, styles) { Object.assign(el.style, styles); return el; }

  function renderBanner(forceShow) {
    if (!isQobuz() || state.closed) return;
    if (!document || !document.documentElement) { setTimeout(() => renderBanner(forceShow), 50); return; }
    if (!forceShow && safeLocalGet(SAVE_KEYS.hidden) === '1') return;

    state.counters.renders += 1;
    const saved = loadSaved();
    const hasToken = Boolean(saved.token);
    const old = document.getElementById(BANNER_ID);
    if (old) old.remove();

    const root = createEl('div', { id: BANNER_ID });
    root.setAttribute(STYLE_ATTR, '1');
    st(root, {
      position: 'fixed', right: '12px', bottom: '12px', width: 'min(780px, calc(100vw - 24px))',
      maxHeight: 'calc(100vh - 24px)', overflow: 'auto', zIndex: '2147483647',
      border: '1px solid #334155', borderLeft: '6px solid ' + (hasToken ? '#22c55e' : '#f59e0b'),
      borderRadius: '12px', background: '#0f172a', color: '#e2e8f0', boxShadow: '0 18px 60px rgba(0,0,0,.45)',
      font: '12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', boxSizing: 'border-box',
    });

    const top = st(createEl('div'), { display: 'flex', gap: '8px', justifyContent: 'space-between', alignItems: 'flex-start', padding: '12px 12px 8px' });
    const titleWrap = createEl('div');
    const title = createEl('div', { text: hasToken ? '✅ Token available — Streamrippa Qobuz token' : '⏳ Streamrippa — waiting for Qobuz token' });
    st(title, { fontWeight: '800', fontSize: '13px' });
    const sub = createEl('div', { text: hasToken ? ('Captured: ' + (saved.capturedAt || '—') + ' · source: ' + (saved.source || 'unknown')) : 'Banner/UI is running. Log in, open/play an album, or press Scan now.' });
    st(sub, { color: '#94a3b8', fontSize: '11px', marginTop: '2px' });
    titleWrap.appendChild(title); titleWrap.appendChild(sub);

    const actions = st(createEl('div'), { display: 'flex', flexWrap: 'wrap', gap: '6px', justifyContent: 'flex-end' });
    const buttons = [
      ['scan', 'Scan now'], ['min', state.minimised ? 'Expand' : 'Minimise'], ['hide', 'Hide'],
    ];
    for (const [act, label] of buttons) actions.appendChild(button(act, label));
    top.appendChild(titleWrap); top.appendChild(actions); root.appendChild(top);

    if (!state.minimised) {
      const body = st(createEl('div'), { padding: '0 12px 12px', display: 'grid', gap: '8px' });
      const status = createEl('div');
      st(status, { color: '#cbd5e1', display: 'flex', flexWrap: 'wrap', gap: '4px' });
      const statusBits = [
        'v' + VERSION,
        'UI ✓',
        'unsafeWindow ' + (state.unsafeHook ? '✓' : '…'),
        'pageHook ' + (state.injectedHook ? '✓/attempted' : '…'),
        'fetch ' + (state.fetchPatched ? '✓' : '…'),
        'XHR ' + (state.xhrPatched ? '✓' : '…'),
        'scans ' + state.counters.scans,
        'hits S/H/R ' + state.counters.storageHits + '/' + state.counters.headerHits + '/' + state.counters.responseHits,
      ];
      for (const bit of statusBits) status.appendChild(badge(bit));
      body.appendChild(status);

      if (hasToken) body.appendChild(capturedBody(saved));
      else body.appendChild(waitingBody());

      if (state.lastScan || state.lastError) {
        const detail = createEl('div', { text: (state.lastScan ? 'Last scan: ' + state.lastScan : '') + (state.lastError ? ' · Last issue: ' + state.lastError : '') });
        st(detail, { color: '#94a3b8', fontSize: '11px' });
        body.appendChild(detail);
      }
      root.appendChild(body);
    }

    root.addEventListener('click', handleBannerClick);
    appendToPage(root);
    state.uiReady = true;
  }

  function badge(text) {
    const el = createEl('span', { text });
    st(el, { display: 'inline-block', border: '1px solid #475569', borderRadius: '999px', padding: '1px 6px', whiteSpace: 'nowrap' });
    return el;
  }

  function button(action, text) {
    const b = createEl('button', { type: 'button', 'data-action': action, text });
    st(b, { border: '1px solid #475569', borderRadius: '8px', background: '#1e293b', color: '#f8fafc', padding: '5px 8px', font: 'inherit', cursor: 'pointer' });
    b.addEventListener('mouseenter', () => { b.style.background = '#334155'; });
    b.addEventListener('mouseleave', () => { b.style.background = '#1e293b'; });
    return b;
  }

  function input(value) {
    const i = createEl('input', { readonly: 'readonly', value: value || '' });
    st(i, { width: '100%', border: '1px solid #334155', borderRadius: '7px', background: '#020617', color: '#bbf7d0', padding: '6px 8px', font: 'inherit', boxSizing: 'border-box' });
    return i;
  }

  function textarea(value) {
    const t = createEl('textarea', { readonly: 'readonly', text: value || '' });
    st(t, { width: '100%', minHeight: '82px', border: '1px solid #334155', borderRadius: '7px', background: '#020617', color: '#bbf7d0', padding: '6px 8px', font: 'inherit', boxSizing: 'border-box' });
    return t;
  }

  function field(label, value, copyAction) {
    const row = st(createEl('div'), { display: 'grid', gridTemplateColumns: '150px minmax(0, 1fr) auto', gap: '6px', alignItems: 'center' });
    const lab = createEl('label', { text: label }); st(lab, { color: '#e2e8f0' });
    row.appendChild(lab); row.appendChild(input(value)); row.appendChild(button(copyAction, 'Copy'));
    return row;
  }

  function capturedBody(saved) {
    const wrap = st(createEl('div'), { display: 'grid', gap: '7px' });
    wrap.appendChild(field('email_or_userid', saved.userId || '', 'copy-userid'));
    wrap.appendChild(field('password_or_token', saved.token || '', 'copy-token'));
    if (saved.appId) wrap.appendChild(field('app_id', saved.appId, 'copy-appid'));
    const preview = createEl('div', { text: 'Token preview: ' + redact(saved.token) }); st(preview, { color: '#94a3b8', fontSize: '11px' }); wrap.appendChild(preview);
    wrap.appendChild(textarea(buildToml(saved)));
    const actions = st(createEl('div'), { display: 'flex', flexWrap: 'wrap', gap: '6px' });
    actions.appendChild(button('copy-toml', 'Copy Streamrip TOML'));
    actions.appendChild(button('scan', 'Rescan'));
    actions.appendChild(button('clear', 'Clear saved'));
    wrap.appendChild(actions);
    return wrap;
  }

  function waitingBody() {
    const wrap = st(createEl('div'), { display: 'grid', gap: '7px' });
    const help = createEl('div', { text: 'No token found yet. Best trigger: after logging in, open an album and start/pause a track so Qobuz sends authenticated API calls. This also scans localStorage.localuser repeatedly.' });
    st(help, { color: '#cbd5e1' });
    const actions = st(createEl('div'), { display: 'flex', flexWrap: 'wrap', gap: '6px' });
    actions.appendChild(button('scan', 'Scan now'));
    actions.appendChild(button('reinstall-hooks', 'Reinstall hooks'));
    actions.appendChild(button('clear', 'Clear saved'));
    wrap.appendChild(help); wrap.appendChild(actions);
    return wrap;
  }

  function buildToml(saved) {
    saved = saved || loadSaved();
    return [
      'use_auth_token = true',
      'email_or_userid = "' + escapeToml(saved.userId || '') + '"',
      'password_or_token = "' + escapeToml(saved.token || '') + '"',
    ].join('\n');
  }

  function escapeToml(value) { return String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"'); }

  function appendToPage(node) {
    const target = document.body || document.documentElement;
    if (target) target.appendChild(node);
    else setTimeout(() => appendToPage(node), 50);
  }

  function handleBannerClick(ev) {
    const b = ev.target && ev.target.closest && ev.target.closest('button[data-action]');
    if (!b) return;
    const action = b.getAttribute('data-action');
    if (action === 'scan') { scanAll('manual'); setTimeout(() => scanAll('manual delayed'), 1000); }
    if (action === 'reinstall-hooks') { installUnsafeWindowHooks(); installInjectedPageHook(); scanAll('reinstall hooks'); renderBanner(true); }
    if (action === 'min') { state.minimised = !state.minimised; renderBanner(true); }
    if (action === 'hide') { safeLocalSet(SAVE_KEYS.hidden, '1'); const el = document.getElementById(BANNER_ID); if (el) el.remove(); }
    if (action === 'clear') { for (const k of Object.values(SAVE_KEYS)) if (k !== SAVE_KEYS.hidden) safeLocalRemove(k); scanAll('clear saved'); renderBanner(true); }
    if (action === 'copy-toml') copyText(buildToml());
    if (action === 'copy-token') copyText(loadSaved().token || '');
    if (action === 'copy-userid') copyText(loadSaved().userId || '');
    if (action === 'copy-appid') copyText(loadSaved().appId || '');
  }

  function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => toast('Copied')).catch(() => fallbackCopy(text));
      } else fallbackCopy(text);
    } catch (_) { fallbackCopy(text); }
  }

  function fallbackCopy(text) {
    const ta = createEl('textarea');
    ta.value = text;
    st(ta, { position: 'fixed', left: '-9999px', top: '0' });
    appendToPage(ta);
    ta.focus(); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    toast(ok ? 'Copied' : 'Copy failed; select manually');
  }

  function toast(message) {
    try {
      const id = BANNER_ID + '-toast';
      const old = document.getElementById(id); if (old) old.remove();
      const el = createEl('div', { id, text: message });
      st(el, { position: 'fixed', right: '18px', bottom: '18px', zIndex: '2147483647', background: '#111827', color: '#f9fafb', border: '1px solid #374151', borderRadius: '8px', padding: '9px 11px', font: '12px ui-monospace, Menlo, Consolas, monospace', boxShadow: '0 10px 30px rgba(0,0,0,.35)' });
      appendToPage(el);
      setTimeout(() => { try { el.remove(); } catch (_) {} }, 1800);
    } catch (_) {}
  }

  function shortErr(err) { return String((err && (err.message || err.name)) || err || '').slice(0, 160); }

  function installRouteAndStorageWatchers() {
    try {
      for (const method of ['pushState', 'replaceState']) {
        const orig = history[method];
        if (!orig || orig.__streamrippaPatched) continue;
        history[method] = function patchedHistory() {
          const ret = orig.apply(this, arguments);
          setTimeout(() => scanAll('history.' + method), 250);
          return ret;
        };
        try { Object.defineProperty(history[method], '__streamrippaPatched', { value: true }); } catch (_) {}
      }
      window.addEventListener('popstate', () => setTimeout(() => scanAll('popstate'), 250));
      window.addEventListener('hashchange', () => setTimeout(() => scanAll('hashchange'), 250));
      window.addEventListener('focus', () => setTimeout(() => scanAll('focus'), 250));
      document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(() => scanAll('visibility'), 250); });
      window.addEventListener('storage', () => setTimeout(() => scanAll('storage event'), 100));
    } catch (_) {}
  }

  function keepBannerAlive() {
    setInterval(() => {
      if (state.closed || safeLocalGet(SAVE_KEYS.hidden) === '1') return;
      if (!document.getElementById(BANNER_ID)) renderBanner(true);
    }, 2000);
  }

  function boot() {
    if (!isQobuz()) return;
    safeLocalRemove(SAVE_KEYS.hidden); // New install/update should show the banner again.
    setupPageEventListeners();
    renderBanner(true); // UI first, before any risky hook work.
    installRouteAndStorageWatchers();

    try { installUnsafeWindowHooks(); } catch (err) { state.lastError = 'unsafeWindow hook: ' + shortErr(err); }
    try { installInjectedPageHook(); } catch (err) { state.lastError = 'page hook: ' + shortErr(err); }
    renderBanner(true);

    const scanTimes = [0, 200, 800, 1800, 3500, 7000, 15000, 30000];
    for (const ms of scanTimes) setTimeout(() => scanAll('scheduled ' + ms + 'ms'), ms);
    keepBannerAlive();

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { renderBanner(true); scanAll('DOMContentLoaded'); }, { once: true });
    } else {
      setTimeout(() => { renderBanner(true); scanAll('already loaded'); }, 0);
    }
    log('loaded', { version: VERSION, href: location.href });
  }

  boot();
})();
