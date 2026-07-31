"""Route and travel-time calculation.

Currently uses LLM estimation. Designed for future map API integration
(Gaode/Baidu/Google) by swapping the implementation behind the same interface.
"""
from __future__ import annotations

import logging

from app.services.tools.base import DataSource, DataWithSource, Place

logger = logging.getLogger(__name__)


def estimate_travel_time_matrix(
    places: list[Place],
    llm_estimates: dict[str, int] | None = None,
) -> dict[str, DataWithSource]:
    """Build a travel-time matrix between places.

    If llm_estimates are provided (from the LLM node), wrap them with
    provenance metadata. Otherwise fall back to distance-based heuristics.
    """
    matrix: dict[str, DataWithSource] = {}

    for i, source in enumerate(places):
        for j, target in enumerate(places):
            if i == j:
                continue
            key = f"{source.place_id}->{target.place_id}"

            if llm_estimates and key in llm_estimates:
                minutes = llm_estimates[key]
                matrix[key] = DataWithSource(
                    value=str(minutes),
                    source=DataSource.LLM_ESTIMATE,
                    confidence=0.6,
                    is_estimated=True,
                )
            else:
                minutes = _heuristic_time(source, target)
                matrix[key] = DataWithSource(
                    value=str(minutes),
                    source=DataSource.INTERNAL,
                    confidence=0.4,
                    is_estimated=True,
                )

    return matrix


def _heuristic_time(source: Place, target: Place) -> int:
    """Rough heuristic when no API or LLM estimate is available."""
    if source.coordinates.lat and target.coordinates.lat:
        import math
        dlat = source.coordinates.lat - target.coordinates.lat
        dlng = source.coordinates.lng - target.coordinates.lng
        dist_km = math.sqrt(dlat**2 + dlng**2) * 111
        if dist_km < 1:
            return 15
        if dist_km < 5:
            return 30
        if dist_km < 15:
            return 50
        return 80
    return 30
