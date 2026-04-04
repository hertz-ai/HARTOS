"""
ProviderGateway — smart router that agents call for any AI task.

Usage:
    from integrations.providers import get_gateway
    gw = get_gateway()

    # Text generation
    result = gw.generate('Tell me a joke', model_type='llm')

    # Image generation
    result = gw.generate('A cat in space', model_type='image_gen')

    # Specific model on specific provider
    result = gw.generate('Hello', provider_id='groq', model_id='llama-3.3-70b-versatile')

The gateway:
  1. Picks the best provider (cheapest/fastest/balanced) from the registry
  2. Calls the provider's API (OpenAI-compatible, Replicate, or custom)
  3. Tracks cost, latency, tok/s — feeds back into registry stats
  4. Falls back to next-best provider on failure
  5. Falls back to local model as last resort
"""

import json
import logging
import os
import time
import threading
from typing import Any, Dict, List, Optional, Generator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class GatewayResult:
    """Result from a gateway call."""
    success: bool
    content: str = ''               # Text response or URL for media
    provider_id: str = ''
    model_id: str = ''
    usage: Dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0          # Estimated cost in USD
    latency_ms: float = 0.0
    tok_per_s: float = 0.0
    model_type: str = 'llm'        # Request type for revenue tracking
    error: str = ''
    raw_response: Any = None        # Full API response for advanced use


