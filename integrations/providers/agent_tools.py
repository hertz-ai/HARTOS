"""
Agent tools for the provider gateway.

Registers LangChain tools that let agents use any cloud provider:
  - generate_text:  LLM generation via optimal provider
  - generate_image: Image generation via optimal provider
  - generate_video: Video generation via optimal provider
  - generate_audio: Audio/music generation via optimal provider
  - list_providers: Show available providers and their capabilities
  - provider_stats: Show efficiency matrix / leaderboard

Also registers as autogen tools for the agent engine.
"""

import json
import logging

logger = logging.getLogger(__name__)


def get_provider_tools():
    """Return LangChain-compatible tool definitions for the provider gateway.

    Called by langchain_gpt_api.py get_tools() to inject into agent tool list.
    """
    tools = []

    try:
        from langchain.tools import Tool
    except ImportError:
        try:
            from langchain_core.tools import Tool
        except ImportError:
            logger.debug("LangChain not available — skipping provider tools")
            return []

    def _generate_text(query: str) -> str:
        """Generate text using the best available cloud LLM provider.
        Automatically selects the cheapest and fastest provider.
        Input: your prompt text.
        """
        try:
            from integrations.providers.gateway import get_gateway
            result = get_gateway().generate(query, model_type='llm', strategy='balanced')
            if result.success:
                return (f"{result.content}\n\n"
                        f"[Provider: {result.provider_id}, "
                        f"Cost: ${result.cost_usd:.6f}, "
                        f"Speed: {result.tok_per_s:.0f} tok/s]")
            return f"Error: {result.error}"
        except Exception as e:
            return f"Provider gateway error: {e}"

    def _generate_image(query: str) -> str:
        """Generate an image using the best available provider (Replicate, fal.ai, etc.).
        Input: image description/prompt.
        Returns: URL of the generated image.
        """
        try:
            from integrations.providers.gateway import get_gateway
            result = get_gateway().generate(query, model_type='image_gen', strategy='balanced')
            if result.success:
                return (f"Image URL: {result.content}\n"
                        f"[Provider: {result.provider_id}, Cost: ${result.cost_usd:.4f}]")
            return f"Error: {result.error}"
        except Exception as e:
            return f"Provider gateway error: {e}"

    def _generate_video(query: str) -> str:
        """Generate a video using the best available provider.
        Input: video description/prompt.
        Returns: URL of the generated video.
        """
        try:
            from integrations.providers.gateway import get_gateway
            result = get_gateway().generate(query, model_type='video_gen', strategy='balanced')
            if result.success:
                return (f"Video URL: {result.content}\n"
                        f"[Provider: {result.provider_id}, Cost: ${result.cost_usd:.4f}]")
            return f"Error: {result.error}"
        except Exception as e:
            return f"Provider gateway error: {e}"

    def _list_providers(query: str) -> str:
        """List all available AI providers and their capabilities.
        Shows which providers are configured, their pricing, and what they support.
        Input: optional filter like 'llm', 'image', 'video', or 'all'.
        """
        try:
            from integrations.providers.registry import get_registry
            reg = get_registry()
            category = query.strip().lower() if query.strip() else ''

            providers = (reg.list_by_category(category) if category and category != 'all'
                         else reg.list_enabled())

            lines = [f"=== Available Providers ({len(providers)}) ===\n"]
            for p in providers:
                key_status = "API key configured" if p.has_api_key() else "No API key"
                model_count = len(p.models)
                cats = ', '.join(p.categories)
                lines.append(
                    f"• {p.name} ({p.id}) — {p.provider_type}\n"
                    f"  Categories: {cats}\n"
                    f"  Models: {model_count} | Status: {key_status}\n"
                    f"  {'Commission: ' + str(p.commission_pct) + '%' if p.commission_pct > 0 else ''}"
                )
            return '\n'.join(lines)
        except Exception as e:
            return f"Error listing providers: {e}"

    def _provider_leaderboard(query: str) -> str:
        """Show the efficiency leaderboard — which provider/model is best.
        Ranks by speed, quality, cost, and overall efficiency.
        Input: model type like 'llm', 'image_gen', etc. Default: 'llm'.
        """
        try:
            from integrations.providers.efficiency_matrix import get_matrix
            model_type = query.strip() or 'llm'
            entries = get_matrix().get_leaderboard(model_type)

            if not entries:
                return f"No benchmark data yet for {model_type}. Data accumulates as providers are used."

            lines = [f"=== Efficiency Leaderboard ({model_type}) ===\n"]
            for i, bm in enumerate(entries[:10], 1):
                lines.append(
                    f"{i}. {bm.provider_id}/{bm.model_id}\n"
                    f"   Efficiency: {bm.efficiency_score:.3f} | "
                    f"Speed: {bm.avg_tok_per_s:.0f} tok/s | "
                    f"Quality: {bm.quality_score:.2f} | "
                    f"Reliability: {bm.success_rate:.0%} | "
                    f"Cost/1k: ${bm.cost_per_1k_output_tokens:.4f}"
                )
            return '\n'.join(lines)
        except Exception as e:
            return f"Error: {e}"

    tools.extend([
        Tool(
            name='Cloud_LLM',
            func=_generate_text,
            description=(
                'Generate text using cloud LLM providers (Together AI, Groq, Fireworks, etc.). '
                'Automatically picks the fastest and cheapest provider. '
                'Use this for tasks requiring powerful cloud models.'
            ),
        ),
        Tool(
            name='Generate_Image',
            func=_generate_image,
            description=(
                'Generate an image from a text prompt using cloud providers '
                '(Replicate, fal.ai, etc.). Returns the image URL.'
            ),
        ),
        Tool(
            name='Generate_Video',
            func=_generate_video,
            description=(
                'Generate a video from a text prompt using cloud providers. '
                'Returns the video URL.'
            ),
        ),
        Tool(
            name='List_AI_Providers',
            func=_list_providers,
            description=(
                'List all available AI providers, their capabilities, pricing, '
                'and configuration status. Input: filter by category (llm, image, video) or "all".'
            ),
        ),
        Tool(
            name='Provider_Leaderboard',
            func=_provider_leaderboard,
            description=(
                'Show the efficiency leaderboard ranking providers by speed, quality, '
                'cost, and overall efficiency. Input: model type (llm, image_gen, etc.).'
            ),
        ),
    ])

    return tools
