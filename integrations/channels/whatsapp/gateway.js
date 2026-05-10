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

let makeWASocket, useMultiFileAuthState;
try {
  const baileys = require('@whiskeysockets/baileys');
  makeWASocket = baileys.default || baileys.makeWASocket;
  useMultiFileAuthState = baileys.useMultiFileAuthState;
} catch (e) {
  console.error(JSON.stringify({
    event: 'startup_error',
    error: 'baileys not installed — run `npm install` in this directory',
    detail: String(e && e.message),
  }));
  process.exit(78);  // EX_CONFIG
}

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

  const authPath = path.join(AUTH_BASE, accountId);
  fs.mkdirSync(authPath, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(authPath);
  const sock = makeWASocket({ auth: state, printQRInTerminal: false });

  ctx = {
    sock,
    qr: null,
    authenticated: false,
    state: 'connecting',
    wsClients: new Set(),
    accountId,
  };
  sessions.set(accountId, ctx);

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
      broadcast(ctx, { type: 'authenticated' });
    } else if (connection === 'close') {
      const code = lastDisconnect && lastDisconnect.error
        && lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
      ctx.authenticated = false;
      ctx.state = 'disconnected';
      broadcast(ctx, { type: 'disconnected', code });
    }
  });

  sock.ev.on('messages.upsert', ({ messages }) => {
    for (const m of messages || []) {
      broadcast(ctx, { type: 'message', message: m });
    }
  });

  return ctx;
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
  });
});

app.get('/api/sessions/:id/qr', (req, res) => {
  const ctx = getSession(req.params.id);
  res.json({ qr: (ctx && ctx.qr) || null });
});

app.post('/api/sessions/:id/messages/send', async (req, res) => {
  const ctx = getSession(req.params.id);
  if (!ctx || !ctx.authenticated) {
    return res.status(409).json({ error: 'not_authenticated' });
  }
  const { to, text } = req.body || {};
  if (!to || !text) {
    return res.status(400).json({ error: 'to + text required' });
  }
  try {
    const result = await ctx.sock.sendMessage(to, { text });
    res.json({
      success: true,
      messageId: result && result.key && result.key.id,
    });
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
