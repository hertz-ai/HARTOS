"""
HevolveSocial - Search Integration
Bridges to existing memory/embeddings modules for semantic search on posts.
"""
import logging
from typing import List, Optional

logger = logging.getLogger('hevolve_social')

_embedding_cache = None
_has_embeddings = False

try:
    from integrations.channels.memory.embeddings import EmbeddingCache
    _has_embeddings = True
except ImportError:
    pass


def get_embedding_cache():
    global _embedding_cache
    if _embedding_cache is None and _has_embeddings:
        try:
            _embedding_cache = EmbeddingCache()
        except Exception as e:
            logger.debug(f"EmbeddingCache init failed: {e}")
    return _embedding_cache


def compute_post_embedding(content: str) -> Optional[str]:
    """Compute and cache embedding for a post's content. Returns embedding_id."""
    cache = get_embedding_cache()
    if cache is None:
        return None
    try:
        embedding = cache.get_embedding(content)
        if embedding is not None:
            return cache.store(content, embedding)
    except Exception as e:
        logger.debug(f"Embedding computation failed: {e}")
    return None


def semantic_search_posts(query: str, limit: int = 20) -> List[str]:
    """Search posts using semantic similarity. Returns list of post IDs."""
    cache = get_embedding_cache()
    if cache is None:
        return []
    try:
        results = cache.search(query, top_k=limit)
        return [r.id for r in results] if results else []
    except Exception as e:
        logger.debug(f"Semantic search failed: {e}")
        return []
