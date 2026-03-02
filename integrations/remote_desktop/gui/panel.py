"""
Remote Desktop Glass Panel — Native system panel for LiquidUI Glass Shell.

Renders inside a frosted-glass panel window in the HARTOS desktop.
Fetches data from /api/remote-desktop/* endpoints and displays:
  - Device ID (large, copyable)
  - Engine status (RustDesk, Sunshine, Moonlight, Native)
  - Active sessions list
  - Host/Connect controls
  - Install recommendations

The actual rendering is done by JavaScript in liquid_ui_service.py
(loadRemoteDesktopPanel function). This module provides the Python-side
data aggregation that the API endpoints serve.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve.remote_desktop')


def get_panel_data() -> Dict[str, Any]:
    """Aggregate all data needed by the remote desktop glass panel.

    Returns dict consumed by the JS panel renderer and the API endpoints.
    """
    result = {
        'device_id': None,
        'formatted_id': None,
        'engines': {},
        'sessions': [],
        'install_recommendations': [],
    }

    # Device identity
    try:
        from integrations.remote_desktop.device_id import get_device_id, format_device_id
        device_id = get_device_id()
        result['device_id'] = device_id
        result['formatted_id'] = format_device_id(device_id)
    except Exception as e:
        logger.debug(f"Device ID unavailable: {e}")

    # Engine status
    try:
        from integrations.remote_desktop.engine_selector import get_all_status
        status = get_all_status()
        result['engines'] = status.get('engines', {})
        result['install_recommendations'] = status.get('install_recommendations', [])
    except Exception as e:
        logger.debug(f"Engine status unavailable: {e}")
        result['engines'] = {'native': {'available': True, 'engine': 'native'}}

    # Active sessions
    try:
        from integrations.remote_desktop.session_manager import get_session_manager
        sm = get_session_manager()
        sessions = sm.get_active_sessions()
        result['sessions'] = [
            {
                'session_id': s.session_id,
                'host_device_id': s.host_device_id,
                'mode': s.mode.value,
                'state': s.state.value,
                'viewers': s.viewer_device_ids,
            }
            for s in sessions
        ]
    except Exception as e:
        logger.debug(f"Session manager unavailable: {e}")

    return result


# ── JavaScript for LiquidUI Glass Shell ────────────────────────

PANEL_JS = """
function loadRemoteDesktopPanel(el, apis) {
  Promise.all(apis.map(u=>fetch(BACKEND+u,{signal:AbortSignal.timeout(5000)}).then(r=>r.json()).catch(()=>({}))))
    .then(([status,engines,sessions])=>{
      const did = status.formatted_id || 'Unknown';
      const deviceId = status.device_id || '';
      const engineList = status.engines || engines.engines || {};
      const sess = (sessions.sessions || status.active_sessions || []);
      const recs = engines.install_recommendations || status.install_recommendations || [];

      let html = '<div style="display:grid;gap:12px">';

      // Header + Device ID
      html += '<div style="display:flex;justify-content:space-between;align-items:center">';
      html += '<span style="font-weight:var(--hart-heading-weight);font-size:var(--hart-heading-size);color:var(--hart-heading)">Remote Desktop</span>';
      html += '<span class="mi material-icons-round" style="font-size:20px;color:var(--hart-active)">connected_tv</span>';
      html += '</div>';

      // Device ID (large, copyable)
      html += '<div style="padding:16px;border-radius:12px;background:var(--hart-surface);text-align:center;cursor:pointer" onclick="navigator.clipboard.writeText(\\''+deviceId+'\\').then(()=>this.querySelector(\\'.copy-hint\\').textContent=\\'Copied!\\')" title="Click to copy">';
      html += '<div style="font-size:11px;color:var(--hart-muted);margin-bottom:4px">Your Device ID</div>';
      html += '<div style="font-size:24px;font-weight:700;letter-spacing:3px;color:var(--hart-heading)">'+did+'</div>';
      html += '<div class="copy-hint" style="font-size:10px;color:var(--hart-muted);margin-top:4px">Click to copy</div>';
      html += '</div>';

      // Engines
      html += '<div style="font-weight:600;font-size:12px;color:var(--hart-muted);text-transform:uppercase;letter-spacing:1px">Engines</div>';
      for(const [name,info] of Object.entries(engineList)) {
        const avail = info.available;
        const color = avail ? 'var(--hart-active)' : 'var(--hart-muted)';
        const icon = avail ? 'check_circle' : 'cancel';
        html += statusRow(icon, name.charAt(0).toUpperCase()+name.slice(1), avail?'Available':'Not installed', color);
      }

      // Sessions
      if(sess.length > 0) {
        html += '<div style="font-weight:600;font-size:12px;color:var(--hart-muted);text-transform:uppercase;letter-spacing:1px;margin-top:4px">Active Sessions ('+sess.length+')</div>';
        for(const s of sess) {
          html += '<div style="padding:8px;border-radius:8px;background:var(--hart-surface);display:flex;justify-content:space-between;align-items:center">';
          html += '<span style="font-size:12px">'+s.session_id.substring(0,8)+' — '+s.mode+'</span>';
          html += '<span style="font-size:11px;color:var(--hart-active)">'+s.state+'</span>';
          html += '</div>';
        }
      }

      // Install recommendations
      if(recs.length > 0) {
        html += '<div style="font-weight:600;font-size:12px;color:var(--hart-muted);text-transform:uppercase;letter-spacing:1px;margin-top:4px">Recommended</div>';
        for(const r of recs) {
          html += '<div style="padding:8px;border-radius:8px;background:var(--hart-surface)">';
          html += '<div style="font-size:12px;font-weight:600">'+r.engine+'</div>';
          html += '<div style="font-size:11px;color:var(--hart-muted)">'+r.reason+'</div>';
          html += '</div>';
        }
      }

      // Action buttons
      html += '<div style="display:flex;gap:8px;margin-top:8px">';
      html += '<button onclick="fetch(BACKEND+\\'/api/remote-desktop/host\\',{method:\\'POST\\',headers:{\\'Content-Type\\':\\'application/json\\'},body:JSON.stringify({engine:\\'auto\\'})}).then(r=>r.json()).then(d=>{alert(\\'Hosting started!\\\\nDevice ID: \\'+d.formatted_id+\\'\\\\nPassword: \\'+d.password)})" style="flex:1;padding:10px;border:none;border-radius:8px;background:var(--hart-active);color:white;font-weight:600;cursor:pointer">Host</button>';
      html += '<button onclick="const id=prompt(\\'Enter Device ID:\\');if(id){const pw=prompt(\\'Password:\\');if(pw)fetch(BACKEND+\\'/api/remote-desktop/connect\\',{method:\\'POST\\',headers:{\\'Content-Type\\':\\'application/json\\'},body:JSON.stringify({device_id:id,password:pw})}).then(r=>r.json()).then(d=>alert(d.message||d.error||JSON.stringify(d)))}" style="flex:1;padding:10px;border:none;border-radius:8px;background:var(--hart-surface);color:var(--hart-heading);font-weight:600;cursor:pointer;border:1px solid var(--hart-border)">Connect</button>';
      html += '</div>';

      html += '</div>';
      el.innerHTML = html;
    });
}
"""
