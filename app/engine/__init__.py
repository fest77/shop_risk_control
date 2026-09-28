# -*- coding: utf-8 -*-
"""哈希链与指标的引擎层（纯计算，不依赖 FastAPI / pymongo）。"""
from __future__ import annotations

from app.engine.audit_hash import (
    CANONICAL_FIELDS,
    GENESIS_HASH,
    canonical_json,
    canonical_payload,
    compute_hash,
    short_hash,
)
from app.engine.metric_agg import (
    DECISION_TO_LEVEL,
    EVENT_SCENE_MAP,
    HIST_KEYS,
    LEVEL_KEYS,
    LEVEL_TO_DECISION,
    SCENE_KEYS,
    build_payloads,
    hist_bucket,
    saved_amount_for,
    scene_of,
)
from app.engine.metric_bucket import (
    DEFAULT_RANGE,
    GRANULARITIES,
    GRANULARITY_MS,
    MAX_TREND_POINTS,
    RANGE_SPECS,
    align,
    bucket_id,
    expire_at,
    iter_bucket_starts,
    range_bounds,
    trend_points,
)
from app.engine.metric_percentile import percentile, total_count

__all__ = [
    # audit
    "CANONICAL_FIELDS", "GENESIS_HASH", "canonical_json", "canonical_payload",
    "compute_hash", "short_hash",
    # metric aggregation
    "DECISION_TO_LEVEL", "EVENT_SCENE_MAP", "HIST_KEYS", "LEVEL_KEYS",
    "LEVEL_TO_DECISION", "SCENE_KEYS", "build_payloads", "hist_bucket",
    "saved_amount_for", "scene_of",
    # metric buckets
    "DEFAULT_RANGE", "GRANULARITIES", "GRANULARITY_MS", "MAX_TREND_POINTS",
    "RANGE_SPECS", "align", "bucket_id", "expire_at", "iter_bucket_starts",
    "range_bounds", "trend_points",
    # percentile
    "percentile", "total_count",
]
