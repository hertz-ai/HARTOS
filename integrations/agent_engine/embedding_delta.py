"""
Embedding Delta — Compression, validation, aggregation, and anomaly detection.

Pure functions operating on numpy arrays. Used by gradient_service.py for
distributed embedding synchronization across HART nodes.

Phase 1: Embedding sync (compressed representation deltas, <100KB per round).
Phase 2: LoRA gradient sync (stubs in federated_gradient_protocol.py).

Intelligence is earned through contribution. Every compute cycle donated
makes the hive smarter. 90% of value flows back to contributors.
"""
import logging
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

MAX_DELTA_SIZE_BYTES = 102_400  # 100KB hard cap per delta
DEFAULT_TOP_K = 32              # Default number of principal components to keep
MIN_PEERS_FOR_AGGREGATION = 1   # Minimum peers needed for trimmed mean
ANOMALY_SIGMA = 3.0             # Z-score threshold for magnitude anomaly
DIRECTION_FLIP_THRESHOLD = -0.5 # Cosine similarity below this = direction flip
MAX_DIMENSION = 8192            # Maximum embedding dimension we accept


# ─── Compression ───

def compress_delta(raw_values: list,
                   method: str = 'top_k',
                   k: int = DEFAULT_TOP_K) -> Dict:
    """Compress an embedding delta for transmission.

    Keeps only the top-k components by absolute magnitude. Falls back to
    uniform sampling if fewer than k non-zero values exist.

    Args:
        raw_values: List of float values (embedding delta).
        method: Compression method ('top_k' or 'none').
        k: Number of components to keep.

    Returns:
        {'method': str, 'k': int, 'dimension': int,
         'indices': [int], 'values': [float], 'magnitude': float}
    """
    if not raw_values:
        return {'method': method, 'k': 0, 'dimension': 0,
                'indices': [], 'values': [], 'magnitude': 0.0}

    dimension = len(raw_values)
    if dimension > MAX_DIMENSION:
        raw_values = raw_values[:MAX_DIMENSION]
        dimension = MAX_DIMENSION

    magnitude = _magnitude(raw_values)

    if method == 'none' or k >= dimension:
        return {
            'method': 'none', 'k': dimension, 'dimension': dimension,
            'indices': list(range(dimension)),
            'values': [round(v, 8) for v in raw_values],
            'magnitude': round(magnitude, 8),
        }

    # Top-k by absolute value
    indexed = [(i, v) for i, v in enumerate(raw_values)]
    indexed.sort(key=lambda x: abs(x[1]), reverse=True)
    top = indexed[:k]
    top.sort(key=lambda x: x[0])  # Restore index order

    return {
        'method': 'top_k',
        'k': k,
        'dimension': dimension,
        'indices': [i for i, _ in top],
        'values': [round(v, 8) for _, v in top],
        'magnitude': round(magnitude, 8),
    }


def decompress_delta(compressed: Dict) -> list:
    """Reconstruct full-dimension delta from compressed form.

    Missing indices are filled with 0.0.
    """
    dimension = compressed.get('dimension', 0)
    if dimension <= 0:
        return []

    result = [0.0] * dimension
    indices = compressed.get('indices', [])
    values = compressed.get('values', [])
    for idx, val in zip(indices, values):
        if 0 <= idx < dimension:
            result[idx] = val
    return result


# ─── Validation ───

def validate_delta(delta: Dict) -> Tuple[bool, str]:
    """Validate a compressed embedding delta for correctness and size.

    Returns: (valid: bool, reason: str)
    """
    if not isinstance(delta, dict):
        return False, 'not_a_dict'

    dimension = delta.get('dimension', 0)
    if not isinstance(dimension, int) or dimension <= 0:
        return False, 'invalid_dimension'
    if dimension > MAX_DIMENSION:
        return False, f'dimension_too_large ({dimension} > {MAX_DIMENSION})'

    indices = delta.get('indices', [])
    values = delta.get('values', [])
    if len(indices) != len(values):
        return False, 'indices_values_length_mismatch'

    # Check indices are valid
    for idx in indices:
        if not isinstance(idx, int) or idx < 0 or idx >= dimension:
            return False, f'invalid_index ({idx})'

    # Check for duplicates
    if len(set(indices)) != len(indices):
        return False, 'duplicate_indices'

    # Check values are numeric
    for v in values:
        if not isinstance(v, (int, float)):
            return False, 'non_numeric_value'
        if math.isnan(v) or math.isinf(v):
            return False, 'nan_or_inf_value'

    # Size estimate (rough: 12 bytes per index+value pair + overhead)
    estimated_size = len(indices) * 12 + 100
    if estimated_size > MAX_DELTA_SIZE_BYTES:
        return False, f'estimated_size_too_large ({estimated_size})'

    return True, 'ok'


# ─── Aggregation ───

