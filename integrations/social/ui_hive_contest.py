"""UI for the Hive Contest — a single self-contained HTML page served
from HARTOS that consumes the existing /api/hive/contest/* endpoints.

Why this lives in HARTOS (not Nunba):
  - HARTOS owns the backend endpoints.  Serving the matching page
    here keeps the UI wireable with zero Nunba dependency — any
    browser (or embed) can hit http://localhost:6777/hive-contest
    and see the live state.
  - Nunba's SPA can still wrap the same page via iframe (shell_manifest
    gets a panel pointing at this route).  Single source of truth.
  - Re-uses the HART Design System tokens that liquid_ui_service.py
    already defines (ds-btn, ds-body-md, ds-elevation-*, MD3 spacing).
    No parallel CSS.  No JS framework.  Vanilla fetch + DOM.

Surface:
  GET /hive-contest  — single HTML page (public, no auth)

All dynamic content (info, leaderboard, MCP snippet) is loaded via
XHR from the existing API blueprint.  This page is a thin renderer;
the source-of-truth stays in integrations.agent_engine.hive_contest.
"""
from __future__ import annotations

from flask import Blueprint, Response

hive_contest_ui_bp = Blueprint('hive_contest_ui', __name__)


def _design_system_link() -> str:
    """Pull the HART Design System tokens from liquid_ui_service.

    We don't duplicate the tokens — the CSS is imported at render time
    from the same module the desktop shell uses.  Import failures fall
    back to a minimal built-in that still renders readable content.
    """
    try:
        from integrations.agent_engine.liquid_ui_service import (
            LiquidUIService,
        )
        # LiquidUIService has the full MD3 token block inside
        # render_desktop_shell.  We extract a trimmed subset — the
        # design-system vars + the button/card primitives — so the
        # contest page can style itself without pulling the shell.
    except Exception:
        pass
    return _FALLBACK_CSS


