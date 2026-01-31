"""
HTTP Server for Plugin System.

Provides a lightweight HTTP server that plugins can use to register
custom routes and endpoints.
"""

import logging
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs
from enum import Enum

logger = logging.getLogger(__name__)


class HTTPMethod(Enum):
    """HTTP methods."""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"


@dataclass
class Route:
    """Represents an HTTP route."""
    path: str
    method: HTTPMethod
    handler: Callable
    plugin_name: str
    description: str = ""

    def matches(self, path: str, method: str) -> bool:
        """Check if this route matches the given path and method."""
        return self.path == path and self.method.value == method


@dataclass
class Request:
    """Represents an HTTP request."""
    method: str
    path: str
    query_params: Dict[str, List[str]]
    headers: Dict[str, str]
    body: Optional[bytes] = None

    def json(self) -> Any:
        """Parse body as JSON."""
        if self.body:
            return json.loads(self.body.decode('utf-8'))
        return None


@dataclass
class Response:
    """Represents an HTTP response."""
    status_code: int = 200
    body: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"

    def to_bytes(self) -> bytes:
        """Convert response body to bytes."""
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode('utf-8')
        return json.dumps(self.body).encode('utf-8')


class PluginHTTPServer:
    """
    HTTP server for plugin routes.

    Allows plugins to register custom HTTP endpoints and handles
    routing requests to the appropriate handlers.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self._host = host
        self._port = port
        self._routes: List[Route] = []
        self._server: Optional[HTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._middleware: List[Callable] = []

    @property
    def host(self) -> str:
        """Return the server host."""
        return self._host

    @property
    def port(self) -> int:
        """Return the server port."""
        return self._port

    @property
    def is_running(self) -> bool:
        """Return whether the server is running."""
        return self._running

    @property
    def routes(self) -> List[Route]:
        """Return all registered routes."""
        return self._routes.copy()

    def register_route(self, path: str, method: HTTPMethod, handler: Callable,
                       plugin_name: str, description: str = "") -> bool:
        """
        Register a new route.

        Args:
            path: The URL path for the route.
            method: The HTTP method.
            handler: The handler function.
            plugin_name: The name of the plugin registering the route.
            description: Optional description of the route.

        Returns:
            True if registration was successful, False otherwise.
        """
        # Check for duplicate routes
        for route in self._routes:
            if route.path == path and route.method == method:
                logger.warning(f"Route {method.value} {path} already registered")
                return False

        route = Route(
            path=path,
            method=method,
            handler=handler,
            plugin_name=plugin_name,
            description=description
        )
        self._routes.append(route)
        logger.info(f"Route registered: {method.value} {path} by {plugin_name}")
        return True

    def unregister_route(self, path: str, method: HTTPMethod) -> bool:
        """
        Unregister a specific route.

        Args:
            path: The URL path of the route.
            method: The HTTP method.

        Returns:
            True if unregistration was successful, False otherwise.
        """
        for route in self._routes:
            if route.path == path and route.method == method:
                self._routes.remove(route)
                logger.info(f"Route unregistered: {method.value} {path}")
                return True

        logger.warning(f"Route {method.value} {path} not found")
        return False

    def unregister_routes(self, plugin_name: str) -> int:
        """
        Unregister all routes for a plugin.

        Args:
            plugin_name: The name of the plugin.

        Returns:
            Number of routes unregistered.
        """
        routes_to_remove = [r for r in self._routes if r.plugin_name == plugin_name]
        for route in routes_to_remove:
            self._routes.remove(route)

        count = len(routes_to_remove)
        if count > 0:
            logger.info(f"Unregistered {count} routes for plugin {plugin_name}")
        return count

    def add_middleware(self, middleware: Callable) -> None:
        """
        Add middleware to the request pipeline.

        Args:
            middleware: A callable that takes (request, next) and returns a response.
        """
        self._middleware.append(middleware)

    def _find_route(self, path: str, method: str) -> Optional[Route]:
        """Find a matching route for the given path and method."""
        for route in self._routes:
            if route.matches(path, method):
                return route
        return None

    def _create_handler(self):
        """Create the HTTP request handler class."""
        server = self

        class RequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                logger.debug(f"HTTP: {format % args}")

            def _handle_request(self, method: str):
                parsed = urlparse(self.path)
                path = parsed.path
                query_params = parse_qs(parsed.query)

                # Read body for POST/PUT/PATCH
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length) if content_length > 0 else None

                # Create request object
                headers = {k: v for k, v in self.headers.items()}
                request = Request(
                    method=method,
                    path=path,
                    query_params=query_params,
                    headers=headers,
                    body=body
                )

                # Find route
                route = server._find_route(path, method)

                if route is None:
                    self._send_response(Response(
                        status_code=404,
                        body={"error": "Not Found", "path": path}
                    ))
                    return

                try:
                    # Call handler
                    result = route.handler(request)

                    if isinstance(result, Response):
                        self._send_response(result)
                    elif isinstance(result, dict) or isinstance(result, list):
                        self._send_response(Response(body=result))
                    else:
                        self._send_response(Response(body=str(result)))
                except Exception as e:
                    logger.exception(f"Error handling request: {e}")
                    self._send_response(Response(
                        status_code=500,
                        body={"error": "Internal Server Error", "message": str(e)}
                    ))

            def _send_response(self, response: Response):
                self.send_response(response.status_code)
                self.send_header('Content-Type', response.content_type)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.to_bytes())

            def do_GET(self):
                self._handle_request("GET")

            def do_POST(self):
                self._handle_request("POST")

            def do_PUT(self):
                self._handle_request("PUT")

            def do_DELETE(self):
                self._handle_request("DELETE")

            def do_PATCH(self):
                self._handle_request("PATCH")

            def do_OPTIONS(self):
                self._handle_request("OPTIONS")

            def do_HEAD(self):
                self._handle_request("HEAD")

        return RequestHandler

    def start(self, blocking: bool = False) -> bool:
        """
        Start the HTTP server.

        Args:
            blocking: If True, run in the current thread (blocking).
                      If False, run in a background thread.

        Returns:
            True if server started successfully.
        """
        if self._running:
            logger.warning("Server is already running")
            return False

        try:
            handler_class = self._create_handler()
            self._server = HTTPServer((self._host, self._port), handler_class)
            self._running = True

            logger.info(f"HTTP server starting on {self._host}:{self._port}")

            if blocking:
                self._server.serve_forever()
            else:
                self._server_thread = threading.Thread(
                    target=self._server.serve_forever,
                    daemon=True
                )
                self._server_thread.start()

            return True
        except Exception as e:
            logger.exception(f"Failed to start HTTP server: {e}")
            self._running = False
            return False

    def stop(self) -> bool:
        """
        Stop the HTTP server.

        Returns:
            True if server stopped successfully.
        """
        if not self._running:
            logger.warning("Server is not running")
            return False

        try:
            if self._server:
                self._server.shutdown()
                self._server.server_close()
                self._server = None

            if self._server_thread:
                self._server_thread.join(timeout=5)
                self._server_thread = None

            self._running = False
            logger.info("HTTP server stopped")
            return True
        except Exception as e:
            logger.exception(f"Error stopping HTTP server: {e}")
            return False

    def list_routes(self) -> List[Dict[str, str]]:
        """
        List all registered routes.

        Returns:
            List of route information.
        """
        return [
            {
                "path": route.path,
                "method": route.method.value,
                "plugin": route.plugin_name,
                "description": route.description
            }
            for route in self._routes
        ]

    def handle_request(self, request: Request) -> Response:
        """
        Handle a request directly (for testing).

        Args:
            request: The request to handle.

        Returns:
            The response.
        """
        route = self._find_route(request.path, request.method)

        if route is None:
            return Response(
                status_code=404,
                body={"error": "Not Found", "path": request.path}
            )

        try:
            result = route.handler(request)

            if isinstance(result, Response):
                return result
            elif isinstance(result, dict) or isinstance(result, list):
                return Response(body=result)
            else:
                return Response(body=str(result))
        except Exception as e:
            logger.exception(f"Error handling request: {e}")
            return Response(
                status_code=500,
                body={"error": "Internal Server Error", "message": str(e)}
            )
