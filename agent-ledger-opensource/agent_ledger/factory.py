"""
Production-Ready Ledger Factory

Automatically creates SmartLedger instances with the fastest available backend:
1. Tries Redis first (10-50x faster than JSON)
2. Falls back to MongoDB if Redis unavailable
3. Falls back to JSON if no databases available

Usage:
    from agent_ledger.factory import create_production_ledger

    ledger = create_production_ledger(agent_id="my_agent", session_id="session_1")
"""

from typing import Optional
import logging
from .core import SmartLedger
from .backends import JSONBackend, RedisBackend, MongoDBBackend

logger = logging.getLogger(__name__)


def create_production_ledger(
    agent_id: str,
    session_id: str,
    redis_config: Optional[dict] = None,
    mongo_config: Optional[dict] = None,
    storage_dir: str = ".agent_ledger",
    prefer_redis: bool = True
) -> SmartLedger:
    """
    Create a SmartLedger with the best available backend.

    Priority order:
    1. Redis (if prefer_redis=True and Redis available)
    2. MongoDB (if mongo_config provided and MongoDB available)
    3. JSON (fallback)

    Args:
        agent_id: Unique identifier for the agent
        session_id: Unique identifier for this session
        redis_config: Optional dict with Redis connection params
                     Example: {"host": "localhost", "port": 6379, "password": "secret"}
        mongo_config: Optional dict with MongoDB connection params
                     Example: {"host": "localhost", "port": 27017, "database": "agent_ledger"}
        storage_dir: Directory for JSON fallback storage
        prefer_redis: If True, try Redis before MongoDB

    Returns:
        SmartLedger instance with fastest available backend
    """
    backend = None
    backend_name = "JSON"

    # Try Redis first (fastest option)
    if prefer_redis:
        try:
            redis_params = redis_config or {"host": "localhost", "port": 6379}
            backend = RedisBackend(**redis_params)
            backend_name = "Redis"
            logger.info(f"Using Redis backend (production mode) for {agent_id}:{session_id}")
        except ImportError:
            logger.warning("Redis package not installed. Install with: pip install redis")
        except Exception as e:
            logger.warning(f"Redis not available: {e}")

    # Try MongoDB if Redis failed
    if backend is None and mongo_config:
        try:
            backend = MongoDBBackend(**mongo_config)
            backend_name = "MongoDB"
            logger.info(f"Using MongoDB backend for {agent_id}:{session_id}")
        except ImportError:
            logger.warning("MongoDB package not installed. Install with: pip install pymongo")
        except Exception as e:
            logger.warning(f"MongoDB not available: {e}")

    # Fallback to JSON
    if backend is None:
        backend = JSONBackend(storage_dir=storage_dir)
        logger.warning(f"Using JSON backend (development mode) for {agent_id}:{session_id}")
        logger.warning("  For production, install Redis: pip install redis")

    # Create ledger with selected backend
    ledger = SmartLedger(
        agent_id=agent_id,
        session_id=session_id,
        ledger_dir=storage_dir,
        backend=backend
    )

    logger.info(f"[LedgerFactory] Created ledger with {backend_name} backend: {ledger}")
    return ledger


