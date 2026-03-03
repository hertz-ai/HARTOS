"""
HevolveSocial - MCP Tool Registry Blueprint
4 endpoints for MCP server/tool discovery and registration.
Wires to frontend mcpApi in socialApi.js.
"""
import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth, optional_auth
from .models import get_db, MCPServer, MCPTool

logger = logging.getLogger('hevolve_social')

mcp_bp = Blueprint('mcp', __name__, url_prefix='/api/social')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _paginate(total, limit, offset):
    return {'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total}


def _get_json():
    return request.get_json(force=True, silent=True) or {}


# ═══════════════════════════════════════════════════════════════
# MCP TOOL REGISTRY (4 endpoints)
# ═══════════════════════════════════════════════════════════════

@mcp_bp.route('/mcp/servers', methods=['GET'])
@optional_auth
def list_mcp_servers():
    """List registered MCP tool servers."""
    db = get_db()
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        offset = int(request.args.get('offset', 0))
        category = request.args.get('category')
        q = request.args.get('q', '').strip()

        query = db.query(MCPServer).filter_by(is_active=True)
        if category:
            query = query.filter_by(category=category)
        if q:
            query = query.filter(
                MCPServer.name.ilike(f'%{q}%') |
                MCPServer.description.ilike(f'%{q}%')
            )
        total = query.count()
        servers = query.order_by(MCPServer.created_at.desc()).offset(offset).limit(limit).all()
        return _ok([s.to_dict() for s in servers], _paginate(total, limit, offset))
    except Exception as e:
        logger.error(f"mcp/servers GET error: {e}")
        return _err(str(e), 500)
    finally:
        db.close()


@mcp_bp.route('/mcp/servers/<server_id>/tools', methods=['GET'])
@optional_auth
def list_mcp_tools(server_id):
    """List tools for a specific MCP server."""
    db = get_db()
    try:
        server = db.query(MCPServer).filter_by(id=server_id, is_active=True).first()
        if not server:
            return _err('Server not found', 404)
        tools = db.query(MCPTool).filter_by(server_id=server_id).all()
        return _ok({
            'server': server.to_dict(),
            'tools': [t.to_dict() for t in tools],
        })
    except Exception as e:
        logger.error(f"mcp/servers/{server_id}/tools error: {e}")
        return _err(str(e), 500)
    finally:
        db.close()


@mcp_bp.route('/mcp/register', methods=['POST'])
@require_auth
def register_mcp_server():
    """Register a new MCP tool server with its tools."""
    db = get_db()
    try:
        data = _get_json()
        name = data.get('name', '').strip()
        if not name:
            return _err('name required')

        server = MCPServer(
            owner_id=g.user_id,
            name=name,
            description=data.get('description', ''),
            url=data.get('url', ''),
            category=data.get('category', 'general'),
        )
        db.add(server)
        db.flush()  # get server.id

        # Register tools if provided
        tools_data = data.get('tools', [])
        for td in tools_data[:50]:  # cap at 50 tools per server
            tool = MCPTool(
                server_id=server.id,
                name=td.get('name', 'unnamed'),
                description=td.get('description', ''),
                input_schema=td.get('input_schema', {}),
            )
            db.add(tool)

        db.commit()
        db.refresh(server)
        return _ok(server.to_dict(), status=201)
    except Exception as e:
        db.rollback()
        logger.error(f"mcp/register POST error: {e}")
        return _err(str(e), 500)
    finally:
        db.close()


@mcp_bp.route('/mcp/discover', methods=['GET'])
@optional_auth
def discover_mcp():
    """Discover MCP servers and tools by search query."""
    db = get_db()
    try:
        q = request.args.get('q', '').strip()
        limit = min(int(request.args.get('limit', 20)), 50)

        if not q:
            return _err('q parameter required')

        # Search servers
        servers = db.query(MCPServer).filter(
            MCPServer.is_active == True,
            MCPServer.name.ilike(f'%{q}%') |
            MCPServer.description.ilike(f'%{q}%')
        ).limit(limit).all()

        # Search tools
        tools = db.query(MCPTool).filter(
            MCPTool.name.ilike(f'%{q}%') |
            MCPTool.description.ilike(f'%{q}%')
        ).limit(limit).all()

        return _ok({
            'servers': [s.to_dict() for s in servers],
            'tools': [t.to_dict() for t in tools],
        })
    except Exception as e:
        logger.error(f"mcp/discover GET error: {e}")
        return _err(str(e), 500)
    finally:
        db.close()