def trimmed_mean_aggregate(deltas: List[Dict],
                           sigma: float = ANOMALY_SIGMA,
                           weights: Optional[List[float]] = None) -> Dict:
    """Aggregate multiple embedding deltas using trimmed mean.

    Removes contributions with magnitude > sigma standard deviations from
    the mean before averaging. This is Byzantine-resilient for Phase 1.

    Args:
        deltas: List of compressed delta dicts.
        sigma: Z-score threshold for outlier removal.
        weights: Optional per-delta weights (e.g., contribution_score).

    Returns:
        Aggregated compressed delta dict.
    """
    if not deltas:
        return {'method': 'aggregated', 'k': 0, 'dimension': 0,
                'indices': [], 'values': [], 'magnitude': 0.0,
                'peer_count': 0, 'outliers_removed': 0}

    if len(deltas) == 1:
        result = dict(deltas[0])
        result['peer_count'] = 1
        result['outliers_removed'] = 0
        return result

    # Decompress all deltas to full dimension
    dimension = max(d.get('dimension', 0) for d in deltas)
    if dimension <= 0:
        return {'method': 'aggregated', 'k': 0, 'dimension': 0,
                'indices': [], 'values': [], 'magnitude': 0.0,
                'peer_count': len(deltas), 'outliers_removed': 0}

    # Normalize all to same dimension
    full_deltas = []
    magnitudes = []
    for d in deltas:
        full = decompress_delta(d)
        # Pad if needed
        if len(full) < dimension:
            full.extend([0.0] * (dimension - len(full)))
        elif len(full) > dimension:
            full = full[:dimension]
        full_deltas.append(full)
        magnitudes.append(_magnitude(full))

    # Outlier detection on magnitudes
    outlier_mask = _detect_outliers(magnitudes, sigma)
    outliers_removed = sum(outlier_mask)

    # Filter
    filtered_deltas = []
    filtered_weights = []
    for i, (fd, is_outlier) in enumerate(zip(full_deltas, outlier_mask)):
        if not is_outlier:
            filtered_deltas.append(fd)
            w = weights[i] if weights and i < len(weights) else 1.0
            filtered_weights.append(max(0.01, w))

    if not filtered_deltas:
        # All were outliers — fall back to simple mean of all
        filtered_deltas = full_deltas
        filtered_weights = [1.0] * len(full_deltas)
        outliers_removed = 0

    # Weighted mean
    total_weight = sum(filtered_weights)
    aggregated_values = [0.0] * dimension
    for fd, w in zip(filtered_deltas, filtered_weights):
        for j in range(dimension):
            aggregated_values[j] += fd[j] * (w / total_weight)

    # Re-compress the result
    compressed = compress_delta(aggregated_values, method='top_k',
                                k=min(DEFAULT_TOP_K, dimension))
    compressed['peer_count'] = len(deltas)
    compressed['outliers_removed'] = outliers_removed
    return compressed


# ─── Anomaly Detection ───

def detect_magnitude_anomaly(magnitude: float,
                              peer_magnitudes: List[float],
                              sigma: float = ANOMALY_SIGMA) -> bool:
    """Detect if a single delta's magnitude is anomalous vs peer population.

    Used by IntegrityService for gradient_magnitude_anomaly fraud signal.
    """
    if len(peer_magnitudes) < 2:
        return False

    mean_m = sum(peer_magnitudes) / len(peer_magnitudes)
    variance = sum((m - mean_m) ** 2 for m in peer_magnitudes) / len(peer_magnitudes)
    stddev = math.sqrt(variance) if variance > 0 else 0.0

    if stddev < 1e-10:
        # All magnitudes are essentially equal
        return abs(magnitude - mean_m) > 1e-6

    z_score = abs(magnitude - mean_m) / stddev
    return z_score > sigma


def detect_direction_flip(current_values: list,
                           previous_values: list) -> bool:
    """Detect if embedding delta has flipped direction vs previous round.

    A direction flip (cosine similarity < -0.5) indicates potential
    adversarial gradient manipulation.
    """
    if not current_values or not previous_values:
        return False

    min_len = min(len(current_values), len(previous_values))
    if min_len == 0:
        return False

    cos_sim = _cosine_similarity(
        current_values[:min_len], previous_values[:min_len])
    return cos_sim < DIRECTION_FLIP_THRESHOLD


# ─── Internal Helpers ───

def _magnitude(values: list) -> float:
    """L2 norm of a vector."""
    return math.sqrt(sum(v * v for v in values)) if values else 0.0


def _cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return dot / (norm_a * norm_b)


def _detect_outliers(values: List[float], sigma: float) -> List[bool]:
    """Return boolean mask: True if value is > sigma stddevs from mean."""
    if len(values) < 3:
        return [False] * len(values)

    mean_v = sum(values) / len(values)
    variance = sum((v - mean_v) ** 2 for v in values) / len(values)
    stddev = math.sqrt(variance) if variance > 0 else 0.0

    if stddev < 1e-10:
        return [False] * len(values)

    return [abs(v - mean_v) / stddev > sigma for v in values]