def create_ledger_from_environment(
    agent_id: str,
    session_id: str,
    storage_dir: str = ".agent_ledger"
) -> SmartLedger:
    """
    Create ledger using environment variables for configuration.

    Environment variables:
    - REDIS_HOST: Redis server host (default: localhost)
    - REDIS_PORT: Redis server port (default: 6379)
    - REDIS_PASSWORD: Redis password (optional)
    - REDIS_DB: Redis database number (default: 0)
    - MONGO_HOST: MongoDB server host
    - MONGO_PORT: MongoDB server port (default: 27017)
    - MONGO_DATABASE: MongoDB database name (default: agent_ledger)
    - USE_REDIS: Set to "false" to skip Redis (default: true)

    Example:
        export REDIS_HOST=localhost
        export REDIS_PORT=6379
        ledger = create_ledger_from_environment("agent1", "session1")

    Args:
        agent_id: Unique identifier for the agent
        session_id: Unique identifier for this session
        storage_dir: Directory for JSON fallback storage

    Returns:
        SmartLedger instance configured from environment
    """
    import os

    # Parse Redis config from environment
    use_redis = os.getenv("USE_REDIS", "true").lower() == "true"
    redis_config = None
    if use_redis:
        redis_config = {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", "6379")),
            "db": int(os.getenv("REDIS_DB", "0"))
        }
        if os.getenv("REDIS_PASSWORD"):
            redis_config["password"] = os.getenv("REDIS_PASSWORD")

    # Parse MongoDB config from environment
    mongo_config = None
    if os.getenv("MONGO_HOST"):
        mongo_config = {
            "host": os.getenv("MONGO_HOST", "localhost"),
            "port": int(os.getenv("MONGO_PORT", "27017")),
            "database": os.getenv("MONGO_DATABASE", "agent_ledger")
        }
        if os.getenv("MONGO_USERNAME") and os.getenv("MONGO_PASSWORD"):
            mongo_config["username"] = os.getenv("MONGO_USERNAME")
            mongo_config["password"] = os.getenv("MONGO_PASSWORD")

    return create_production_ledger(
        agent_id=agent_id,
        session_id=session_id,
        redis_config=redis_config,
        mongo_config=mongo_config,
        storage_dir=storage_dir,
        prefer_redis=use_redis
    )


def migrate_ledger_to_redis(
    agent_id: str,
    session_id: str,
    source_storage_dir: str = ".agent_ledger",
    redis_config: Optional[dict] = None
) -> bool:
    """
    Migrate existing JSON ledger to Redis for better performance.

    Args:
        agent_id: Agent identifier
        session_id: Session identifier
        source_storage_dir: Directory containing JSON ledger files
        redis_config: Redis connection parameters

    Returns:
        True if migration successful, False otherwise
    """
    try:
        # Load from JSON
        json_backend = JSONBackend(storage_dir=source_storage_dir)
        json_ledger = SmartLedger(
            agent_id=agent_id,
            session_id=session_id,
            ledger_dir=source_storage_dir,
            backend=json_backend
        )

        logger.info(f"Loaded {len(json_ledger.tasks)} tasks from JSON")

        # Save to Redis
        redis_params = redis_config or {"host": "localhost", "port": 6379}
        redis_backend = RedisBackend(**redis_params)
        redis_ledger = SmartLedger(
            agent_id=agent_id,
            session_id=session_id,
            backend=redis_backend
        )

        # Copy all tasks
        redis_ledger.tasks = json_ledger.tasks
        redis_ledger.save()

        logger.info(f"Successfully migrated {len(redis_ledger.tasks)} tasks to Redis")
        return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


# Singleton pattern for shared ledger instances
_ledger_cache = {}


def get_or_create_ledger(
    agent_id: str,
    session_id: str,
    use_cache: bool = True,
    **kwargs
) -> SmartLedger:
    """
    Get existing ledger from cache or create new one.

    This prevents creating multiple ledger instances for the same agent/session,
    which could cause inconsistent state.

    Args:
        agent_id: Agent identifier
        session_id: Session identifier
        use_cache: If False, always create new ledger
        **kwargs: Additional arguments passed to create_production_ledger

    Returns:
        SmartLedger instance (cached or new)
    """
    cache_key = f"{agent_id}_{session_id}"

    if use_cache and cache_key in _ledger_cache:
        logger.info(f"[LedgerFactory] Reusing cached ledger for {cache_key}")
        return _ledger_cache[cache_key]

    ledger = create_production_ledger(agent_id=agent_id, session_id=session_id, **kwargs)

    if use_cache:
        _ledger_cache[cache_key] = ledger

    return ledger


def clear_ledger_cache():
    """Clear the global ledger cache."""
    global _ledger_cache
    _ledger_cache = {}
    logger.info("[LedgerFactory] Cleared ledger cache")
