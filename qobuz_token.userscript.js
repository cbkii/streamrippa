// ==UserScript==
// @name         Streamrippa — Qobuz Token Extractor
// @namespace    https://github.com/cbkii/streamrippa
// @version      1.0.0
// @description  Intercepts the Qobuz login response, extracts user ID and auth token, then displays them in a persistent banner at the bottom of all qobuz.com pages for use with streamrippa's use_auth_token config.
// @author       cbkii/streamrippa
// @match        https://*.qobuz.com/*
// @run-at       document-start
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_setClipboard
// ==/UserScript==

(function () {
  'use strict';

  // ─── Constants ────────────────────────────────────────────────────────────
  const STORAGE_KEY_ID    = 'streamrippa_qobuz_user_id';
  const STORAGE_KEY_TOKEN = 'streamrippa_qobuz_auth_token';
  const STORAGE_KEY_TS    = 'streamrippa_qobuz_captured_at';
  const LOGIN_ENDPOINT    = 'user/login';
  const BANNER_ID         = 'streamrippa-token-banner';

  // ─── XHR Interception ─────────────────────────────────────────────────────
  // Wrap XMLHttpRequest to intercept the user/login API response.
  const OriginalXHR = window.XMLHttpRequest;

  function PatchedXHR() {
    const xhr        = new OriginalXHR();
    const origOpen   = xhr.open.bind(xhr);
    let   _url       = '';

    xhr.open = function (method, url, ...rest) {
      _url = url;
      return origOpen(method, url, ...rest);
    };

    xhr.addEventListener('load', function () {
      if (_url && _url.includes(LOGIN_ENDPOINT)) {
        try {
          const data = JSON.parse(xhr.responseText);
          handleLoginResponse(data);
        } catch (_) {
          // Response was not JSON — ignore.
        }
      }
    });

    return xhr;
  }

  // Copy all static properties/prototype so nothing breaks.
  Object.defineProperty(PatchedXHR, 'UNSENT',           { value: 0 });
  Object.defineProperty(PatchedXHR, 'OPENED',           { value: 1 });
  Object.defineProperty(PatchedXHR, 'HEADERS_RECEIVED', { value: 2 });
  Object.defineProperty(PatchedXHR, 'LOADING',          { value: 3 });
  Object.defineProperty(PatchedXHR, 'DONE',             { value: 4 });
  PatchedXHR.prototype = OriginalXHR.prototype;

  window.XMLHttpRequest = PatchedXHR;

  // ─── Fetch Interception ───────────────────────────────────────────────────
  // Also intercept fetch() in case Qobuz uses it.
  const originalFetch = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    const response = await originalFetch(input, init);
    const url      = (typeof input === 'string') ? input : (input.url || '');

    if (url.includes(LOGIN_ENDPOINT)) {
      try {
        const clone = response.clone();
        const data  = await clone.json();
        handleLoginResponse(data);
      } catch (_) {
        // Not JSON or already consumed — ignore.
      }
    }

    return response;
  };

  // ─── Response Handler ─────────────────────────────────────────────────────
  function handleLoginResponse(data) {
    // The user/login response contains:
    //   data.user.id          — numeric user ID
    //   data.user_auth_token  — JWT auth token
    const userId    = data?.user?.id    ? String(data.user.id) : null;
    const authToken = data?.user_auth_token                    ? String(data.user_auth_token) : null;

    if (!userId || !authToken) return;

    const ts = new Date().toISOString();

    // Persist via GM storage (survives page reloads across all qobuz.com pages).
    GM_setValue(STORAGE_KEY_ID,    userId);
    GM_setValue(STORAGE_KEY_TOKEN, authToken);
    GM_setValue(STORAGE_KEY_TS,    ts);

    // Update or create the banner immediately.
    renderBanner(userId, authToken, ts, /* justCaptured= */ true);
  }

  // ─── Banner Rendering ─────────────────────────────────────────────────────
  function renderBanner(userId, authToken, capturedAt, justCaptured = false) {
    // Remove any existing banner before re-rendering.
    const existing = document.getElementById(BANNER_ID);
    if (existing) existing.remove();

    const ts      = capturedAt ? new Date(capturedAt).toLocaleString() : '—';
    const banner  = document.createElement('div');
    banner.id     = BANNER_ID;

    Object.assign(banner.style, {
      position:        'fixed',
      bottom:          '0',
      left:            '0',
      right:           '0',
      zIndex:          '2147483647',
      background:      justCaptured ? '#1a472a' : '#1a1a2e',
      color:           '#e8e8e8',
      fontFamily:      'monospace, monospace',
      fontSize:        '12px',
      lineHeight:      '1.5',
      padding:         '10px 14px 10px 14px',
      borderTop:       justCaptured ? '2px solid #4caf50' : '2px solid #3a3a6e',
      boxShadow:       '0 -2px 12px rgba(0,0,0,0.6)',
      display:         'flex',
      flexDirection:   'column',
      gap:             '6px',
      transition:      'border-color 0.4s',
    });

    // ── Header row ──
    const headerRow = document.createElement('div');
    Object.assign(headerRow.style, {
      display:        'flex',
      justifyContent: 'space-between',
      alignItems:     'center',
    });

    const title = document.createElement('span');
    title.textContent = justCaptured
      ? '✅  Streamrippa — Qobuz token captured!'
      : '📋  Streamrippa — Cached Qobuz token';
    Object.assign(title.style, {
      fontWeight: 'bold',
      color:      justCaptured ? '#4caf50' : '#7986cb',
      fontSize:   '13px',
    });

    const tsLabel = document.createElement('span');
    tsLabel.textContent = `Captured: ${ts}`;
    Object.assign(tsLabel.style, { color: '#888', fontSize: '11px' });

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    Object.assign(closeBtn.style, {
      background:   'transparent',
      border:       'none',
      color:        '#aaa',
      cursor:       'pointer',
      fontSize:     '15px',
      marginLeft:   '12px',
      padding:      '0 4px',
      lineHeight:   '1',
    });
    closeBtn.title = 'Dismiss banner (token is still saved)';
    closeBtn.addEventListener('click', () => banner.remove());

    headerRow.append(title, tsLabel, closeBtn);

    // ── Instruction row ──
    const instrRow = document.createElement('div');
    instrRow.style.color = '#bbb';
    instrRow.innerHTML =
      'Add the values below to your <code style="background:#333;padding:1px 4px;border-radius:3px">~/.config/streamrip/config.toml</code>:&nbsp;&nbsp;' +
      '<code style="background:#333;padding:1px 4px;border-radius:3px">use_auth_token = true</code>';

    // ── Field rows ──
    const fields = [
      { label: 'email_or_userid',   value: userId,    title: 'Copy user ID' },
      { label: 'password_or_token', value: authToken, title: 'Copy auth token' },
    ];

    const fieldContainer = document.createElement('div');
    Object.assign(fieldContainer.style, {
      display:       'flex',
      flexDirection: 'column',
      gap:           '4px',
    });

    fields.forEach(({ label, value, title: btnTitle }) => {
      const row = document.createElement('div');
      Object.assign(row.style, {
        display:    'flex',
        alignItems: 'center',
        gap:        '8px',
      });

      const labelEl = document.createElement('span');
      labelEl.textContent = `${label} =`;
      Object.assign(labelEl.style, {
        color:      '#9fa8da',
        minWidth:   '160px',
        flexShrink: '0',
      });

      const valueEl = document.createElement('input');
      valueEl.type     = 'text';
      valueEl.readOnly = true;
      valueEl.value    = value;
      Object.assign(valueEl.style, {
        flex:            '1',
        background:      '#0d0d1a',
        border:          '1px solid #333',
        borderRadius:    '4px',
        color:           '#c8e6c9',
        fontFamily:      'monospace',
        fontSize:        '11px',
        padding:         '3px 7px',
        outline:         'none',
        overflow:        'hidden',
        textOverflow:    'ellipsis',
        whiteSpace:      'nowrap',
      });
      // Select all on click for easy manual copying.
      valueEl.addEventListener('click', () => valueEl.select());

      const copyBtn = document.createElement('button');
      copyBtn.textContent = '⧉ Copy';
      Object.assign(copyBtn.style, {
        background:   '#333',
        border:       '1px solid #555',
        borderRadius: '4px',
        color:        '#ccc',
        cursor:       'pointer',
        fontSize:     '11px',
        padding:      '3px 8px',
        flexShrink:   '0',
        whiteSpace:   'nowrap',
      });
      copyBtn.title = btnTitle;

      copyBtn.addEventListener('click', () => {
        GM_setClipboard(value);
        copyBtn.textContent = '✓ Copied!';
        copyBtn.style.color = '#4caf50';
        setTimeout(() => {
          copyBtn.textContent = '⧉ Copy';
          copyBtn.style.color = '#ccc';
        }, 1800);
      });

      row.append(labelEl, valueEl, copyBtn);
      fieldContainer.append(row);
    });

    // ── Expiry reminder ──
    const reminderRow = document.createElement('div');
    reminderRow.style.color  = '#e57373';
    reminderRow.style.fontSize = '11px';
    reminderRow.textContent =
      '⚠  Tokens expire (typically within days). Re-login here to refresh. ' +
      'When expired, streamrip will return 401 again — just repeat the process.';

    banner.append(headerRow, instrRow, fieldContainer, reminderRow);
    document.body.appendChild(banner);
  }

  // ─── On Page Load: Show Cached Token ──────────────────────────────────────
  // If we already have a stored token, show the banner on every qobuz.com page.
  function maybeShowCachedBanner() {
    const userId    = GM_getValue(STORAGE_KEY_ID,    null);
    const authToken = GM_getValue(STORAGE_KEY_TOKEN, null);
    const ts        = GM_getValue(STORAGE_KEY_TS,    null);

    if (userId && authToken) {
      renderBanner(userId, authToken, ts, /* justCaptured= */ false);
    }
  }

  // Wait for <body> to exist before injecting the banner.
  if (document.body) {
    maybeShowCachedBanner();
  } else {
    document.addEventListener('DOMContentLoaded', maybeShowCachedBanner);
  }

})();