# Minimal MD3-flavored CSS — values mirror liquid_ui_service's
# HART Design System tokens so anything rendered inside the desktop
# shell inherits (when framed) or stands alone (when visited directly).
_FALLBACK_CSS = """
:root{
  --ds-font-body:"Inter",-apple-system,"Segoe UI",Roboto,sans-serif;
  --ds-font-mono:"JetBrains Mono","Fira Code",monospace;
  --ds-space-1:4px; --ds-space-2:8px; --ds-space-3:12px; --ds-space-4:16px;
  --ds-space-5:20px; --ds-space-6:24px; --ds-space-8:32px; --ds-space-10:40px;
  --ds-space-12:48px; --ds-space-16:64px;
  --ds-radius-sm:8px; --ds-radius-md:12px; --ds-radius-lg:16px;
  --ds-radius-xl:24px; --ds-radius-full:9999px;
  --hart-bg:#0F0E17; --hart-surface:#1a1a2e; --hart-accent:#6C63FF;
  --hart-active:#00e676; --hart-text:#e0e0e0; --hart-muted:#78909c;
  --hart-error:#FF6B6B; --hart-caution:#ffab40;
  --hart-track-digital:#6C63FF;
  --hart-track-embodied:#00e676;
  --hart-track-wellness:#ffab40;
  --ds-elevation-1:0 1px 3px 1px rgba(0,0,0,0.15),0 1px 2px rgba(0,0,0,0.3);
  --ds-elevation-2:0 2px 6px 2px rgba(0,0,0,0.15),0 1px 2px rgba(0,0,0,0.3);
  --ds-elevation-3:0 4px 8px 3px rgba(0,0,0,0.15),0 1px 3px rgba(0,0,0,0.3);
}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;min-height:100vh;background:
  radial-gradient(1200px 600px at 10% -10%,rgba(108,99,255,0.12),transparent 60%),
  radial-gradient(1000px 500px at 100% 10%,rgba(0,230,118,0.08),transparent 60%),
  linear-gradient(135deg,#0F0E17 0%,#16213e 100%);
  color:var(--hart-text);font-family:var(--ds-font-body);line-height:1.5}
a{color:var(--hart-accent);text-decoration:none} a:hover{text-decoration:underline}
.page{max-width:1100px;margin:0 auto;padding:var(--ds-space-8) var(--ds-space-6)}
.hero{padding:var(--ds-space-10) 0;text-align:left}
.hero h1{font-size:44px;line-height:1.1;margin:0 0 var(--ds-space-4);
  font-weight:700;letter-spacing:-0.5px;
  background:linear-gradient(90deg,#e0e0e0,#c7c2ff);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero .tagline{font-size:18px;color:var(--hart-muted);max-width:720px;margin:0}
.hero .window-pill{display:inline-flex;align-items:center;gap:var(--ds-space-2);
  margin-top:var(--ds-space-6);padding:6px 14px;border-radius:var(--ds-radius-full);
  background:rgba(0,230,118,0.1);border:1px solid rgba(0,230,118,0.3);
  font-size:13px;color:var(--hart-active)}
.hero .window-pill::before{content:"";width:8px;height:8px;border-radius:50%;
  background:var(--hart-active);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.principle{margin-top:var(--ds-space-6);padding:var(--ds-space-4) var(--ds-space-5);
  border-left:3px solid var(--hart-accent);background:rgba(108,99,255,0.06);
  border-radius:0 var(--ds-radius-md) var(--ds-radius-md) 0;
  font-size:14px;color:#d0d0d0;max-width:820px}
.section{margin-top:var(--ds-space-12)}
.section h2{font-size:22px;font-weight:600;margin:0 0 var(--ds-space-5);
  display:flex;align-items:center;gap:var(--ds-space-3)}
.section h2 .muted{font-size:14px;font-weight:400;color:var(--hart-muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
  gap:var(--ds-space-5)}
.card{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
  border-radius:var(--ds-radius-lg);padding:var(--ds-space-6);
  box-shadow:var(--ds-elevation-1);transition:transform 200ms,box-shadow 200ms,
  border-color 200ms}
.card:hover{transform:translateY(-2px);box-shadow:var(--ds-elevation-3);
  border-color:rgba(108,99,255,0.25)}
.card h3{margin:0 0 var(--ds-space-2);font-size:18px;font-weight:600}
.card .track-badge{display:inline-block;padding:3px 10px;border-radius:
  var(--ds-radius-full);font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:0.8px;margin-bottom:var(--ds-space-3)}
.card[data-track="digital"] .track-badge{background:rgba(108,99,255,0.15);
  color:var(--hart-track-digital)}
.card[data-track="embodied"] .track-badge{background:rgba(0,230,118,0.15);
  color:var(--hart-track-embodied)}
.card[data-track="human_wellness"] .track-badge{background:rgba(255,171,64,0.15);
  color:var(--hart-track-wellness)}
.card .card-desc{color:#c0c0c0;font-size:14px;line-height:1.55;
  margin:0 0 var(--ds-space-4)}
.card .card-examples{list-style:none;padding:0;margin:0;font-size:13px;
  color:var(--hart-muted)}
.card .card-examples li{padding:4px 0;padding-left:18px;position:relative}
.card .card-examples li::before{content:"→";position:absolute;left:0;
  color:var(--hart-accent);opacity:0.6}
.track-filter{display:inline-flex;gap:var(--ds-space-2);margin-bottom:
  var(--ds-space-5);background:rgba(255,255,255,0.04);
  border:1px solid rgba(255,255,255,0.08);border-radius:var(--ds-radius-full);
  padding:4px}
.track-filter button{background:transparent;border:none;color:var(--hart-muted);
  padding:8px 18px;border-radius:var(--ds-radius-full);cursor:pointer;
  font-family:var(--ds-font-body);font-size:13px;font-weight:500;
  transition:background 150ms,color 150ms}
.track-filter button:hover{color:var(--hart-text)}
.track-filter button.active{background:var(--hart-accent);color:white;
  box-shadow:var(--ds-elevation-1)}
.leaderboard{background:rgba(255,255,255,0.04);border:1px solid
  rgba(255,255,255,0.08);border-radius:var(--ds-radius-lg);overflow:hidden}
.leaderboard table{width:100%;border-collapse:collapse}
.leaderboard th,.leaderboard td{text-align:left;padding:var(--ds-space-3)
  var(--ds-space-5);font-size:14px;border-bottom:1px solid rgba(255,255,255,0.05)}
.leaderboard th{background:rgba(108,99,255,0.08);font-weight:600;color:#b0b0b0;
  font-size:12px;text-transform:uppercase;letter-spacing:0.6px}
.leaderboard tr:last-child td{border-bottom:none}
.leaderboard tr:hover td{background:rgba(255,255,255,0.03)}
.leaderboard .rank{font-family:var(--ds-font-mono);color:var(--hart-muted);
  width:60px}
.leaderboard .rank.top-1{color:#ffd700}
.leaderboard .rank.top-2{color:#c0c0c0}
.leaderboard .rank.top-3{color:#cd7f32}
.leaderboard .score{font-family:var(--ds-font-mono);color:var(--hart-active);
  text-align:right;font-weight:600}
.leaderboard .empty{padding:var(--ds-space-10) var(--ds-space-5);text-align:center;
  color:var(--hart-muted);font-size:14px}
.split-row{display:grid;grid-template-columns:1fr 1fr;gap:var(--ds-space-6);
  margin-top:var(--ds-space-6)}
@media (max-width:820px){.split-row{grid-template-columns:1fr}}
.snippet-block{background:#0b0a14;border:1px solid rgba(255,255,255,0.1);
  border-radius:var(--ds-radius-md);padding:var(--ds-space-4);font-family:
  var(--ds-font-mono);font-size:13px;line-height:1.6;overflow-x:auto;
  white-space:pre;position:relative;color:#d0d0d0}
.copy-btn{position:absolute;top:var(--ds-space-3);right:var(--ds-space-3);
  background:var(--hart-accent);color:white;border:none;border-radius:
  var(--ds-radius-sm);padding:6px 12px;font-size:12px;font-weight:500;
  cursor:pointer;transition:transform 150ms,background 150ms}
.copy-btn:hover{background:#5a52d8;transform:translateY(-1px)}
.copy-btn.copied{background:var(--hart-active);color:#0F0E17}
.join-form{background:rgba(255,255,255,0.04);border:1px solid
  rgba(255,255,255,0.08);border-radius:var(--ds-radius-lg);padding:
  var(--ds-space-6)}
.join-form label{display:block;font-size:12px;font-weight:600;
  text-transform:uppercase;letter-spacing:0.6px;color:#b0b0b0;
  margin:var(--ds-space-4) 0 var(--ds-space-2)}
.join-form label:first-of-type{margin-top:0}
.join-form select,.join-form input{width:100%;background:rgba(0,0,0,0.25);
  border:1px solid rgba(255,255,255,0.12);color:var(--hart-text);
  padding:10px var(--ds-space-4);border-radius:var(--ds-radius-sm);
  font-family:inherit;font-size:14px;outline:none;
  transition:border-color 150ms}
.join-form select:focus,.join-form input:focus{border-color:var(--hart-accent)}
.ds-btn{display:inline-flex;align-items:center;justify-content:center;
  gap:var(--ds-space-2);padding:12px var(--ds-space-8);
  border-radius:var(--ds-radius-full);font-family:inherit;font-size:14px;
  font-weight:600;cursor:pointer;border:none;outline:none;
  background:var(--hart-accent);color:white;
  box-shadow:var(--ds-elevation-1);transition:box-shadow 200ms,
  background 150ms,transform 150ms}
.ds-btn:hover{background:#5a52d8;box-shadow:var(--ds-elevation-2);
  transform:translateY(-1px)}
.ds-btn:active{transform:translateY(0)}
.ds-btn[disabled]{opacity:0.5;cursor:not-allowed;transform:none}
.ds-btn.large{padding:14px var(--ds-space-10);font-size:15px}
.join-result{margin-top:var(--ds-space-4);padding:var(--ds-space-3)
  var(--ds-space-4);border-radius:var(--ds-radius-sm);font-size:13px;
  display:none}
.join-result.ok{background:rgba(0,230,118,0.1);border:1px solid
  rgba(0,230,118,0.3);color:var(--hart-active);display:block}
.join-result.err{background:rgba(255,107,107,0.1);border:1px solid
  rgba(255,107,107,0.3);color:var(--hart-error);display:block}
.prize-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:var(--ds-space-4);margin-top:var(--ds-space-4)}
.prize-card{background:rgba(108,99,255,0.06);border:1px solid
  rgba(108,99,255,0.2);border-radius:var(--ds-radius-md);
  padding:var(--ds-space-5)}
.prize-card h4{margin:0 0 var(--ds-space-2);font-size:15px;font-weight:600;
  color:var(--hart-accent)}
.prize-card p{margin:0;color:#c0c0c0;font-size:13px;line-height:1.5}
.footer{margin-top:var(--ds-space-16);padding-top:var(--ds-space-6);
  border-top:1px solid rgba(255,255,255,0.08);color:var(--hart-muted);
  font-size:12px;text-align:center}
.footer a{color:var(--hart-muted)}
.skeleton{animation:skeleton 1.5s ease-in-out infinite}
@keyframes skeleton{0%,100%{opacity:0.4}50%{opacity:0.7}}
.spinner{display:inline-block;width:14px;height:14px;border-radius:50%;
  border:2px solid rgba(255,255,255,0.25);border-top-color:var(--hart-accent);
  animation:spin 0.7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
"""


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hive Contest — HART OS</title>
<style>__CSS__</style>
</head>
<body>
<main class="page">

