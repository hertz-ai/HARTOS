"""
DLNA Bridge — Cast HARTOS remote desktop sessions to DLNA/UPnP renderers.

Architecture:
  Discovery: SSDP M-SEARCH (239.255.255.250:1900) for MediaRenderer devices
  Control:   UPnP AVTransport SOAP (SetAVTransportURI + Play/Stop)
  Streaming: MJPEG HTTP server from FrameCapture/WindowCapture output

HARTOS doesn't reimplement DLNA — it uses raw SSDP (stdlib socket) for discovery
and HTTP POST (urllib) for UPnP control. Optional async_upnp_client if available.

Reuses:
  - frame_capture.py: FrameCapture as default frame source
  - window_capture.py: WindowCapture for per-window casting
  - security.py: audit_session_event() for cast audit
"""

import http.server
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Generator, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


@dataclass
class DLNARenderer:
    """Discovered DLNA/UPnP renderer (smart TV, speaker, etc.)."""
    device_id: str            # UDN (Unique Device Name)
    friendly_name: str
    ip: str
    port: int
    location: str = ''        # SSDP Location URL
    control_url: str = ''     # AVTransport control URL
    supports_video: bool = True
    supports_audio: bool = True
    manufacturer: str = ''
    model: str = ''

    def to_dict(self) -> dict:
        return {
            'device_id': self.device_id,
            'friendly_name': self.friendly_name,
            'ip': self.ip,
            'port': self.port,
            'supports_video': self.supports_video,
            'supports_audio': self.supports_audio,
            'manufacturer': self.manufacturer,
            'model': self.model,
        }


@dataclass
class CastSession:
    """Active DLNA cast session."""
    cast_session_id: str
    renderer: DLNARenderer
    source_session_id: str   # Remote desktop session being cast
    stream_url: str          # MJPEG URL the renderer pulls from
    started_at: float = 0.0
    active: bool = True


