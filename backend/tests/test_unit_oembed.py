"""Unit tests for the oEmbed provider service + route wiring.

Pure offline — no Flask app, no network, no simulation runner, no on-disk
state. Covers the contract the `GET /oembed` endpoint depends on:

  1. ``parse_sim_id_from_url`` extracts a sim id from each recognised
     URL shape (``/share/<id>``, ``/embed/<id>``, ``/simulation/<id>``).
  2. Host allow-listing rejects a foreign domain and a host-less URL.
  3. ``build_oembed_payload`` produces a spec-shaped ``rich`` payload:
     version ``"1.0"``, provider ``MiroShark``, share-card thumbnail,
     ``/embed/<id>`` iframe, title capped at 100 chars.
  4. ``oembed_to_xml`` emits well-formed XML that round-trips through an
     XML parser with the same fields.
  5. The route decorator, the publish gate, the surface-stat increment,
     and the discovery-link injection exist in ``app/api/share.py``.
  6. ``oembed`` is registered in the surface_stats schema.
  7. ``openapi.yaml`` documents ``/oembed`` and the ``OEmbedResponse``
     schema so the drift test stays green.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import oembed_service  # noqa: E402


_ALLOWED = {"miroshark.example.com"}


# ── parse_sim_id_from_url ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://miroshark.example.com/share/sim_abc123", "sim_abc123"),
        ("https://miroshark.example.com/embed/sim_abc123", "sim_abc123"),
        ("https://miroshark.example.com/simulation/sim_abc123/start", "sim_abc123"),
        ("https://miroshark.example.com/share/sim_abc123?ref=notion", "sim_abc123"),
    ],
)
def test_parse_extracts_sim_id_from_known_shapes(url: str, expected: str):
    assert oembed_service.parse_sim_id_from_url(url, allowed_hosts=_ALLOWED) == expected


def test_parse_rejects_foreign_domain():
    """A share-shaped path on a host we don't own must not resolve — the
    endpoint can't be aimed at someone else's site."""
    foreign = "https://evil.example.org/share/sim_abc123"
    assert oembed_service.parse_sim_id_from_url(foreign, allowed_hosts=_ALLOWED) is None


def test_parse_rejects_hostless_url():
    """A bare path has no host to validate against — reject it."""
    assert oembed_service.parse_sim_id_from_url("/share/sim_abc123", allowed_hosts=_ALLOWED) is None


def test_parse_rejects_url_with_no_sim_path():
    """A valid-host URL that doesn't point at a sim surface returns None."""
    assert oembed_service.parse_sim_id_from_url(
        "https://miroshark.example.com/explore", allowed_hosts=_ALLOWED
    ) is None


def test_parse_skips_host_check_when_allowlist_is_none():
    """With no allow-list supplied the host gate is skipped (pure path
    extraction) — the route always supplies one."""
    assert oembed_service.parse_sim_id_from_url(
        "https://anything.test/share/sim_xyz1", allowed_hosts=None
    ) == "sim_xyz1"


def test_parse_matches_host_with_port():
    """Dev / proxy hosts carry a port — the match is exact incl. port."""
    assert oembed_service.parse_sim_id_from_url(
        "http://localhost:5000/share/sim_local1", allowed_hosts={"localhost:5000"}
    ) == "sim_local1"


def test_parse_rejects_empty_and_non_string():
    assert oembed_service.parse_sim_id_from_url("", allowed_hosts=_ALLOWED) is None
    assert oembed_service.parse_sim_id_from_url(None, allowed_hosts=_ALLOWED) is None  # type: ignore[arg-type]


# ── build_oembed_payload ───────────────────────────────────────────────────


def test_payload_has_rich_type_and_required_fields():
    payload = oembed_service.build_oembed_payload(
        "Will BTC break 100k by Q3?", "sim_abc123", "https://miroshark.example.com"
    )
    assert payload["version"] == "1.0"
    assert payload["type"] == "rich"
    assert payload["provider_name"] == "MiroShark"
    assert payload["provider_url"] == "https://miroshark.example.com"
    assert payload["cache_age"] == 300