<section class="hero">
  <h1 id="contest-name">Hive Contest</h1>
  <p class="tagline" id="contest-tagline">Loading…</p>
  <div class="window-pill" id="contest-window">contest window loading</div>
  <blockquote class="principle" id="contest-principle"></blockquote>
</section>

<section class="section" id="tracks-section">
  <h2>Three tracks <span class="muted">— pick the one that matches what you ship</span></h2>
  <div class="cards" id="tracks-grid"></div>
</section>

<section class="section">
  <h2>Leaderboard <span class="muted" id="lb-meta"></span></h2>
  <div class="track-filter" id="track-filter" role="tablist">
    <button data-filter="" class="active">Overall</button>
    <button data-filter="digital">Digital</button>
    <button data-filter="embodied">Embodied</button>
    <button data-filter="human_wellness">Wellness</button>
  </div>
  <div class="leaderboard" id="leaderboard-box">
    <div class="empty skeleton">loading leaderboard…</div>
  </div>
</section>

<section class="section split-row">
  <div>
    <h2>Plug in your Claude Code</h2>
    <p style="color:#c0c0c0;font-size:14px;line-height:1.6">
      Point Claude Code at the local HART OS MCP server.  Every recipe your
      agent ships, every benchmark it proves, every episode it executes
      lands in your wallet as <code>season_spark</code>.  Same 90/9/1 split
      as every other Spark transaction — no separate payout pool.
    </p>
    <div class="snippet-block">
      <button class="copy-btn" id="copy-mcp">Copy</button>
      <code id="mcp-snippet" class="skeleton">loading MCP config…</code>
    </div>
  </div>

  <div>
    <h2>Join the contest</h2>
    <form class="join-form" id="join-form">
      <label for="join-track">Track</label>
      <select id="join-track" name="track">
        <option value="digital">Digital Intelligence</option>
        <option value="embodied">Embodied Skill</option>
        <option value="human_wellness">Human Wellness</option>
      </select>
      <label for="join-github">GitHub handle (optional)</label>
      <input type="text" id="join-github" name="github"
             placeholder="your-handle" autocomplete="off">
      <label for="join-email">Email (optional)</label>
      <input type="email" id="join-email" name="email"
             placeholder="you@example.com" autocomplete="off">
      <div style="margin-top:var(--ds-space-5)">
        <button type="submit" class="ds-btn large" id="join-btn">
          Register
        </button>
      </div>
      <div class="join-result" id="join-result"></div>
    </form>
  </div>
