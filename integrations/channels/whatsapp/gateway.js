// Embedded Baileys gateway — exposes the WAHA-compatible HTTP+WS API
// subset that integrations/channels/whatsapp_adapter.py expects, so we
// can drop the WAHA Docker dependency without refactoring the adapter.
//
// Why a shim instead of importing Baileys directly into the adapter:
//   - Baileys is Node-only.  HARTOS's adapter is async Python.  The
//     existing adapter already speaks WAHA HTTP/WS — re-routing it
//     through a localhost Node process keeps the adapter unchanged
//     and gives us a single transport seam to swap implementations.
//   - The supervisor (whatsapp_supervisor.py, sister of
//     livekit_supervisor.py) owns the subprocess lifecycle: install
//     deps once, daemon-thread respawn on crash, port-conflict
//     short-circuit when an operator already runs WAHA elsewhere.
//
// Spec endpoints implemented (the 6 the adapter actually calls today):
//   GET  /api/health
//   POST /api/sessions/:id/start
//   GET  /api/sessions/:id/status
//   GET  /api/sessions/:id/qr
//   POST /api/sessions/:id/messages/send
//   POST /api/sessions/:id/stop
//   WS   /ws/:id    — fans out qr / authenticated / disconnected /
//                     message events to the adapter.
//
// Auth state persisted under $HEVOLVE_HOME/whatsapp/auth/<accountId>/
// via Baileys' useMultiFileAuthState — same path the supervisor
// references when checking "previously paired" status.
//
// Port: $WHATSAPP_GATEWAY_PORT (default 3000).  Bound to 127.0.0.1
// only — never exposed externally; operator-managed remote WAHA goes
// through the existing WHATSAPP_API_URL override path on the adapter.

const path = require('path');
const fs = require('fs');
const os = require('os');

// Baileys is published as an ES Module (since v6.x).  CommonJS `require()`
// of an ESM module throws ERR_REQUIRE_ESM, which is exactly what was
// killing the supervisor in a respawn loop ("rc=78; respawning in 60s").
// Node tells you the remedy in its own error text: use dynamic import().
// The function-scoped `let`s below are populated by the async bootstrap
// at the bottom of the file BEFORE app.listen() — every handler that
// references them runs only after a session is started, which can't
// happen until the server is listening.
let makeWASocket, useMultiFileAuthState, DisconnectReason, getContentType, Browsers;

let express, expressWs;
try {
  express = require('express');
  expressWs = require('express-ws');
} catch (e) {
  console.error(JSON.stringify({
    event: 'startup_error',
    error: 'express / express-ws not installed — run `npm install`',
    detail: String(e && e.message),
  }));
  process.exit(78);
}

const PORT = parseInt(process.env.WHATSAPP_GATEWAY_PORT || '3000', 10);
const HOME = process.env.HEVOLVE_HOME || path.join(os.homedir(), '.hevolve');
const AUTH_BASE = path.join(HOME, 'whatsapp', 'auth');
fs.mkdirSync(AUTH_BASE, { recursive: true });

// One Baileys socket per accountId.  `wsClients` is the set of
// connected adapter WebSockets fanning out QR / message events.
const sessions = new Map();

function getSession(id) { return sessions.get(id); }

async function ensureSession(accountId) {
  let ctx = sessions.get(accountId);
  if (ctx) return ctx;

  ctx = {
    sock: null,
    qr: null,
    authenticated: false,
    state: 'connecting',
    wsClients: new Set(),
    accountId,
    messages: [],
    // Dedup for messages.upsert — see the handler below for why this
    // exists (2026-07-28: reconnects re-delivered entire chat history
    // as if new, spamming the real self-chat with a fresh LLM reply per
    // historical message on every restart).
    seenMessageIds: new Set(),
  };
  sessions.set(accountId, ctx);

  await connectSocket(ctx);
  return ctx;
}

// Newest-last, capped — the mobile app polls GET /messages instead of
// holding a live WebSocket open, so the gateway keeps a short in-memory
// history per session rather than requiring a persistent connection.
const MAX_BUFFERED_MESSAGES = 50;
function bufferMessage(ctx, wahaMessage) {
  ctx.messages.push(wahaMessage);
  if (ctx.messages.length > MAX_BUFFERED_MESSAGES) {
    ctx.messages.splice(0, ctx.messages.length - MAX_BUFFERED_MESSAGES);
  }
}

