"""Platform-level aggregate stats endpoints.

Two surfaces on one blueprint:

* ``GET /api/stats`` — JSON envelope describing every public,
  completed simulation on disk (total count, consensus distribution,
  average confidence, total surface views, unique projects, newest sim
  metadata). The first endpoint that describes the platform itself
  rather than one simulation; press kits, external dashboards, and
  LLM-agent health checks all consume the same payload.

* ``GET /api/stats/badge.svg`` — A flat 20-pixel Shields.io-compatible
  pill (``MiroShark | N simulations``) that any README, Substack, or
  portfolio can embed in one line of Markdown. The platform-level
  sibling of the per-sim ``/badge.svg`` introduced in PR #94.

Both endpoints are public (no auth), cache for 60 seconds, and reuse
the same ``compute_platform_stats`` scan. The JSON endpoint emits a
short ETag derived from ``total_sims`` + ``newest_sim_id`` so a
conditional ``If-None-Match`` GET short-circuits to ``304`` without
serialising the body — useful for the README badge that polls every
minute.

Sandbox note: stdlib + Flask only. The scan walks
``Config.WONDERWALL_SIMULATION_DATA_DIR`` directly; no Neo4j, no LLM,
no outbound network.
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from ..config import Config
from ..services import platform_stats as platform_stats_service
from ..services.badge_service import render_platform_badge_svg_bytes
from ..utils.logger import get_logger


logger = get_logger("miroshark.api.stats")


stats_bp = Blueprint("stats", __name__)


def _cache_header() -> str:
    """Cache-Control value shared by both endpoints.

    60 seconds matches the cache TTL on ``compute_platform_stats`` and
    the per-sim ``/badge.svg`` route's cache header — a polling
    consumer sees consistent freshness across the platform-stats and
    per-sim badge surfaces.
    """
    return "public, max-age=60"


@stats_bp.route("", methods=["GET"])
def get_platform_stats() -> Response:
    """Return platform-level aggregate statistics as JSON.

    Response shape::

        {
          "success": true,
          "data": {
            "schema_version": "1",
            "total_sims": <int>,
            "consensus_distribution": {
              "bullish": <int>, "neutral": <int>, "bearish": <int>,
              "bullish_pct": <float>, "neutral_pct": <float>,
              "bearish_pct": <float>
            },
            "avg_confidence_pct": <float>,
            "total_surface_views": <int>,
            "unique_projects": <int>,
            "newest_sim_id": <str | null>,
            "newest_sim_created_at": <ISO-8601 str | null>
          }
        }

    ETag header is set; a matching ``If-None-Match`` short-circuits to
    ``304 Not Modified`` so polling consumers (READMEs that embed the
    badge, dashboards refreshing every minute) don't pay the JSON
    serialisation cost on every request.

    Cache-Control: ``public, max-age=60`` to absorb bursty press
    unfurls. The 60-second cache window matches the
    ``compute_platform_stats`` module-level cache exactly — every call
    after the first inside the window is a dict copy, not a disk scan.
    """
    try:
        payload = platform_stats_service.compute_platform_stats(
            Config.WONDERWALL_SIMULATION_DATA_DIR
        )
    except Exception as exc:
        logger.error(f"Failed to compute platform stats: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500

    etag = platform_stats_service.stats_etag(payload)
    if_none_match = (request.headers.get("If-None-Match") or "").strip()
    if if_none_match and if_none_match == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = _cache_header()
        return resp

    response = jsonify({"success": True, "data": payload})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _cache_header()
    return response


@stats_bp.route("/badge.svg", methods=["GET"])
def get_platform_stats_badge() -> Response:
    """Render the platform-stats badge as an SVG.

    A flat 20-pixel Shields.io-style pill: ``MiroShark`` (grey) +
    ``N simulations`` (platform-blue). Count is the same
    ``total_sims`` value the JSON endpoint reports. Always returns
    ``200`` with a renderable badge — a zero-sim instance still
    produces a valid ``MiroShark | 0 simulations`` pill rather than
    a 404, so a README embed never breaks on a fresh deployment.

    Cache-Control: ``public, max-age=60``; Content-Type:
    ``image/svg+xml``.
    """
    try:
        payload = platform_stats_service.compute_platform_stats(
            Config.WONDERWALL_SIMULATION_DATA_DIR
        )
        count = int(payload.get("total_sims", 0) or 0)
    except Exception as exc:
        logger.warning(
            f"platform-stats badge: stats computation failed, rendering 0-sim badge: {exc}"
        )
        count = 0

    body = render_platform_badge_svg_bytes(count)
    response = Response(body, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = _cache_header()
    return response
