"""
Universal Provider Gateway — route any AI task to the optimal provider.

Nunba becomes the everything app: agents call the gateway, gateway routes
to the cheapest/fastest provider, earns commission where available.

Architecture:
  ProviderRegistry  — catalog of all providers + their capabilities/pricing
  ProviderGateway   — smart router: pick provider, call API, track cost
  EfficiencyMatrix  — continuous benchmarking: tok/s, latency, quality per provider

Providers fall into two categories:
  1. Raw API providers (direct integration, we pay API cost):
     Replicate, Together AI, Fireworks AI, fal.ai, DeepInfra, Groq,
     Cerebras, SambaNova, RunPod, Modal, HuggingFace Inference, OpenRouter

  2. Business services (affiliate/commission, user pays them):
     Seedance, RunwayML, Pika, Kling, Luma, ElevenLabs, Midjourney, etc.

For (1): agents use the model directly via gateway. Nunba pays provider,
         charges user credits. Margin = credit price - API cost.
For (2): agents recommend/redirect. Revenue = affiliate commission.

Both types register as agent tools automatically.
"""

from integrations.providers.registry import ProviderRegistry, get_registry
from integrations.providers.gateway import ProviderGateway, get_gateway

__all__ = ['ProviderRegistry', 'get_registry', 'ProviderGateway', 'get_gateway']