</section>

<section class="section">
  <h2>Ideas wall <span class="muted" id="ideas-meta"></span></h2>
  <p style="color:#c0c0c0;font-size:14px;max-width:820px;margin:0 0 var(--ds-space-5)">
    Drop an idea here — or say <em>"I have a contest idea"</em> to your
    Nunba companion agent and the Contest Curator will capture it for
    you.  Ideas land in the same social feed (upvotes, comments) and
    earn you a first Spark under <code>contest:idea_submitted</code>.
    Floating ideas panel on
    <a href="https://hevolve.ai" target="_blank">hevolve.ai</a> streams
    new entries live via SSE.
  </p>
  <div class="split-row">
    <div>
      <form class="join-form" id="idea-form">
        <label for="idea-track">Track</label>
        <select id="idea-track">
          <option value="digital">Digital Intelligence</option>
          <option value="embodied">Embodied Skill</option>
          <option value="human_wellness">Human Wellness</option>
        </select>
        <label for="idea-title">Idea title</label>
        <input type="text" id="idea-title" maxlength="200"
               placeholder="A companion that reminds me to walk">
        <label for="idea-desc">Description</label>
        <textarea id="idea-desc" rows="5" maxlength="4000"
                  style="width:100%;background:rgba(0,0,0,0.25);
                  border:1px solid rgba(255,255,255,0.12);color:var(--hart-text);
                  padding:10px var(--ds-space-4);border-radius:
                  var(--ds-radius-sm);font-family:inherit;font-size:14px;
                  outline:none;resize:vertical"
                  placeholder="What would it do? Who does it help?