class MJPEGStreamServer:
    """Lightweight HTTP server serving MJPEG stream from a frame source.

    Binds to a local port, serves multipart/x-mixed-replace JPEG frames.
    DLNA renderers pull from this URL.
    """

    def __init__(self, host: str = '0.0.0.0', port: int = 0):
        self._host = host
        self._port = port
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._frame_source: Optional[Callable] = None
        self._running = False
        self._current_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()

    def start(self, frame_source: Callable[[], Optional[bytes]],
              port: int = 0) -> str:
        """Start serving MJPEG stream.

        Args:
            frame_source: Callable that returns JPEG bytes (or None).
            port: Port to bind (0 = auto-assign).

        Returns:
            Stream URL (e.g., http://192.168.1.10:8554/stream.mjpeg).
        """
        self._frame_source = frame_source
        self._running = True

        # Find local IP
        local_ip = self._get_local_ip()

        # Build handler
        server_ref = self

        class MJPEGHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/stream.mjpeg':
                    self.send_error(404)
                    return

                self.send_response(200)
                self.send_header('Content-Type',
                                 'multipart/x-mixed-replace; boundary=--frame')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()

                while server_ref._running:
                    frame = None
                    if server_ref._frame_source:
                        try:
                            frame = server_ref._frame_source()
                        except Exception:
                            pass

                    if frame:
                        try:
                            self.wfile.write(b'--frame\r\n')
                            self.wfile.write(
                                b'Content-Type: image/jpeg\r\n')
                            self.wfile.write(
                                f'Content-Length: {len(frame)}\r\n\r\n'
                                .encode())
                            self.wfile.write(frame)
                            self.wfile.write(b'\r\n')
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    time.sleep(0.033)  # ~30fps cap

            def log_message(self, format, *args):
                pass  # Suppress request logging

        bind_port = port or self._port or 0
        self._server = http.server.HTTPServer(
            (self._host, bind_port), MJPEGHandler)
        actual_port = self._server.server_address[1]

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name='mjpeg-stream',
        )
        self._thread.start()

        url = f'http://{local_ip}:{actual_port}/stream.mjpeg'
        logger.info(f"MJPEG stream server started: {url}")
        return url

    def stop(self) -> None:
        """Stop the MJPEG stream server."""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    def _get_local_ip(self) -> str:
        """Get local IP address for LAN access."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'


class DLNABridge:
    """Cast HARTOS streams to DLNA/UPnP renderers.

    Discovery via SSDP M-SEARCH, control via UPnP AVTransport SOAP.
    Frames served as MJPEG over HTTP (renderers pull from URL).
    """

    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900

    @staticmethod
    def _default_stream_port():
        from core.port_registry import get_port
        return get_port('dlna_stream')

    def __init__(self):
        self._cast_sessions: Dict[str, CastSession] = {}
        self._stream_servers: Dict[str, MJPEGStreamServer] = {}
        self._renderer_cache: List[DLNARenderer] = []
        self._lock = threading.Lock()

    def discover_renderers(self,
                            timeout: float = 5.0) -> List[DLNARenderer]:
        """Discover DLNA/UPnP MediaRenderer devices on the LAN.

        Uses SSDP M-SEARCH multicast to find devices advertising
        urn:schemas-upnp-org:device:MediaRenderer:1.

        Args:
            timeout: Discovery timeout in seconds.

        Returns:
            List of discovered DLNARenderer devices.
        """
        renderers = []

        m_search = (
            'M-SEARCH * HTTP/1.1\r\n'
            f'HOST: {self.SSDP_ADDR}:{self.SSDP_PORT}\r\n'
            'MAN: "ssdp:discover"\r\n'
            'MX: 3\r\n'
            'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n'
            '\r\n'
        ).encode()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.IPPROTO_UDP)
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Send M-SEARCH
            sock.sendto(m_search, (self.SSDP_ADDR, self.SSDP_PORT))

            # Collect responses
            seen = set()
            end_time = time.time() + timeout

            while time.time() < end_time:
                try:
                    data, addr = sock.recvfrom(4096)
                    response = data.decode('utf-8', errors='ignore')

                    # Parse LOCATION header
                    location = self._parse_header(response, 'LOCATION')
                    if not location or location in seen:
                        continue
                    seen.add(location)

                    # Fetch device description
                    renderer = self._fetch_device_info(location, addr[0])
                    if renderer:
                        renderers.append(renderer)
                except socket.timeout:
                    break
                except Exception:
                    continue

            sock.close()
        except Exception as e:
            logger.debug(f"SSDP discovery failed: {e}")

        self._renderer_cache = renderers
        logger.info(f"DLNA discovery found {len(renderers)} renderer(s)")
        return renderers

    def cast_session(self, session_id: str, renderer_id: str,
                      frame_source: Optional[Callable] = None,
                      stream_port: int = 0) -> dict:
        """Cast a session to a DLNA renderer.

        Args:
            session_id: Remote desktop session to cast.
            renderer_id: Target renderer device_id.
            frame_source: Callable returning JPEG bytes. If None, uses
                          FrameCapture (full-screen).
            stream_port: Port for MJPEG server (0 = auto).

        Returns:
            {success, cast_session_id, renderer_name, stream_url}
        """
        # Find renderer
        renderer = None
        for r in self._renderer_cache:
            if r.device_id == renderer_id:
                renderer = r
                break

        if not renderer:
            return {'success': False,
                    'error': f'Renderer not found: {renderer_id}'}

        # Default frame source: full-screen capture
        if frame_source is None:
            frame_source = self._default_frame_source()

        # Start MJPEG stream server
        server = MJPEGStreamServer()
        port = stream_port or self._default_stream_port()
        stream_url = server.start(frame_source, port=port)

        # Send SetAVTransportURI to renderer
        ok = self._send_play(renderer, stream_url)
        if not ok:
            server.stop()
            return {'success': False,
                    'error': 'Failed to send play command to renderer'}

        # Track cast session
        import uuid
        cast_id = f'cast-{uuid.uuid4().hex[:8]}'
        cast = CastSession(
            cast_session_id=cast_id,
            renderer=renderer,
            source_session_id=session_id,
            stream_url=stream_url,
            started_at=time.time(),
        )

        with self._lock:
            self._cast_sessions[cast_id] = cast
            self._stream_servers[cast_id] = server

        # Audit
        self._audit('cast_started', session_id,
                     f'Renderer: {renderer.friendly_name}, URL: {stream_url}')

        logger.info(f"Casting to {renderer.friendly_name} ({stream_url})")
        return {
            'success': True,
            'cast_session_id': cast_id,
            'renderer_name': renderer.friendly_name,
            'stream_url': stream_url,
        }

    def stop_cast(self, cast_session_id: str) -> bool:
        """Stop a cast session."""
        with self._lock:
            cast = self._cast_sessions.pop(cast_session_id, None)
            server = self._stream_servers.pop(cast_session_id, None)

        if not cast:
            return False

        # Send Stop to renderer
        self._send_stop(cast.renderer)

        # Stop MJPEG server
        if server:
            server.stop()

        self._audit('cast_stopped', cast.source_session_id,
                     f'Renderer: {cast.renderer.friendly_name}')

        logger.info(f"Cast stopped: {cast_session_id}")
        return True

    def stop_all(self) -> None:
        """Stop all cast sessions."""
        with self._lock:
            cast_ids = list(self._cast_sessions.keys())
        for cid in cast_ids:
            self.stop_cast(cid)

    def get_cast_status(self) -> List[dict]:
        """Get status of all active cast sessions."""
        with self._lock:
            return [
                {
                    'cast_session_id': c.cast_session_id,
                    'renderer': c.renderer.friendly_name,
                    'source_session': c.source_session_id,
                    'stream_url': c.stream_url,
                    'started_at': c.started_at,
                    'active': c.active,
                }
                for c in self._cast_sessions.values()
            ]

    def get_cached_renderers(self) -> List[DLNARenderer]:
        """Get renderers from last discovery."""
        return self._renderer_cache

    # ── UPnP SOAP Control ─────────────────────────────────────

    def _send_play(self, renderer: DLNARenderer,
                    stream_url: str) -> bool:
        """Send SetAVTransportURI + Play SOAP commands to renderer."""
        if not renderer.control_url:
            logger.debug("No control URL for renderer")
            return False

        # SetAVTransportURI
        set_uri_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:SetAVTransportURI '
            'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            f'<CurrentURI>{stream_url}</CurrentURI>'
            '<CurrentURIMetaData></CurrentURIMetaData>'
            '</u:SetAVTransportURI>'
            '</s:Body></s:Envelope>'
        )

        ok = self._soap_post(
            renderer.control_url,
            'urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI',
            set_uri_body,
        )
        if not ok:
            return False

        # Play
        play_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Play '
            'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '<Speed>1</Speed>'
            '</u:Play>'
            '</s:Body></s:Envelope>'
        )

        return self._soap_post(
            renderer.control_url,
            'urn:schemas-upnp-org:service:AVTransport:1#Play',
            play_body,
        )

    def _send_stop(self, renderer: DLNARenderer) -> bool:
        """Send Stop SOAP command to renderer."""
        if not renderer.control_url:
            return False

        stop_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Stop '
            'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '</u:Stop>'
            '</s:Body></s:Envelope>'
        )

        return self._soap_post(
            renderer.control_url,
            'urn:schemas-upnp-org:service:AVTransport:1#Stop',
            stop_body,
        )

    def _soap_post(self, url: str, action: str, body: str) -> bool:
        """Send a SOAP POST to a UPnP control URL."""
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                data=body.encode('utf-8'),
                headers={
                    'Content-Type': 'text/xml; charset="utf-8"',
                    'SOAPACTION': f'"{action}"',
                },
                method='POST',
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status == 200
        except Exception as e:
            logger.debug(f"SOAP POST failed: {e}")
            return False

    # ── SSDP Helpers ──────────────────────────────────────────

    def _parse_header(self, response: str, header: str) -> Optional[str]:
        """Parse a header from an SSDP response."""
        for line in response.split('\r\n'):
            if line.upper().startswith(header.upper() + ':'):
                return line.split(':', 1)[1].strip()
        return None

    def _fetch_device_info(self, location: str,
                            ip: str) -> Optional[DLNARenderer]:
        """Fetch and parse device description XML from Location URL."""
        try:
            import urllib.request
            import xml.etree.ElementTree as ET

            resp = urllib.request.urlopen(location, timeout=5)
            xml_data = resp.read()
            root = ET.fromstring(xml_data)

            # Namespace
            ns = {'d': 'urn:schemas-upnp-org:device-1-0'}

            device = root.find('.//d:device', ns)
            if device is None:
                return None

            device_type = device.findtext('d:deviceType', '', ns)
            if 'MediaRenderer' not in device_type:
                return None

            friendly_name = device.findtext('d:friendlyName', 'Unknown', ns)
            udn = device.findtext('d:UDN', '', ns)
            manufacturer = device.findtext('d:manufacturer', '', ns)
            model = device.findtext('d:modelName', '', ns)

            # Find AVTransport control URL
            control_url = ''
            for service in device.findall('.//d:service', ns):
                stype = service.findtext('d:serviceType', '', ns)
                if 'AVTransport' in stype:
                    ctrl = service.findtext('d:controlURL', '', ns)
                    if ctrl:
                        # Make absolute
                        from urllib.parse import urljoin
                        control_url = urljoin(location, ctrl)
                    break

            # Parse port from location
            from urllib.parse import urlparse
            parsed = urlparse(location)
            port = parsed.port or 80

            return DLNARenderer(
                device_id=udn or f'dlna-{ip}:{port}',
                friendly_name=friendly_name,
                ip=ip,
                port=port,
                location=location,
                control_url=control_url,
                manufacturer=manufacturer,
                model=model,
            )
        except Exception as e:
            logger.debug(f"Failed to fetch device info from {location}: {e}")
            return None

    def _default_frame_source(self) -> Callable:
        """Create default frame source from FrameCapture."""
        try:
            from integrations.remote_desktop.frame_capture import FrameCapture
            capture = FrameCapture()
            return capture.capture_frame
        except Exception:
            # Return a blank frame generator
            def blank_frame():
                return None
            return blank_frame

    def _audit(self, event_type: str, session_id: str,
               detail: str) -> None:
        """Audit log cast events."""
        try:
            from integrations.remote_desktop.security import audit_session_event
            audit_session_event(event_type, session_id, 'dlna_bridge', detail)
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────

_dlna_bridge: Optional[DLNABridge] = None


def get_dlna_bridge() -> DLNABridge:
    """Get or create the singleton DLNABridge."""
    global _dlna_bridge
    if _dlna_bridge is None:
        _dlna_bridge = DLNABridge()
    return _dlna_bridge
