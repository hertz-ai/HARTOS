/**
 * HART OS Docs — Hevolve Agent Chat Widget
 *
 * Injects a floating chat pill (bottom-right) that opens an iframe
 * to the hosted Hevolve chat. Reuses the existing embed=true path
 * from Hevolve-Landing — no new chat UI code.
 *
 * Config: set HEVOLVE_CHAT_URL via mkdocs extra, or defaults to
 * https://hevolve.hertzai.com
 */
(function () {
  'use strict';

  var BASE = 'https://hevolve.hertzai.com';
  var AGENT = 'Nunba';
  var GREETINGS = [
    'Ask Nunba anything\u2026',
    'Need help with HART OS?',
    'Try the SDK\u2026',
    'Create your HART\u2026',
  ];

  // --- State ---
  var open = false;
  var greetIdx = 0;

  // --- Pill (collapsed) ---
  var pill = document.createElement('div');
  pill.id = 'hevolve-chat-pill';
  pill.setAttribute('aria-label', 'Open Hevolve AI chat');
  pill.innerHTML =
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>' +
    '<span class="hevolve-pill-text">' + GREETINGS[0] + '</span>';

  // --- Panel (expanded) ---
  var panel = document.createElement('div');
  panel.id = 'hevolve-chat-panel';
  panel.style.display = 'none';

  var header = document.createElement('div');
  header.className = 'hevolve-chat-header';
  header.innerHTML =
    '<span class="hevolve-chat-title">Hevolve AI</span>' +
    '<button class="hevolve-chat-close" aria-label="Close chat">&times;</button>';

  var iframe = document.createElement('iframe');
  iframe.className = 'hevolve-chat-iframe';
  iframe.allow = 'microphone; camera; autoplay';
  iframe.title = 'Chat with ' + AGENT;
  // Lazy-load: src set on first open

  panel.appendChild(header);
  panel.appendChild(iframe);

  // --- Styles (injected once) ---
  var style = document.createElement('style');
  style.textContent = [
    '#hevolve-chat-pill{',
    '  position:fixed;bottom:24px;right:24px;z-index:9999;',
    '  display:flex;align-items:center;gap:8px;',
    '  background:#7c3aed;color:#fff;border:none;border-radius:28px;',
    '  padding:10px 18px 10px 14px;cursor:pointer;',
    '  font:500 14px/1.4 system-ui,sans-serif;',
    '  box-shadow:0 4px 16px rgba(124,58,237,.35);',
    '  transition:transform .2s,box-shadow .2s;',
    '}',
    '#hevolve-chat-pill:hover{transform:translateY(-2px);box-shadow:0 6px 24px rgba(124,58,237,.45)}',
    '.hevolve-pill-text{max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '@media(max-width:600px){.hevolve-pill-text{display:none}#hevolve-chat-pill{padding:12px}}',
    '#hevolve-chat-panel{',
    '  position:fixed;bottom:24px;right:24px;z-index:10000;',
    '  width:400px;height:560px;border-radius:16px;overflow:hidden;',
    '  box-shadow:0 8px 32px rgba(0,0,0,.25);',
    '  background:#1a1a2e;display:flex;flex-direction:column;',
    '}',
    '@media(max-width:600px){#hevolve-chat-panel{width:calc(100vw - 16px);height:70vh;bottom:8px;right:8px;border-radius:12px}}',
    '.hevolve-chat-header{',
    '  display:flex;align-items:center;justify-content:space-between;',
    '  padding:12px 16px;background:#000000;color:#fff;flex-shrink:0;',
    '}',
    '.hevolve-chat-title{font:600 15px/1.4 system-ui,sans-serif}',
    '.hevolve-chat-close{',
    '  background:none;border:none;color:#fff;font-size:22px;',
    '  cursor:pointer;padding:0 4px;line-height:1;',
    '}',
    '.hevolve-chat-iframe{flex:1;border:none;width:100%;background:#1a1a2e}',
  ].join('\n');

  // --- Logic ---
  function toggle() {
    open = !open;
    if (open) {
      pill.style.display = 'none';
      panel.style.display = 'flex';
      if (!iframe.src) {
        // Guest register via existing API, then pass token to iframe
        var guestToken = sessionStorage.getItem('hevolve_guest_token');
        if (guestToken) {
          iframe.src = BASE + '/?embed=true&companionAppInstalled=true&token=' + encodeURIComponent(guestToken);
        } else {
          fetch(BASE + '/api/social/auth/guest-register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({guest_name: 'Docs Visitor', source: 'docs_widget'})
          })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            var token = (data.data && data.data.token) || data.token || '';
            if (token) sessionStorage.setItem('hevolve_guest_token', token);
            iframe.src = BASE + '/?embed=true&companionAppInstalled=true&token=' + encodeURIComponent(token);
          })
          .catch(function() {
            iframe.src = BASE + '/?embed=true&companionAppInstalled=true';
          });
        }
      }
    } else {
      panel.style.display = 'none';
      pill.style.display = 'flex';
    }
  }

  function cycleGreeting() {
    greetIdx = (greetIdx + 1) % GREETINGS.length;
    var span = pill.querySelector('.hevolve-pill-text');
    if (span) span.textContent = GREETINGS[greetIdx];
  }

  // --- Mount ---
  document.head.appendChild(style);
  document.body.appendChild(pill);
  document.body.appendChild(panel);

  pill.addEventListener('click', toggle);
  header.querySelector('.hevolve-chat-close').addEventListener('click', toggle);

  // Cycle greetings every 4s
  setInterval(cycleGreeting, 4000);
})();
