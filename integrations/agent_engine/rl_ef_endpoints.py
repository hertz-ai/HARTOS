"""
RL-EF API Endpoints

Flask endpoints for expert corrections and learning statistics.
Add these to your existing Flask application.

Usage:
    from integrations.agent_engine.rl_ef_endpoints import rl_ef_blueprint
    app.register_blueprint(rl_ef_blueprint)
"""

from flask import Blueprint, request, jsonify
from typing import Dict, Any
import sys
from pathlib import Path

# Add HevolveAI to path
_hevolve_core_path = Path("C:/Users/sathi/PycharmProjects/hevolveai")
if not _hevolve_core_path.exists():
    _hevolve_core_path = Path("C:/Users/sathi/PycharmProjects/hevolveai")  # legacy fallback
if _hevolve_core_path.exists():
    sys.path.insert(0, str(_hevolve_core_path))

from embodied_ai.rl_ef import (
    send_expert_correction,
    get_learning_provider
)

# Create blueprint
rl_ef_blueprint = Blueprint('rl_ef', __name__, url_prefix='/api/rl_ef')


@rl_ef_blueprint.route('/correction', methods=['POST'])
def submit_correction():
    """
    Submit expert correction and trigger immediate learning.

    Request body:
    {
        "domain": "medical",
        "original_response": "Patient has flu",
        "corrected_response": "Patient has COVID-19 confirmed by PCR",
        "expert_id": "dr_smith_001",
        "confidence": 0.95,
        "explanation": "PCR test positive, CT scan shows COVID pneumonia"
    }

    Response:
    {
        "status": "success",
        "message": "Correction learned! Total: 5",
        "correction_id": 3,
        "expert_id": "dr_smith_001",
        "confidence": 0.95,
        "total_corrections": 5
    }

    Example usage:
        curl -X POST http://localhost:5000/api/rl_ef/correction \
             -H "Content-Type: application/json" \
             -d '{
                "domain": "medical",
                "original_response": "Patient has flu",
                "corrected_response": "Patient has COVID-19",
                "expert_id": "dr_smith",
                "confidence": 0.95
             }'
    """
    try:
        data = request.json

        # Validate required fields
        required = ['domain', 'original_response', 'corrected_response', 'expert_id']
        missing = [f for f in required if f not in data]

        if missing:
            return jsonify({
                'status': 'error',
                'message': f'Missing required fields: {", ".join(missing)}'
            }), 400

        # Send correction to learning provider
        result = send_expert_correction(
            domain=data['domain'],
            original_response=data['original_response'],
            corrected_response=data['corrected_response'],
            expert_id=data['expert_id'],
            confidence=data.get('confidence', 1.0),
            explanation=data.get('explanation')
        )

        if result['success']:
            return jsonify({
                'status': 'success',
                'message': f"✅ Correction learned! Total: {result['total_corrections']}",
                'correction_id': result['correction_id'],
                'expert_id': result['expert_id'],
                'confidence': result['confidence'],
                'total_corrections': result['total_corrections']
            }), 200
        else:
            return jsonify({
                'status': 'error',
                'message': result['error']
            }), 500

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@rl_ef_blueprint.route('/stats/<domain>', methods=['GET'])
def get_stats(domain: str):
    """
    Get learning statistics for a domain.

    Response:
    {
        "domain": "medical",
        "conversations": 42,
        "expert_corrections": 5,
        "unique_experts": 2,
        "total_experiences": 156,
        "expert_breakdown": {
            "dr_smith_001": {
                "correction_count": 3,
                "avg_confidence": 0.95
            },
            "dr_jones_002": {
                "correction_count": 2,
                "avg_confidence": 0.90
            }
        }
    }

    Example usage:
        curl http://localhost:5000/api/rl_ef/stats/medical
    """
    try:
        provider = get_learning_provider(domain)

        if provider is None:
            return jsonify({
                'status': 'error',
                'message': f'No learning provider registered for domain: {domain}'
            }), 404

        stats = provider.get_stats()

        return jsonify({
            'status': 'success',
            'domain': stats['domain'],
            'conversations': stats['conversations'],
            'expert_corrections': stats['expert_feedback']['total_corrections'],
            'unique_experts': stats['expert_feedback']['unique_experts'],
            'total_experiences': stats['embodied_ai']['total_experiences'],
            'expert_breakdown': stats['expert_feedback']['experts']
        }), 200

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@rl_ef_blueprint.route('/domains', methods=['GET'])
def list_domains():
    """
    List all domains with active learning providers.

    Response:
    {
        "domains": ["medical", "trading", "coding"],
        "count": 3
    }

    Example usage:
        curl http://localhost:5000/api/rl_ef/domains
    """
    try:
        from embodied_ai.rl_ef.learning_llm_provider import _LEARNING_PROVIDERS

        domains = list(_LEARNING_PROVIDERS.keys())

        return jsonify({
            'status': 'success',
            'domains': domains,
            'count': len(domains)
        }), 200

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@rl_ef_blueprint.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint.

    Response:
    {
        "status": "healthy",
        "version": "0.1.0"
    }
    """
    return jsonify({
        'status': 'healthy',
        'service': 'RL-EF Learning System',
        'version': '0.1.0'
    }), 200


# Example: Add to your Flask app
"""
# In your main Flask app file (e.g., app.py):

from flask import Flask
from integrations.agent_engine.rl_ef_endpoints import rl_ef_blueprint

app = Flask(__name__)

# Register RL-EF endpoints
app.register_blueprint(rl_ef_blueprint)

# Your existing endpoints...
@app.route('/')
def index():
    return "Hello World"

if __name__ == '__main__':
    app.run(debug=True)
"""


# Example: Test the endpoints
if __name__ == "__main__":
    print("=" * 80)
    print("RL-EF API Endpoints")
    print("=" * 80)
    print()
    print("Available endpoints:")
    print()
    print("1. POST /api/rl_ef/correction")
    print("   Submit expert correction and trigger learning")
    print()
    print("2. GET /api/rl_ef/stats/<domain>")
    print("   Get learning statistics for a domain")
    print()
    print("3. GET /api/rl_ef/domains")
    print("   List all active domains")
    print()
    print("4. GET /api/rl_ef/health")
    print("   Health check")
    print()
    print("=" * 80)
    print()
    print("Integration:")
    print()
    print("  from integrations.agent_engine.rl_ef_endpoints import rl_ef_blueprint")
    print("  app.register_blueprint(rl_ef_blueprint)")
    print()
    print("Then start your Flask app and test:")
    print()
    print("  curl http://localhost:5000/api/rl_ef/health")
    print()