// (Re)creates the Baileys socket for an existing ctx and wires its
// listeners.  Called on first connect AND after every disconnect that
// isn't a real logout — WhatsApp's multi-device protocol always tears
// the stream down once (DisconnectReason.restartRequired / code 515)
// right after a pairing code or QR scan is accepted, and expects the
// client to immediately reconnect with the now-saved creds.  Without
// this, every successful pairing looked identical to a failure: creds
// were written to disk (creds.update fires before the restart) but
// ctx.state stayed 'disconnected' forever because nothing ever called
// makeWASocket() again.
async function connectSocket(ctx) {
  const authPath = path.join(AUTH_BASE, ctx.accountId);
  fs.mkdirSync(authPath, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(authPath);
  // Fixed, REAL browser identity — Browsers.macOS('Chrome') (a preset
  // Baileys itself ships in Utils/browser-utils.js), not Baileys'
  // unpinned default and NOT a hand-rolled tuple. Observed twice in
  // testing: without ANY pinning, each (re)connect can present a
  // different fingerprint for the same linked device (e.g.
  // Ubuntu/Chrome, then Mac OS/Chrome on the very next reconnect) —
  // WhatsApp's servers treat that as a hijacked/cloned session and kill
  // the link (stream:error code 401, conflict=device_removed) within
  // minutes of a fresh pairing succeeding. But a MADE-UP tuple (tried:
  // ['Nunba','Chrome','120.0.0']) gets rejected outright during fresh
  // pairing-code registration specifically ("Connection Failure" in the
  // noise handshake, every time) — WhatsApp validates the identity
  // during that sensitive step more strictly than on an established
  // reconnect. A real Baileys preset is both stable (fixes device
  // removal) and legitimate (fixes registration).
  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    browser: Browsers.macOS('Chrome'),
  });
  ctx.sock = sock;

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      ctx.qr = qr;
      broadcast(ctx, { type: 'qr', qr });
    }
    if (connection === 'open') {
      ctx.authenticated = true;
      ctx.state = 'connected';
      ctx.qr = null;
      ctx._reconnects = 0;  // healthy connection — reset the backoff counter
      broadcast(ctx, { type: 'authenticated' });
    } else if (connection === 'close') {
      // Intentional teardown (a newer request replaced this ctx with a
      // fresh one — see request-pair-code) must NOT reconnect. Without
      // this, calling sock.end() on the old socket still fires its own
      // 'close' event through these same listeners, and the reconnect
      // logic below would spin up a SECOND socket authenticating as the
      // same device at the same moment as the deliberately-fresh one —
      // WhatsApp's servers see two concurrent connections for one
      // identity and kill the handshake (observed as "Connection
      // Failure" on the new socket, seconds after a clean teardown).
      if (ctx._torndown) return;

      const code = lastDisconnect && lastDisconnect.error
        && lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
      ctx.authenticated = false;
      ctx.state = 'disconnected';
      broadcast(ctx, { type: 'disconnected', code });

      const loggedOut = DisconnectReason && code === DisconnectReason.loggedOut;
      if (!loggedOut) {
        // Backoff + cap so a repeatedly-rejected handshake cannot hot-loop
        // connectSocket() and hammer WhatsApp into rate-limiting/blocking the
        // number.  Previously this reconnected IMMEDIATELY with no delay and
        // no limit, so a fresh-registration "Connection Failure" produced 60+
        // attempts a minute and got the device temporarily blocked.
        // restartRequired (515) right after pairing is the normal fast path
        // and is not counted; every other close backs off exponentially and
        // gives up after MAX_RECONNECTS consecutive failures (call /start
        // again to retry a stopped session).
        const MAX_RECONNECTS = 5;
        const restartRequired = DisconnectReason
          && code === DisconnectReason.restartRequired;
        ctx._reconnects = restartRequired ? 0 : ((ctx._reconnects || 0) + 1);
        if (ctx._reconnects > MAX_RECONNECTS) {
          ctx.state = 'disconnected';
          console.error(JSON.stringify({
            event: 'reconnect_giveup',
            accountId: ctx.accountId,
            code,
            attempts: ctx._reconnects,
            detail: 'stopped reconnecting to avoid WhatsApp rate-limit; '
              + 'POST /start again to retry',
          }));
          broadcast(ctx, { type: 'disconnected', code, gaveUp: true });
          return;
        }
        const delay = restartRequired
          ? 1000
          : Math.min(2000 * (2 ** (ctx._reconnects - 1)), 60000);
        setTimeout(() => {
          connectSocket(ctx).catch((e) => {
            console.error(JSON.stringify({
              event: 'reconnect_failed',
              accountId: ctx.accountId,
              error: String(e && e.message),
            }));
          });
        }, delay);
      } else {
        // A real logout (the phone removed this linked device, or the
        // user unlinked it) — the socket is permanently dead and the
        // saved creds are invalid on WhatsApp's servers now. Without
        // this, `sessions` keeps the dead ctx forever: ensureSession()
        // short-circuits on "ctx exists" and every future /qr or
        // /pair-code call reuses the same closed socket, always
        // failing with "Connection Closed" until the whole gateway
        // process is restarted. Drop both so the next request builds
        // a genuinely fresh session + fresh creds.
        sessions.delete(ctx.accountId);
        fs.rm(path.join(AUTH_BASE, ctx.accountId), { recursive: true, force: true }, () => {});
      }
    }
  });

  sock.ev.on('messages.upsert', ({ messages, type }) => {
    // Baileys fires this on every reconnect too, with `type: 'append'`
    // (history resync) delivering recently-seen messages again, not just
    // `type: 'notify'` (genuinely new). Broadcasting 'append' as if it
    // were a fresh inbound message meant every HARTOS restart replayed
    // the WHOLE recent chat history to any live self-chat adapter,
    // spamming a real WhatsApp thread with a duplicate LLM reply per old
    // message (confirmed live 2026-07-28). Message-id dedup on top is
    // belt-and-suspenders in case 'notify' itself ever repeats on a
    // flaky reconnect.
    if (type !== 'notify') return;
    for (const m of messages || []) {
      const wahaMessage = toWahaShape(m);
      if (!wahaMessage) continue;
      const msgId = wahaMessage.id && wahaMessage.id._serialized;
      if (msgId) {
        if (ctx.seenMessageIds.has(msgId)) continue;
        ctx.seenMessageIds.add(msgId);
        if (ctx.seenMessageIds.size > 500) {
          // Bound growth — drop the oldest half (insertion-ordered Set).
          const it = ctx.seenMessageIds.values();
          for (let i = 0; i < 250; i++) ctx.seenMessageIds.delete(it.next().value);
        }
      }
      bufferMessage(ctx, wahaMessage);
      broadcast(ctx, { type: 'message', data: wahaMessage });
    }
  });

  return ctx;
}