class ProviderGateway:
    """Smart router for all AI API calls.

    Agents call gateway methods. The gateway picks the optimal provider
    from the registry, calls the API, tracks stats, handles failures.
    """

    def __init__(self):
        from integrations.providers.registry import get_registry
        self._registry = get_registry()
        self._usage_lock = threading.Lock()
        self._total_cost_usd = 0.0
        self._total_requests = 0
        self._request_log: List[Dict] = []  # Last 100 requests for dashboard

    # ═══════════════════════════════════════════════════════════════════
    # Public API — what agents call
    # ═══════════════════════════════════════════════════════════════════

    def generate(self, prompt: str, model_type: str = 'llm',
                 provider_id: str = '', model_id: str = '',
                 strategy: str = 'balanced',
                 system_prompt: str = '',
                 max_tokens: int = 4096,
                 temperature: float = 0.7,
                 stream: bool = False,
                 **kwargs) -> GatewayResult:
        """Generate content via the optimal provider.

        Args:
            prompt: User prompt or generation instruction
            model_type: 'llm', 'image_gen', 'video_gen', 'tts', etc.
            provider_id: Force a specific provider (optional)
            model_id: Force a specific model (optional)
            strategy: 'cheapest', 'fastest', 'quality', 'balanced'
            system_prompt: System message for LLMs
            max_tokens: Max output tokens for LLMs
            temperature: Sampling temperature
            stream: Whether to stream (LLM only)
            **kwargs: Provider-specific params (image size, video duration, etc.)
        """
        t0 = time.time()

        # Resolve provider + model
        provider, provider_model = self._resolve(
            model_type, provider_id, model_id, strategy)

        if not provider:
            return GatewayResult(
                success=False,
                error=f'No provider available for {model_type}'
                      f'{" model=" + model_id if model_id else ""}'
                      f' (strategy={strategy}). Configure API keys in Settings.',
            )

        # Try the primary provider, then fallbacks
        providers_tried = []
        result = self._call_provider(
            provider, provider_model, prompt, model_type,
            system_prompt=system_prompt, max_tokens=max_tokens,
            temperature=temperature, stream=stream, **kwargs,
        )
        providers_tried.append(provider.id)

        # On failure, try fallbacks (up to 2 more providers)
        if not result.success:
            for _ in range(2):
                fb_provider, fb_model = self._resolve(
                    model_type, '', '', strategy,
                    exclude=providers_tried,
                )
                if not fb_provider:
                    break
                result = self._call_provider(
                    fb_provider, fb_model, prompt, model_type,
                    system_prompt=system_prompt, max_tokens=max_tokens,
                    temperature=temperature, stream=stream, **kwargs,
                )
                providers_tried.append(fb_provider.id)
                if result.success:
                    break

        # Track stats
        elapsed_ms = (time.time() - t0) * 1000
        result.latency_ms = elapsed_ms
        result.model_type = model_type
        self._track(result)

        return result

    def generate_stream(self, prompt: str, model_type: str = 'llm',
                        **kwargs) -> Generator[str, None, None]:
        """Stream text generation. Yields chunks."""
        kwargs['stream'] = True
        # For streaming, we need to handle it differently
        provider_id = kwargs.pop('provider_id', '')
        model_id = kwargs.pop('model_id', '')
        strategy = kwargs.pop('strategy', 'balanced')
        system_prompt = kwargs.pop('system_prompt', '')
        max_tokens = kwargs.pop('max_tokens', 4096)
        temperature = kwargs.pop('temperature', 0.7)

        provider, provider_model = self._resolve(
            model_type, provider_id, model_id, strategy)

        if not provider:
            yield '[Error: No provider available]'
            return

        yield from self._stream_openai(
            provider, provider_model, prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Return gateway usage stats for dashboards."""
        with self._usage_lock:
            return {
                'total_cost_usd': round(self._total_cost_usd, 6),
                'total_requests': self._total_requests,
                'recent_requests': list(self._request_log[-20:]),
                'capabilities': self._registry.get_capabilities_summary(),
            }

    # ═══════════════════════════════════════════════════════════════════
    # Resolution — pick the best provider
    # ═══════════════════════════════════════════════════════════════════

    def _resolve(self, model_type, provider_id, model_id, strategy,
                 exclude=None):
        """Resolve the best (Provider, ProviderModel) for the request."""
        from integrations.providers.registry import (
            Provider, ProviderModel, PROVIDER_TYPE_LOCAL)

        exclude = exclude or []

        # Specific provider requested
        if provider_id:
            p = self._registry.get(provider_id)
            if p and p.id not in exclude:
                if model_id and model_id in p.models:
                    return p, p.models[model_id]
                # Find first matching model type
                for pm in p.models.values():
                    if pm.model_type == model_type and pm.enabled:
                        return p, pm
            return None, None

        # Auto-select from registry
        result = self._registry.find_best(model_type, strategy=strategy)
        if result:
            p, pm = result
            if p.id not in exclude:
                return p, pm

        # Try all candidates excluding already-tried
        for p in self._registry.list_api_providers():
            if p.id in exclude or not p.has_api_key():
                continue
            for pm in p.models.values():
                if pm.model_type == model_type and pm.enabled:
                    return p, pm

        # Last resort: local provider
        local = self._registry.get('local')
        if local and local.id not in exclude:
            return local, ProviderModel(
                model_id='local', canonical_id='local',
                model_type=model_type,
            )

        return None, None

    # ═══════════════════════════════════════════════════════════════════
    # Provider Callers — format-specific API calls
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _build_headers(provider) -> dict:
        """Build HTTP headers with correct auth for a provider (DRY)."""
        headers = {'Content-Type': 'application/json'}
        api_key = provider.get_api_key()
        if api_key:
            if provider.id == 'fal':
                headers['Authorization'] = f'Key {api_key}'
            elif provider.auth_method == 'header':
                headers[provider.auth_header] = f'Bearer {api_key}'
            else:  # bearer (default)
                headers['Authorization'] = f'Bearer {api_key}'
        return headers

    def _call_provider(self, provider, provider_model, prompt, model_type,
                       **kwargs) -> GatewayResult:
        """Dispatch to the correct API format handler."""
        from integrations.providers.registry import PROVIDER_TYPE_LOCAL

        try:
            if provider.provider_type == PROVIDER_TYPE_LOCAL:
                return self._call_local(prompt, model_type, **kwargs)
            elif provider.api_format == 'openai':
                return self._call_openai(provider, provider_model, prompt,
                                         model_type, **kwargs)
            elif provider.api_format == 'replicate':
                return self._call_replicate(provider, provider_model, prompt,
                                            model_type, **kwargs)
            else:
                return self._call_custom(provider, provider_model, prompt,
                                         model_type, **kwargs)
        except Exception as e:
            logger.error("Provider %s call failed: %s", provider.id, e)
            self._registry.update_model_stats(
                provider.id, provider_model.model_id, success=False)
            return GatewayResult(
                success=False, error=str(e),
                provider_id=provider.id, model_id=provider_model.model_id,
            )

    def _call_openai(self, provider, provider_model, prompt, model_type,
                     system_prompt='', max_tokens=4096, temperature=0.7,
                     stream=False, **kwargs) -> GatewayResult:
        """Call an OpenAI-compatible API (Together, Fireworks, Groq, etc.)."""
        import urllib.request
        import urllib.error

        url = f"{provider.base_url.rstrip('/')}/chat/completions"

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})

        body = {
            'model': provider_model.model_id,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'stream': False,
        }

        headers = self._build_headers(provider)

        t0 = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ''
            logger.error("OpenAI API error %d from %s: %s",
                         e.code, provider.id, error_body[:500])
            return GatewayResult(
                success=False,
                error=f'HTTP {e.code}: {error_body[:200]}',
                provider_id=provider.id,
                model_id=provider_model.model_id,
            )

        elapsed_ms = (time.time() - t0) * 1000

        # Parse response
        content = ''
        usage = data.get('usage', {})
        if 'choices' in data and data['choices']:
            content = data['choices'][0].get('message', {}).get('content', '')

        # Calculate cost
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
        total_tokens = input_tokens + output_tokens
        cost = self._calculate_cost(provider_model, input_tokens, output_tokens)
        tok_per_s = (output_tokens / (elapsed_ms / 1000)) if elapsed_ms > 0 and output_tokens > 0 else 0

        # Update provider stats
        self._registry.update_model_stats(
            provider.id, provider_model.model_id,
            tok_per_s=tok_per_s, latency_ms=elapsed_ms, success=True,
        )

        return GatewayResult(
            success=True,
            content=content,
            provider_id=provider.id,
            model_id=provider_model.model_id,
            usage={'input_tokens': input_tokens, 'output_tokens': output_tokens,
                   'total_tokens': total_tokens},
            cost_usd=cost,
            latency_ms=elapsed_ms,
            tok_per_s=tok_per_s,
            raw_response=data,
        )

    def _stream_openai(self, provider, provider_model, prompt,
                       system_prompt='', max_tokens=4096, temperature=0.7,
                       **kwargs) -> Generator[str, None, None]:
        """Stream from OpenAI-compatible API."""
        import urllib.request

        url = f"{provider.base_url.rstrip('/')}/chat/completions"

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})

        body = {
            'model': provider_model.model_id,
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'stream': True,
        }

        headers = self._build_headers(provider)

        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers, method='POST',
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            for line in resp:
                line = line.decode('utf-8').strip()
                if line.startswith('data: ') and line != 'data: [DONE]':
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                        text = delta.get('content', '')
                        if text:
                            yield text
                    except json.JSONDecodeError:
                        continue
            resp.close()
        except Exception as e:
            yield f'\n[Stream error: {e}]'

    def _call_replicate(self, provider, provider_model, prompt, model_type,
                        **kwargs) -> GatewayResult:
        """Call Replicate's prediction API."""
        import urllib.request
        import urllib.error

        api_key = provider.get_api_key()
        url = f"{provider.base_url.rstrip('/')}/predictions"

        # Replicate uses a different input format per model
        input_data = {'prompt': prompt}
        if model_type == 'image_gen':
            input_data.update({
                'width': kwargs.get('width', 1024),
                'height': kwargs.get('height', 1024),
                'num_outputs': kwargs.get('num_outputs', 1),
            })
        elif model_type == 'video_gen':
            input_data['duration'] = kwargs.get('duration', 5)

        body = {
            'version': provider_model.model_id,
            'input': input_data,
        }

        headers = self._build_headers(provider)
        headers['Prefer'] = 'wait'  # Synchronous mode

        t0 = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return GatewayResult(
                success=False, error=f'Replicate HTTP {e.code}',
                provider_id=provider.id, model_id=provider_model.model_id,
            )

        elapsed_ms = (time.time() - t0) * 1000
        output = data.get('output', '')
        if isinstance(output, list):
            output = output[0] if output else ''

        return GatewayResult(
            success=True, content=str(output),
            provider_id=provider.id, model_id=provider_model.model_id,
            latency_ms=elapsed_ms, raw_response=data,
        )

    def _call_custom(self, provider, provider_model, prompt, model_type,
                     **kwargs) -> GatewayResult:
        """Call custom API format (fal.ai, HuggingFace, etc.)."""
        import urllib.request
        import urllib.error

        api_key = provider.get_api_key()

        if provider.id == 'fal':
            return self._call_fal(provider, provider_model, prompt,
                                  model_type, api_key, **kwargs)

        # Generic: POST JSON to base_url/model_id
        url = f"{provider.base_url.rstrip('/')}/{provider_model.model_id}"
        body = {'inputs': prompt}

        headers = self._build_headers(provider)

        t0 = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return GatewayResult(
                success=False, error=str(e),
                provider_id=provider.id, model_id=provider_model.model_id,
            )

        elapsed_ms = (time.time() - t0) * 1000
        content = data if isinstance(data, str) else json.dumps(data)

        return GatewayResult(
            success=True, content=content,
            provider_id=provider.id, model_id=provider_model.model_id,
            latency_ms=elapsed_ms, raw_response=data,
        )

    def _call_fal(self, provider, provider_model, prompt, model_type,
                  api_key, **kwargs) -> GatewayResult:
        """Call fal.ai serverless API."""
        import urllib.request

        url = f"https://fal.run/{provider_model.model_id}"
        body = {'prompt': prompt}
        if model_type == 'image_gen':
            body.update({
                'image_size': kwargs.get('image_size', 'landscape_16_9'),
                'num_images': kwargs.get('num_images', 1),
            })

        headers = self._build_headers(provider)

        t0 = time.time()
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers=headers, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return GatewayResult(
                success=False, error=str(e),
                provider_id=provider.id, model_id=provider_model.model_id,
            )

        elapsed_ms = (time.time() - t0) * 1000
        # fal.ai returns images/videos in 'images' or 'video' fields
        content = ''
        if 'images' in data:
            content = data['images'][0].get('url', '') if data['images'] else ''
        elif 'video' in data:
            content = data['video'].get('url', '')
        elif 'audio' in data:
            content = data['audio'].get('url', '')
        else:
            content = json.dumps(data)

        return GatewayResult(
            success=True, content=content,
            provider_id=provider.id, model_id=provider_model.model_id,
            latency_ms=elapsed_ms, raw_response=data,
        )

    def _call_local(self, prompt, model_type, **kwargs) -> GatewayResult:
        """Route to local model via existing HARTOS infrastructure."""
        try:
            if model_type == 'llm':
                # Use existing LangChain / llama.cpp path
                import urllib.request
                url = os.environ.get('HEVOLVE_LOCAL_LLM_URL',
                                     'http://localhost:8080/v1')
                body = {
                    'model': 'local',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': kwargs.get('max_tokens', 4096),
                    'temperature': kwargs.get('temperature', 0.7),
                    'stream': False,
                }
                if kwargs.get('system_prompt'):
                    body['messages'].insert(0, {
                        'role': 'system',
                        'content': kwargs['system_prompt'],
                    })
                req = urllib.request.Request(
                    f"{url.rstrip('/')}/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode())
                elapsed_ms = (time.time() - t0) * 1000
                content = data.get('choices', [{}])[0].get(
                    'message', {}).get('content', '')
                return GatewayResult(
                    success=True, content=content,
                    provider_id='local', model_id='local-llm',
                    latency_ms=elapsed_ms, cost_usd=0.0,
                )
            else:
                return GatewayResult(
                    success=False,
                    error=f'Local {model_type} not yet implemented via gateway',
                    provider_id='local',
                )
        except Exception as e:
            return GatewayResult(
                success=False, error=f'Local call failed: {e}',
                provider_id='local',
            )

    # ═══════════════════════════════════════════════════════════════════
    # Cost calculation
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _calculate_cost(provider_model, input_tokens, output_tokens):
        from integrations.providers.registry import (
            PRICE_PER_1M_TOKENS, PRICE_PER_1K_TOKENS, PRICE_PER_IMAGE,
            PRICE_PER_SECOND, PRICE_PER_REQUEST, PRICE_FREE,
        )
        unit = provider_model.pricing_unit
        if unit == PRICE_FREE:
            return 0.0
        if unit == PRICE_PER_1M_TOKENS:
            return (input_tokens * provider_model.input_price / 1_000_000 +
                    output_tokens * provider_model.output_price / 1_000_000)
        if unit == PRICE_PER_1K_TOKENS:
            return (input_tokens * provider_model.input_price / 1_000 +
                    output_tokens * provider_model.output_price / 1_000)
        if unit in (PRICE_PER_IMAGE, PRICE_PER_REQUEST):
            return provider_model.input_price
        if unit == PRICE_PER_SECOND:
            return provider_model.input_price  # Per-second, duration-dependent
        return 0.0

    # ═══════════════════════════════════════════════════════════════════
    # Tracking
    # ═══════════════════════════════════════════════════════════════════

    def _track(self, result: GatewayResult):
        with self._usage_lock:
            self._total_requests += 1
            self._total_cost_usd += result.cost_usd
            self._request_log.append({
                'ts': time.time(),
                'provider': result.provider_id,
                'model': result.model_id,
                'success': result.success,
                'cost': result.cost_usd,
                'latency_ms': result.latency_ms,
                'tok_per_s': result.tok_per_s,
            })
            # Keep last 100
            if len(self._request_log) > 100:
                self._request_log = self._request_log[-100:]

        # Feed into efficiency matrix (continuous learning)
        try:
            from integrations.providers.efficiency_matrix import get_matrix
            get_matrix().record_request(
                provider_id=result.provider_id,
                model_id=result.model_id,
                tok_per_s=result.tok_per_s,
                e2e_ms=result.latency_ms,
                cost_usd=result.cost_usd,
                output_tokens=result.usage.get('output_tokens', 0),
                success=result.success,
            )
        except Exception:
            pass

        # Feed into revenue tracker (cost side — revenue recorded by affiliate layer)
        if result.cost_usd > 0:
            try:
                from integrations.providers.revenue_tracker import get_tracker
                get_tracker().record_cost(
                    provider_id=result.provider_id,
                    model_id=result.model_id,
                    cost_usd=result.cost_usd,
                    tokens_used=result.usage.get('total_tokens', 0),
                    request_type=result.model_type,
                )
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_gateway: Optional[ProviderGateway] = None
_gateway_lock = threading.Lock()


def get_gateway() -> ProviderGateway:
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = ProviderGateway()
    return _gateway