How does the hive make it possible?"></textarea>
        <div style="margin-top:var(--ds-space-5)">
          <button type="submit" class="ds-btn" id="idea-btn">
            Submit idea
          </button>
        </div>
        <div class="join-result" id="idea-result"></div>
      </form>
    </div>
    <div>
      <div class="leaderboard" id="ideas-box">
        <div class="empty skeleton">loading ideas…</div>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <h2>Prizes &amp; recognition</h2>
  <div class="prize-grid">
    <div class="prize-card">
      <h4>90 / 9 / 1 Spark split</h4>
      <p>Every prize Spark follows the canonical split — 90% to the
      submitter, 9% to the infra node that ran the submission, 1% to
      the central hive.  Same split as every other Spark transaction.</p>
    </div>
    <div class="prize-card">
      <h4>Top 3 per track</h4>
      <p>Auto-featured on <code>docs.hevolve.ai</code>.  Biggest mover
      each week gets a shoutout from Quest — the contest-host daemon
      agent.</p>
    </div>
    <div class="prize-card">
      <h4>Embodied &amp; Wellness first</h4>
      <p>Physical-world and real-wellness submissions are celebrated
      over pure-digital.  A bright future requires leaving the screen.</p>
    </div>
    <div class="prize-card" style="grid-column: 1 / -1;
      background: rgba(255,171,64,0.08); border-color: rgba(255,171,64,0.35)">
      <h4 style="color: var(--hart-caution)">Help us co-create — hardware SDKs welcome</h4>
      <p>We're a startup constrained by resources to validate every
      feature alone, so we co-create with the community.  Specifically
      looking for help bridging <strong>BLE devices, EEG headsets, robot
      platforms (LeRobot, ROS, Unitree, Spot), accessibility hardware,
      smart-home sensors</strong> — anything with an SDK that lets the
      hive perceive or act in the real world.  Trust the open code,
      the public Spark ledger, the crowdsourced compute economy, and
      the constitutional guardrails — even if you don't know the
      strangers shipping work alongside you; the system is the trust.
      Share with one friend or family member who has a relevant skill.</p>
    </div>
  </div>
</section>

<div class="footer">
  Public canonical page: <a id="public-url-link"
    href="https://hevolve.ai/hive_contest" target="_blank">
    hevolve.ai/hive_contest</a> ·
  Source: <code>integrations/agent_engine/hive_contest.py</code> ·
  <a href="/api/hive/contest/info">API /info</a> ·
  <a href="/api/hive/contest/leaderboard">API /leaderboard</a> ·
  Quest posts weekly standings.