// Baileys' raw WAMessage (key.remoteJid / message.conversation / ...)
// has nothing in common with the WAHA/whatsapp-web.js shape that
// integrations/channels/whatsapp_adapter.py's _convert_message() was
// written against (chat.id._serialized, sender.id._serialized, body,
// hasMedia, ...).  Broadcasting the raw object silently produced an
// all-empty Message on the Python side (every field defaulted) even
// though a real message had arrived — this translates into the shape
// the adapter already expects, so the "same WAHA API subset" promise
// in this file's header comment is actually true for messages too.
const MEDIA_TYPE_MAP = {
  imageMessage: 'image',
  videoMessage: 'video',
  audioMessage: 'audio',
  pttMessage: 'ptt',
  documentMessage: 'document',
  stickerMessage: 'sticker',
};

function toWahaShape(m) {
  if (!m || !m.message || !m.key) return null;
  const contentType = getContentType(m.message);
  if (!contentType) return null;
  // Baileys sends a handful of non-content protocol messages through
  // the same upsert stream (e.g. protocolMessage for deletes/edits,
  // reactionMessage) — nothing the adapter's Message model represents.
  if (contentType === 'protocolMessage' || contentType === 'reactionMessage') return null;

  const remoteJid = m.key.remoteJid || '';
  const isGroup = remoteJid.endsWith('@g.us');
  const senderId = m.key.participant || remoteJid;
  const content = m.message[contentType] || {};

  let body = '';
  if (contentType === 'conversation') {
    body = m.message.conversation || '';
  } else if (contentType === 'extendedTextMessage') {
    body = content.text || '';
  } else {
    body = content.caption || '';
  }

  const mediaType = MEDIA_TYPE_MAP[contentType];
  const contextInfo = content.contextInfo || {};
  const ts = typeof m.messageTimestamp === 'number'
    ? m.messageTimestamp
    : Number(m.messageTimestamp) || Math.floor(Date.now() / 1000);

  return {
    id: { _serialized: (m.key.id || '') },
    chat: { id: { _serialized: remoteJid }, isGroup },
    sender: { id: { _serialized: senderId }, pushname: m.pushName || null },
    body,
    hasMedia: Boolean(mediaType),
    type: mediaType || 'chat',
    mediaKey: content.mediaKey || null,
    mimetype: content.mimetype || null,
    filename: content.fileName || null,
    caption: mediaType ? (content.caption || null) : null,
    mentionedIds: contextInfo.mentionedJid || [],
    quotedMsgId: contextInfo.stanzaId || null,
    timestamp: ts,
    fromMe: Boolean(m.key.fromMe),
  };
}

