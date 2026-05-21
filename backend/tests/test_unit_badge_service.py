"""Unit tests for the status-badge SVG renderer.

Pure offline — no Flask, no network, no simulation runner, no on-disk
state. Covers the properties the ``/badge.svg`` endpoint depends on:

  1. The SVG is well-formed XML and has the expected root element +
     namespace.
  2. The ``aria-label`` carries the rendered status — every screen
     reader picks it up.
  3. Direction → fill colour mapping (bullish / neutral / bearish)
     matches the pinned colour vocabulary every other surface uses.
  4. The right-hand label echoes the direction + integer-rounded
     confidence percentage.
  5. Unknown / missing direction falls back to neutral grey + an
     ``Unknown`` label rather than raising.
  6. Confidence outside ``[0, 100]`` clamps; non-numeric becomes ``0``.
  7. Cache header + content-type wired correctly on the served route.
     (Static text scan over the route file — the route file lives
     outside this module and the full integration test belongs in
     the live-server suite.)
  8. ``badge_svg`` is registered in the surface_stats schema and the
     handler increments the counter.
  9. The rendered SVG is bytewise-deterministic across calls.
 10. Pill-style rounded corners (``rx=3``) are present so the badge
     renders correctly in `<img>` consumers.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ── Property 1 — well-formed SVG with expected root + namespace ───────────


def test_badge_renders_well_formed_svg():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    # ET.fromstring would raise on malformed XML — the parse itself
    # is the assertion.
    root = ET.fromstring(body)
    assert root.tag.endswith("svg"), f"root must be <svg>; got {root.tag!r}"
    # ``role="img"`` so assistive tech treats the badge as an image,
    # not as a generic graphic.
    assert root.attrib.get("role") == "img"


def test_badge_declares_svg_namespace():
    """`<img>` consumers reject SVGs lacking the SVG namespace."""
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    root = ET.fromstring(body)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"


# ── Property 2 — aria-label + title carry the status ──────────────────────


def test_aria_label_contains_direction_and_confidence():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    root = ET.fromstring(body)
    aria = root.attrib.get("aria-label", "")
    assert "MiroShark" in aria
    assert "Bullish" in aria
    assert "72%" in aria


def test_title_element_is_present_for_screen_readers():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    root = ET.fromstring(body)
    titles = root.findall("{http://www.w3.org/2000/svg}title")
    assert titles, "badge must include a <title> element for a11y"
    assert "Bullish" in (titles[0].text or "")


# ── Property 3 — direction → colour mapping ───────────────────────────────


@pytest.mark.parametrize(
    "direction, expected_color",
    [
        ("Bullish", "#22c55e"),
        ("bullish", "#22c55e"),
        ("BULLISH", "#22c55e"),
        ("Neutral", "#6b7280"),
        ("neutral", "#6b7280"),
        ("Bearish", "#ef4444"),
        ("bearish", "#ef4444"),
    ],
)
def test_direction_maps_to_canonical_stance_color(direction, expected_color):
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg(direction, 65.0)
    # The right-half ``<rect>`` carries the stance colour.
    assert expected_color in body, (
        f"badge for {direction!r} must paint the right half {expected_color!r}; "
        f"badge body did not include the colour"
    )


# ── Property 4 — right-hand label echoes direction + integer confidence ───


@pytest.mark.parametrize(
    "direction, confidence, expected_token",
    [
        ("Bullish", 72.4, "Bullish 72%"),
        ("Bullish", 72.6, "Bullish 73%"),
        ("Neutral", 50.0, "Neutral 50%"),
        ("Bearish", 88.9, "Bearish 89%"),
        ("Bullish", 0.0, "Bullish 0%"),
        ("Bearish", 100.0, "Bearish 100%"),
    ],
)
def test_right_label_echoes_direction_and_integer_confidence(
    direction, confidence, expected_token
):
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg(direction, confidence)
    assert expected_token in body, (
        f"badge for ({direction!r}, {confidence!r}) must render "
        f"{expected_token!r}; not found in SVG body"
    )


# ── Property 5 — defensive on unknown / missing direction ─────────────────


def test_unknown_direction_falls_back_to_neutral_grey():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Sideways", 50.0)
    assert "#6b7280" in body, "unknown direction should paint the neutral grey"


def test_none_direction_renders_unknown_label():
    """Should not raise; should still produce a valid badge with an
    explicit ``Unknown`` label rather than a misleading stance."""
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg(None, 50.0)
    assert "Unknown" in body
    # And still neutral-grey.
    assert "#6b7280" in body


def test_empty_string_direction_renders_unknown_label():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("", 50.0)
    assert "Unknown" in body


# ── Property 6 — confidence clamping + coercion ───────────────────────────


def test_negative_confidence_clamps_to_zero():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", -10.0)
    assert "Bullish 0%" in body


def test_over_hundred_confidence_clamps_to_hundred():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 150.0)
    assert "Bullish 100%" in body


def test_non_numeric_confidence_becomes_zero():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", "not-a-number")
    assert "Bullish 0%" in body


def test_none_confidence_becomes_zero():
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", None)
    assert "Bullish 0%" in body


# ── Property 7 — route + content-type wiring ──────────────────────────────


def test_badge_route_decorator_exists():
    """Static guard: a future refactor that removes the decorator must
    fail this test before reaching CI."""
    api_file = _BACKEND / "app" / "api" / "simulation.py"
    text = api_file.read_text(encoding="utf-8")
    assert "/<simulation_id>/badge.svg" in text
    assert "def get_status_badge_svg" in text


def test_badge_route_sets_svg_mimetype_and_short_cache():
    api_file = _BACKEND / "app" / "api" / "simulation.py"
    text = api_file.read_text(encoding="utf-8")
    # The handler must serve as image/svg+xml and cache for a minute
    # so live-sim badges reflect a stance flip within one polling
    # cycle. Two string checks keep this independent of formatting.
    assert "image/svg+xml" in text
    assert "max-age=60" in text


# ── Property 8 — surface_stats registration + counter increment ───────────


def test_badge_svg_registered_in_surface_stats():
    from app.services.surface_stats import SURFACE_KEYS, read_surface_stats

    assert "badge_svg" in SURFACE_KEYS
    stats = read_surface_stats(None)
    assert stats["badge_svg"] == 0
    assert "total" in stats


def test_badge_handler_increments_counter():
    """A static check that the handler references ``"badge_svg"`` for
    the counter increment — the integration test covers the actual
    file write."""
    api_file = _BACKEND / "app" / "api" / "simulation.py"
    text = api_file.read_text(encoding="utf-8")
    assert '"badge_svg"' in text


# ── Property 9 — bytewise deterministic across calls ──────────────────────


def test_badge_render_is_bytewise_deterministic():
    """Two calls with identical inputs must produce identical bytes —
    a future ETag layer / on-disk cache relies on this."""
    from app.services.badge_service import build_badge_svg

    one = build_badge_svg("Bullish", 72.4)
    two = build_badge_svg("Bullish", 72.4)
    assert one == two


def test_bytes_wrapper_includes_xml_declaration():
    from app.services.badge_service import render_badge_svg_bytes

    payload = render_badge_svg_bytes("Bullish", 72.4)
    assert payload.startswith(b'<?xml version="1.0"')
    # The body is valid UTF-8 SVG bytes.
    decoded = payload.decode("utf-8")
    # The body parses if we strip the declaration.
    body = decoded.split("?>", 1)[-1]
    root = ET.fromstring(body)
    assert root.tag.endswith("svg")


# ── Property 10 — rounded-corner pill styling ─────────────────────────────


def test_badge_has_rounded_pill_corners():
    """Shields.io flat badges use ``rx=3`` for the pill ends — the
    clipPath in our renderer references that value."""
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    assert 'rx="3"' in body


def test_badge_height_is_shields_io_flat_standard():
    """20 px is the canonical Shields.io flat-badge height — a
    MiroShark badge sitting next to a GitHub-Actions badge in a
    README must match."""
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    root = ET.fromstring(body)
    assert root.attrib.get("height") == "20"


def test_badge_left_label_is_miroshark():
    """The left half must carry the product name so a reader skimming
    a README knows what the badge represents before clicking through."""
    from app.services.badge_service import build_badge_svg

    body = build_badge_svg("Bullish", 72.4)
    assert "MiroShark" in body
    assert "#555555" in body, "left half must be Shields.io standard grey"


# ── Property 11 — viewBox + width scale together ──────────────────────────


def test_badge_width_scales_with_label_length():
    """A longer right label (Bearish + 3-digit confidence) must
    produce a wider total badge — so renderers that read ``width``
    don't clip text."""
    from app.services.badge_service import build_badge_svg

    narrow = ET.fromstring(build_badge_svg("Bullish", 5.0))
    wide = ET.fromstring(build_badge_svg("Bearish", 100.0))
    # "Bullish 5%" vs "Bearish 100%" — the wider label must produce a
    # wider total width.
    assert int(wide.attrib["width"]) > int(narrow.attrib["width"])


def test_badge_viewbox_matches_width_and_height():
    """The viewBox must match the declared width/height so the SVG
    scales cleanly under ``<img width=... height=...>`` without
    distortion."""
    from app.services.badge_service import build_badge_svg

    root = ET.fromstring(build_badge_svg("Bullish", 72.4))
    width = root.attrib["width"]
    height = root.attrib["height"]
    assert root.attrib["viewBox"] == f"0 0 {width} {height}"
