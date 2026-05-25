"""oEmbed 1.0 provider for MiroShark simulation share URLs.

Implements the discovery half of the `oEmbed spec <https://oembed.com>`_
so a paste of a MiroShark ``/share/<id>`` link auto-unfurls into a rich
sim card on any oEmbed consumer — Notion, Ghost, Substack, WordPress, and
the long tail of CMS / note-taking tools that all implement the same
protocol.

The share page already emits Open Graph / Twitter / Farcaster-Frame meta
tags, which cover the *social* platforms (Twitter/X, Discord, Slack,
LinkedIn, Warpcast). Those platforms read meta tags. The *writing*
platforms — where researchers and analysts actually publish — read an
``<link rel="alternate" type="application/json+oembed">`` discovery tag
instead, then call back to this endpoint for a structured embed payload.
Before this surface, a MiroShark link pasted into a Notion page or a
Substack draft rendered as a bare URL; with it, every organic citation
becomes a rich preview card with no user action.

This module is the pure, Flask-free core: URL → ``sim_id`` parsing with
host allow-listing, payload construction, and JSON↔XML serialization.
The route handler in ``app/api/share.py`` owns the publish gate, the
surface-stat increment, and the request/response plumbing.

Design notes
------------

* **Pure stdlib.** ``re`` + ``urllib.parse`` + ``xml.etree.ElementTree``
  + ``html``. No new dependencies — the same posture as every other
  surface module (signal.json, polymarket.json, badge.svg, …).
* **A protocol, not a new renderer.** The ``thumbnail_url`` points at the
  existing ``/api/simulation/<id>/share-card.png`` surface and the
  ``html`` iframe at the existing ``/embed/<id>`` SPA route. oEmbed adds
  a *discovery protocol* over surfaces that already ship; it renders
  nothing new itself.
* **Host allow-listing, never fetching.** ``parse_sim_id_from_url`` never
  dereferences the URL. It extracts a ``sim_id`` only from a path that
  lives on a host this deployment owns (the caller passes the allow-list
  in). A foreign-host ``url``, a bare path with no host, or a URL with no
  recognisable sim path all yield ``None`` so the route can answer 404 —
  the endpoint can't be coerced into describing content it doesn't host.
* **Type ``rich``.** A MiroShark embed is an interactive iframe, not a
  static photo or a raw video file, so the oEmbed ``type`` is ``rich``
  with an ``html`` iframe payload, per the spec's rich-type contract.
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


# oEmbed version this provider speaks. Required top-level field per the
# 1.0 spec; consumers key their parsing on it.
OEMBED_VERSION = "1.0"

# A MiroShark embed is an interactive iframe → the rich type. Photo /
# video types would require a static asset URL we don't expose.
OEMBED_TYPE = "rich"

PROVIDER_NAME = "MiroShark"

# Fixed embed geometry. Matches the 800×500 iframe the EmbedDialog hands
# out and the 1200×630 OG share-card thumbnail every other surface uses.
EMBED_WIDTH = 800
EMBED_HEIGHT = 500
THUMBNAIL_WIDTH = 1200
THUMBNAIL_HEIGHT = 630

# Consumers may cache the embed this long (seconds). Matches the
# 5-minute Cache-Control the share page and signal.json already set.
CACHE_AGE_SECONDS = 300

# oEmbed title field cap. Long scenario prompts are truncated with an
# ellipsis so the unfurled card title stays a headline, not a paragraph.
_TITLE_MAX = 100

# Sim-id path matcher. Accepts the three public URL shapes that reference
# a single simulation:
#   * /share/<id>            — the canonical paste-able landing URL
#   * /embed/<id>            — the iframe SPA route
#   * /simulation/<id>/...   — the SPA deep link (e.g. /simulation/<id>/start)
# The id char class mirrors ``validate_simulation_id`` (alphanumeric, dot,
# hyphen, underscore); the 4–64 length bound rejects a bare ``..`` and
# keeps the match tight. The route handler still runs the captured id
# through ``validate_simulation_id`` for path-traversal defense in depth.
_SIM_PATH_RE = re.compile(r"/(?:share|embed|simulation)/([A-Za-z0-9_.\-]{4,64})")


def _host_of(url: str) -> Optional[str]:
    """Return the lower-cased ``host[:port]`` of an absolute URL, or ``None``.

    Requires both a scheme and a netloc so a bare path (``/share/x``) or a
    scheme-relative string is treated as un-validatable — without a host
    we can't prove the URL belongs to this deployment, so the caller
    rejects it.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.netloc.lower()