function broadcast(ctx, event) {
  const text = JSON.stringify(event);
  for (const client of ctx.wsClients) {
    if (client.readyState === 1) {  // OPEN
      try { client.send(text); } catch (_) { /* fan-out best-effort */ }
    }
  }
}

const app = express();
expressWs(app);
app.use(express.json({ limit: '5mb' }));

app.get('/api/health', (req, res) => res.json({ status: 'ok', impl: 'baileys' }));

app.post('/api/sessions/:id/start', async (req, res) => {
  try {
    await ensureSession(req.params.id);
    res.status(201).json({ success: true });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message) });
  }
});

app.get('/api/sessions/:id/status', (req, res) => {
  const ctx = getSession(req.params.id);
  res.json({
    authenticated: !!(ctx && ctx.authenticated),
    state: ctx ? ctx.state : 'not_started',
    qr: (ctx && ctx.qr) || null,
    // Lets the send route resolve "message myself" without duplicating
    // Baileys' own-identity lookup — sock.user is populated once
    // 'connection.update' has fired connection:'open'.
    own_jid: (ctx && ctx.sock && ctx.sock.user && ctx.sock.user.id) || null,
    // WhatsApp's newer LID (privacy ID) scheme: for LID-enabled accounts,
    // a self-chat's message.sender/chat id shows up as "<digits>@lid",
    // NOT the phone-based "<phone>@s.whatsapp.net" own_jid above — so
    // self-chat detection needs this too, or it can never match. Baileys
    // populates sock.user.lid once WhatsApp sends the lid-mapping creds
    // update (may be null briefly right after connecting).
    own_lid: (ctx && ctx.sock && ctx.sock.user && ctx.sock.user.lid) || null,
  });
});

app.get('/api/sessions/:id/qr', (req, res) => {
  const ctx = getSession(req.params.id);
  res.json({ qr: (ctx && ctx.qr) || null });
});

// Newest messages only, via ?since_ts=<unix seconds> — the mobile app
// polls this instead of holding a WebSocket open. Includes both
// received AND our own sent messages (Baileys' messages.upsert fires
// for both, fromMe distinguishes them), so a chat UI sees one thread.
app.get('/api/sessions/:id/messages', (req, res) => {
  const ctx = getSession(req.params.id);
  const sinceTs = Number(req.query.since_ts) || 0;
  const all = (ctx && ctx.messages) || [];
  res.json({ messages: sinceTs ? all.filter((m) => m.timestamp > sinceTs) : all });
});

app.post('/api/sessions/:id/messages/send', async (req, res) => {
  const ctx = getSession(req.params.id);
  if (!ctx || !ctx.authenticated) {
    return res.status(409).json({ error: 'not_authenticated' });
  }
  const { to, text, media, mimetype, filename } = req.body || {};
  if (!to || !text) {
    return res.status(400).json({ error: 'to + text required' });
  }
  try {
    // Media leg (#752): the Python adapter base64-encodes the file into
    // req.body.media (whatsapp_adapter.py:299-305). Baileys wants a
    // Buffer under a type-keyed field; images ride {image, caption} so
    // the receipt text arrives as the picture's caption in one bubble.
    // NOT yet live-verified against a paired device (flagged on the
    // board) — the text-only path below stays the proven one.
    let content = { text };
    if (media) {
      const buf = Buffer.from(media, 'base64');
      const mt = String(mimetype || 'image/png');
      if (mt.startsWith('image/')) {
        content = { image: buf, caption: text, mimetype: mt };
      } else if (mt.startsWith('video/')) {
        content = { video: buf, caption: text, mimetype: mt };
      } else if (mt.startsWith('audio/')) {
        content = { audio: buf, mimetype: mt };
      } else {
        content = { document: buf, mimetype: mt,
                    fileName: String(filename || 'file'), caption: text };
      }
    }
    const result = await ctx.sock.sendMessage(to, content);
    res.json({
      success: true,
      messageId: result && result.key && result.key.id,
    });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message) });
  }
});