def test_payload_thumbnail_and_iframe_point_at_existing_surfaces():
    payload = oembed_service.build_oembed_payload(
        "scenario", "sim_abc123", "https://miroshark.example.com"
    )
    assert payload["thumbnail_url"] == (
        "https://miroshark.example.com/api/simulation/sim_abc123/share-card.png"
    )
    assert payload["thumbnail_width"] == 1200
    assert payload["thumbnail_height"] == 630
    assert "<iframe" in payload["html"]
    assert "/embed/sim_abc123" in payload["html"]
    assert 'width="800"' in payload["html"]
    assert 'height="500"' in payload["html"]


def test_payload_strips_trailing_slash_on_base():
    payload = oembed_service.build_oembed_payload(
        "scenario", "sim_abc123", "https://miroshark.example.com/"
    )
    assert "//api/simulation" not in payload["thumbnail_url"]
    assert payload["thumbnail_url"].startswith("https://miroshark.example.com/api/")


def test_payload_title_capped_at_100_chars():
    long_scenario = "A" * 250
    payload = oembed_service.build_oembed_payload(
        long_scenario, "sim_abc123", "https://miroshark.example.com"
    )
    assert len(payload["title"]) <= 100
    assert payload["title"].endswith("…")


def test_payload_empty_scenario_gets_fallback_title():
    payload = oembed_service.build_oembed_payload(
        "", "sim_abc123", "https://miroshark.example.com"
    )
    assert payload["title"] == "MiroShark Simulation"


# ── oembed_to_xml ──────────────────────────────────────────────────────────


def test_xml_is_well_formed_and_round_trips():
    payload = oembed_service.build_oembed_payload(
        "scenario", "sim_abc123", "https://miroshark.example.com"
    )
    xml = oembed_service.oembed_to_xml(payload)
    assert xml.startswith("<?xml")

    # Strip the declaration so ElementTree parses the body.
    body = xml.split("?>", 1)[1].strip()
    root = ET.fromstring(body)
    assert root.tag == "oembed"

    fields = {child.tag: child.text for child in root}
    assert fields["version"] == "1.0"
    assert fields["type"] == "rich"
    assert fields["provider_name"] == "MiroShark"
    # The iframe markup is carried as escaped text the consumer un-escapes.
    assert "<iframe" in (fields["html"] or "")


# ── Static wiring guards ───────────────────────────────────────────────────


def _read_share_api() -> str:
    return (_BACKEND / "app" / "api" / "share.py").read_text(encoding="utf-8")


def test_route_decorator_registered_on_share_bp():
    text = _read_share_api()
    assert "@share_bp.route('/oembed'" in text or '@share_bp.route("/oembed"' in text, (
        "share.py must register the /oembed route on share_bp"
    )


def test_route_enforces_publish_gate():
    text = _read_share_api()
    assert "is_public" in text
    # The oEmbed handler reuses the manager-based publish lookup.
    assert "get_simulation_config" in text


def test_route_increments_oembed_surface_stat():
    text = _read_share_api()
    assert '"oembed"' in text, (
        "share.py must increment the oembed counter via "
        "surface_stats.increment_surface_stat(..., \"oembed\")"
    )
    assert "increment_surface_stat" in text


def test_share_page_injects_discovery_links():
    text = _read_share_api()
    assert "application/json+oembed" in text, (
        "the share page <head> must emit the JSON oEmbed discovery <link>"
    )
    assert "text/xml+oembed" in text, (
        "the share page <head> must emit the XML oEmbed discovery <link>"
    )


def test_surface_stats_registers_oembed_key():
    from app.services import surface_stats

    assert "oembed" in surface_stats.SURFACE_KEYS


def test_openapi_documents_oembed_path_and_schema():
    spec_text = (_BACKEND / "openapi.yaml").read_text(encoding="utf-8")
    assert "/oembed:" in spec_text, "openapi.yaml is missing the /oembed path entry"
    assert "OEmbedResponse:" in spec_text, "openapi.yaml is missing the OEmbedResponse schema"