def parse_sim_id_from_url(
    url: str,
    allowed_hosts: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Extract a simulation id from a MiroShark URL, or ``None``.

    ``url`` must be an absolute URL whose host is in ``allowed_hosts``
    (when supplied) — a foreign domain returns ``None`` so the oEmbed
    endpoint can't be pointed at someone else's site. When
    ``allowed_hosts`` is ``None`` the host check is skipped (used by the
    pure unit tests); the route handler always supplies it.

    The path must contain a recognised sim surface (``/share/<id>``,
    ``/embed/<id>``, or ``/simulation/<id>``). Returns the captured id —
    never validated for filesystem safety here; the caller does that.
    """
    host = _host_of(url)
    if host is None:
        return None

    if allowed_hosts is not None:
        normalized = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
        if host not in normalized:
            return None

    parsed = urlparse(url.strip())
    match = _SIM_PATH_RE.search(parsed.path or "")
    if not match:
        return None
    return match.group(1)


def _truncate_title(scenario: Optional[str]) -> str:
    """Collapse a scenario prompt into an oEmbed ``title`` ≤ ``_TITLE_MAX``.

    Falls back to a generic title when the scenario is empty (a published
    sim with no recorded requirement still gets a usable card). The result
    is always at most ``_TITLE_MAX`` characters including the ellipsis.
    """
    title = (scenario or "").strip()
    if not title:
        return "MiroShark Simulation"
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return title


def build_oembed_payload(scenario: Optional[str], sim_id: str, base_url: str) -> dict:
    """Build the oEmbed ``rich`` payload for a published simulation.

    ``base_url`` is the deployment's canonical origin (no trailing slash);
    the ``thumbnail_url`` and iframe ``src`` are absolute so the embed
    works when rendered on a third-party domain. The iframe ``src`` is
    HTML-attribute-escaped because the ``html`` field is injected verbatim
    into the consumer's page — ``sim_id`` is validated upstream and
    ``base_url`` is operator-controlled, but escaping keeps the surface
    safe even if a proxy header ever smuggles a quote into the host.
    """
    base = (base_url or "").rstrip("/")
    iframe_src = html.escape(f"{base}/embed/{sim_id}", quote=True)

    return {
        "version": OEMBED_VERSION,
        "type": OEMBED_TYPE,
        "provider_name": PROVIDER_NAME,
        "provider_url": base,
        "title": _truncate_title(scenario),
        "thumbnail_url": f"{base}/api/simulation/{sim_id}/share-card.png",
        "thumbnail_width": THUMBNAIL_WIDTH,
        "thumbnail_height": THUMBNAIL_HEIGHT,
        "width": EMBED_WIDTH,
        "height": EMBED_HEIGHT,
        "html": (
            f'<iframe src="{iframe_src}" width="{EMBED_WIDTH}" '
            f'height="{EMBED_HEIGHT}" frameborder="0" '
            f'allowfullscreen></iframe>'
        ),
        "cache_age": CACHE_AGE_SECONDS,
    }


def oembed_to_xml(payload: dict) -> str:
    """Serialize an oEmbed payload dict to the spec's XML representation.

    The oEmbed spec defines both a JSON and an XML format; a consumer
    selects one via the discovery ``<link type>`` or a ``?format=`` query
    param. The XML form is a flat ``<oembed>`` element with one child per
    field. ``ElementTree`` escapes every text node (so the ``html``
    iframe field is emitted as entity-escaped text the consumer
    un-escapes) and we prepend an explicit declaration for determinism.
    """
    root = ET.Element("oembed")
    for key, value in payload.items():
        child = ET.SubElement(root, key)
        child.text = str(value)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + body