// "Link with phone number" pairing — Baileys' requestPairingCode().
// Phone must be a digits-only E.164 (e.g. "919003054371" — no '+'). Returns
// the 8-char code the user types in WhatsApp → Linked Devices → Link with
// phone number.  Code is valid for ~60s on WhatsApp's side; if it expires
// the same endpoint can be called again to mint a new one.
app.post('/api/sessions/:id/request-pair-code', async (req, res) => {
  try {
    const accountId = req.params.id;
    const phone = String((req.body && req.body.phone) || '').replace(/\D/g, '');
    if (!phone) {
      return res.status(400).json({ error: 'phone (digits only, E.164 without +) required' });
    }

    let ctx = sessions.get(accountId);
    if (ctx && ctx.authenticated) {
      return res.status(409).json({ error: 'already_authenticated' });
    }

    // requestPairingCode() must be called on a freshly-created socket's
    // very first connection attempt. Every other route (/qr, /start)
    // defaults to QR-mode via ensureSession(), which is idempotent and
    // reuses whatever ctx already exists — including one that's already
    // cycled through one or more QR-refresh reconnects (Baileys expires
    // and regenerates the QR every ~2.5 min while nobody scans it).
    // Calling requestPairingCode() on that already-cycled socket
    // produces a low-level noise-handshake "Connection Failure" instead
    // of a real code (observed repeatedly in testing — only ever worked
    // when the socket was brand new). Tear down whatever exists and
    // build a clean one just for this request.
    if (ctx) {
      ctx._torndown = true;
      try { ctx.sock && ctx.sock.end && ctx.sock.end(); } catch (_) { /* best-effort */ }
      sessions.delete(accountId);
    }
    ctx = {
      sock: null, qr: null, authenticated: false, state: 'connecting',
      wsClients: new Set(), accountId, messages: [],
    };
    sessions.set(accountId, ctx);
    await connectSocket(ctx);

    if (typeof ctx.sock.requestPairingCode !== 'function') {
      return res.status(501).json({ error: 'pair-code not supported by this Baileys version' });
    }
    const code = await ctx.sock.requestPairingCode(phone);
    res.json({ success: true, code });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message) });
  }
});

app.post('/api/sessions/:id/stop', async (req, res) => {
  const ctx = getSession(req.params.id);
  if (ctx) {
    try { ctx.sock.end(); } catch (_) {}
    for (const c of ctx.wsClients) {
      try { c.close(1000); } catch (_) {}
    }
    sessions.delete(req.params.id);
  }
  res.json({ success: true });
});

app.ws('/ws/:id', (ws, req) => {
  const ctx = getSession(req.params.id);
  if (!ctx) {
    try { ws.close(1008, 'session not started'); } catch (_) {}
    return;
  }
  ctx.wsClients.add(ws);
  // Replay last QR on connect so a reconnecting adapter doesn't miss it.
  if (ctx.qr) {
    try { ws.send(JSON.stringify({ type: 'qr', qr: ctx.qr })); } catch (_) {}
  }
  if (ctx.authenticated) {
    try { ws.send(JSON.stringify({ type: 'authenticated' })); } catch (_) {}
  }
  ws.on('close', () => ctx.wsClients.delete(ws));
  ws.on('error', () => ctx.wsClients.delete(ws));
});

// Async bootstrap: load baileys via dynamic import() (ESM-only), then
// start listening.  Routes are wired above synchronously and only
// dereference makeWASocket / useMultiFileAuthState inside ensureSession(),
// which can't run until a /api/sessions/:id/start request arrives —
// which can't arrive until app.listen() returns — which we don't call
// until the baileys import resolves.  So the timing is safe.
(async () => {
  try {
    const baileys = await import('@whiskeysockets/baileys');
    makeWASocket = baileys.default || baileys.makeWASocket;
    useMultiFileAuthState = baileys.useMultiFileAuthState;
    DisconnectReason = baileys.DisconnectReason;
    getContentType = baileys.getContentType;
    Browsers = baileys.Browsers;
  } catch (e) {
    console.error(JSON.stringify({
      event: 'startup_error',
      error: 'baileys not installed — run `npm install` in this directory',
      detail: String(e && e.message),
    }));
    process.exit(78);  // EX_CONFIG
  }

  const server = app.listen(PORT, '127.0.0.1', () => {
    console.log(JSON.stringify({
      event: 'gateway_started',
      port: PORT,
      auth_base: AUTH_BASE,
      pid: process.pid,
    }));
  });

  function shutdown(signal) {
    console.log(JSON.stringify({ event: 'gateway_shutdown', signal }));
    for (const [id, ctx] of sessions) {
      try { ctx.sock.end(); } catch (_) {}
    }
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 5000).unref();
  }
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
})();