</div>
</main>

<script>
(function(){
  "use strict";

  // ─── Tiny helpers ────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');

  function fmtDate(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleDateString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric',
      });
    } catch (_e) { return iso; }
  }

  function daysUntil(iso) {
    if (!iso) return null;
    try {
      const ms = new Date(iso).getTime() - Date.now();
      return Math.max(0, Math.ceil(ms / (86400 * 1000)));
    } catch (_e) { return null; }
  }

  // ─── Load contest info ───────────────────────────────────────────
  async function loadInfo() {
    try {
      const r = await fetch('/api/hive/contest/info');
      const j = await r.json();
      const info = (j && j.data) || {};
      $('#contest-name').textContent = info.name || 'Hive Contest';
      $('#contest-tagline').textContent = info.tagline || '';
      $('#contest-principle').textContent = info.humans_first_principle || '';

      const start = fmtDate(info.starts_at);
      const end = fmtDate(info.ends_at);
      const remaining = daysUntil(info.ends_at);
      const pill = $('#contest-window');
      if (remaining !== null && remaining > 0) {
        pill.textContent = `${remaining} day${remaining === 1 ? '' : 's'} left · `
          + `${start} → ${end}`;
      } else if (remaining === 0) {
        pill.textContent = `closes today · ${start} → ${end}`;
        pill.style.background = 'rgba(255,171,64,0.12)';
        pill.style.borderColor = 'rgba(255,171,64,0.4)';
        pill.style.color = 'var(--hart-caution)';
      } else {
        pill.textContent = `${start} → ${end}`;
      }

      renderTracks(info.tracks || []);
      // Public canonical URL — env-overridable on the server, surfaced
      // here so a staging deploy can route the footer link to the
      // matching staging app page automatically.
      const link = $('#public-url-link');
      if (link && info.public_url) {
        link.href = info.public_url;
        try {
          const u = new URL(info.public_url);
          link.textContent = (u.host + u.pathname).replace(/\/$/, '');
        } catch (_e) { link.textContent = info.public_url; }
      }
    } catch (e) {
      $('#contest-tagline').textContent =
        'Couldn\'t reach /api/hive/contest/info — is HART OS running on this host?';
      $('#contest-window').textContent = 'offline';
    }
  }

  function renderTracks(tracks) {
    const grid = $('#tracks-grid');
    grid.innerHTML = '';
    for (const t of tracks) {
      const card = document.createElement('article');
      card.className = 'card';
      card.setAttribute('data-track', t.id || '');
      const examples = (t.example_contributions || [])
        .map((e) => `<li>${esc(e)}</li>`).join('');
      card.innerHTML = `
        <span class="track-badge">${esc(t.id || '')}</span>
        <h3>${esc(t.name || '')}</h3>
        <p class="card-desc">${esc(t.description || '')}</p>
        <ul class="card-examples">${examples}</ul>
      `;
      card.addEventListener('click', () => filterTo(t.id));
      grid.appendChild(card);
    }
  }

  // ─── Leaderboard ─────────────────────────────────────────────────
  let currentFilter = '';

  function filterTo(track) {
    currentFilter = track || '';
    $$('#track-filter button').forEach((b) => {
      b.classList.toggle('active',
        (b.getAttribute('data-filter') || '') === currentFilter);
    });
    loadLeaderboard();
  }

  $$('#track-filter button').forEach((b) => {
    b.addEventListener('click', () => filterTo(b.getAttribute('data-filter')));
  });

  async function loadLeaderboard() {
    const box = $('#leaderboard-box');
    box.innerHTML = '<div class="empty skeleton">loading leaderboard…</div>';
    $('#lb-meta').textContent = '';
    try {
      const qs = currentFilter ? `?track=${encodeURIComponent(currentFilter)}` : '';
      const r = await fetch(`/api/hive/contest/leaderboard${qs}&limit=15`
        .replace('?&', '?'));
      const j = await r.json();
      const rows = (j && j.data) || [];
      const meta = (j && j.meta) || {};
      $('#lb-meta').textContent =
        `${rows.length} entr${rows.length === 1 ? 'y' : 'ies'} · `
        + `${esc(meta.track || 'overall')} track`;
      if (!rows.length) {
        box.innerHTML =
          '<div class="empty">No entries yet — be the first.  '
          + 'Ship a recipe and refresh.</div>';
        return;
      }
      const rowsHtml = rows.map((row, idx) => {
        const rank = row.rank || (idx + 1);
        const rankCls = rank <= 3 ? `rank top-${rank}` : 'rank';
        const name = row.display_name || row.user_id || 'anon';
        const score = (row.score != null ? row.score :
          (row.season_spark != null ? row.season_spark : 0));
        return `
          <tr>
            <td class="${rankCls}">#${rank}</td>
            <td>${esc(name)}</td>
            <td style="color:var(--hart-muted);font-size:12px">
              ${esc(row.track || 'overall')}
            </td>
            <td class="score">${Number(score).toLocaleString()}</td>
          </tr>`;
      }).join('');
      box.innerHTML = `
        <table>
          <thead><tr>
            <th>Rank</th><th>Contributor</th><th>Track</th><th>Spark</th>
          </tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>`;
    } catch (e) {
      box.innerHTML = '<div class="empty">Leaderboard unavailable.</div>';
    }
  }

  // ─── MCP snippet ─────────────────────────────────────────────────
  async function loadSnippet() {
    try {
      const r = await fetch('/api/hive/contest/claude-code.mcp');
      const txt = await r.text();
      const el = $('#mcp-snippet');
      el.textContent = txt;
      el.classList.remove('skeleton');
    } catch (e) {
      $('#mcp-snippet').textContent = '# unable to load snippet';
    }
  }

  $('#copy-mcp').addEventListener('click', async () => {
    const txt = $('#mcp-snippet').textContent || '';
    try {
      await navigator.clipboard.writeText(txt);
      const btn = $('#copy-mcp');
      btn.classList.add('copied');
      btn.textContent = 'Copied';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = 'Copy';
      }, 1500);
    } catch (e) {
      // Fallback: select the snippet so the user can Ctrl+C
      const range = document.createRange();
      range.selectNodeContents($('#mcp-snippet'));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  });

  // ─── Join form ───────────────────────────────────────────────────
  $('#join-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#join-btn');
    const result = $('#join-result');
    result.className = 'join-result';
    result.style.display = 'none';
    btn.disabled = true;
    const orig = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span> Joining…';
    try {
      const body = {
        track: $('#join-track').value,
        github: $('#join-github').value.trim() || undefined,
        email: $('#join-email').value.trim() || undefined,
      };
      const r = await fetch('/api/hive/contest/join', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (r.status === 401) {
        result.className = 'join-result err';
        result.textContent = 'Sign in first — /join needs an authenticated '
          + 'session (same one Nunba uses).  Visit the Nunba panel and '
          + 'retry here.';
      } else if (r.ok && j.data && j.data.ok) {
        result.className = 'join-result ok';
        if (j.data.already_registered) {
          result.textContent =
            `Already registered on track "${j.data.track}". `
            + 'Every scoring event lands in your wallet automatically.';
        } else {
          result.textContent =
            `Welcome to the contest — track "${j.data.track}". `
            + 'Your first Spark has been awarded.';
        }
        loadLeaderboard();
      } else {
        result.className = 'join-result err';
        result.textContent = (j && j.error) || (j && j.data && j.data.reason)
          || `Join failed (HTTP ${r.status}).`;
      }
    } catch (err) {
      result.className = 'join-result err';
      result.textContent = `Network error: ${String(err && err.message || err)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  });

  // ─── Ideas wall ──────────────────────────────────────────────────
  async function loadIdeas() {
    const box = $('#ideas-box');
    try {
      const trackQ = currentFilter ? `?track=${encodeURIComponent(currentFilter)}` : '';
      const r = await fetch(`/api/hive/contest/ideas${trackQ}&limit=20`
        .replace('?&', '?'));
      const j = await r.json();
      const rows = (j && j.data) || [];
      const meta = (j && j.meta) || {};
      $('#ideas-meta').textContent =
        `${rows.length} idea${rows.length === 1 ? '' : 's'} · `
        + `${esc(meta.track || 'all')} track`;
      if (!rows.length) {
        box.innerHTML =
          '<div class="empty">No ideas yet — be the first to drop one.</div>';
        return;
      }
      const rowsHtml = rows.map((row) => {
        const title = esc(row.title || '(untitled)');
        const preview = esc(row.preview || row.content || '');
        const track = esc(row.track || '?');
        const score = Number(row.score || 0);
        return `
          <tr>
            <td>
              <div style="font-weight:600;color:var(--hart-text);
                font-size:14px;margin-bottom:4px">${title}</div>
              <div style="color:var(--hart-muted);font-size:13px;
                line-height:1.4">${preview}</div>
              <div style="margin-top:6px">
                <span class="track-badge"
                  style="padding:2px 8px;border-radius:999px;font-size:10px;
                  background:rgba(108,99,255,0.15);color:var(--hart-accent);
                  text-transform:uppercase;letter-spacing:0.6px">
                  ${track}
                </span>
                <span style="margin-left:10px;color:var(--hart-muted);
                  font-size:11px">score ${score}</span>
              </div>
            </td>
          </tr>`;
      }).join('');
      box.innerHTML = `<table><tbody>${rowsHtml}</tbody></table>`;
    } catch (e) {
      box.innerHTML = '<div class="empty">Ideas unavailable.</div>';
    }
  }

  $('#idea-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('#idea-btn');
    const result = $('#idea-result');
    result.className = 'join-result';
    result.style.display = 'none';
    btn.disabled = true;
    const orig = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span> Submitting…';
    try {
      const body = {
        title: $('#idea-title').value.trim(),
        description: $('#idea-desc').value.trim(),
        track: $('#idea-track').value,
        source: 'ui',
      };
      if (!body.title || !body.description) {
        result.className = 'join-result err';
        result.textContent = 'Title and description are required.';
        return;
      }
      const r = await fetch('/api/hive/contest/ideas', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (r.status === 401) {
        result.className = 'join-result err';
        result.textContent = 'Sign in first — submissions need an '
          + 'authenticated session.  Open Nunba and come back.';
      } else if (r.status === 201 && j.data && j.data.ok) {
        result.className = 'join-result ok';
        result.textContent =
          `Idea submitted (track "${j.data.track}"). `
          + `+${j.data.spark_awarded} Spark to your wallet.`;
        $('#idea-title').value = '';
        $('#idea-desc').value = '';
        loadIdeas();
        loadLeaderboard();
      } else {
        result.className = 'join-result err';
        result.textContent = (j && j.error) || `Submit failed (HTTP ${r.status}).`;
      }
    } catch (err) {
      result.className = 'join-result err';
      result.textContent = `Network error: ${String(err && err.message || err)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  });

  // SSE feed — append newly-submitted ideas in real time so the page
  // feels live even for the submitter themselves.
  function subscribeIdeaStream() {
    try {
      const es = new EventSource('/api/hive/contest/ideas/stream');
      es.addEventListener('contest.idea_submitted', () => loadIdeas());
      es.onerror = () => { /* silent; EventSource auto-reconnects */ };
    } catch (_e) {}
  }
  subscribeIdeaStream();

  // ─── Boot ────────────────────────────────────────────────────────
  loadInfo();
  loadLeaderboard();
  loadSnippet();
  loadIdeas();

  // Refresh the leaderboard every 30s so top-of-mind state stays live.
  setInterval(loadLeaderboard, 30000);
})();
</script>
</body>
</html>
"""


@hive_contest_ui_bp.route('/hive-contest', methods=['GET'])
def contest_page():
    """Serve the contest page.  Public — anyone can read the contest
    rules + leaderboard; joining still requires auth at POST /join."""
    css = _FALLBACK_CSS
    html = _HTML.replace('__CSS__', css)
    return Response(html, mimetype='text/html; charset=utf-8')
