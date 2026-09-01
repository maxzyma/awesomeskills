"""Checks on the static files the page references directly.

A broken asset here fails silently: the server returns 200, the browser drops it, and the
page looks merely unstyled rather than wrong. The favicon shipped malformed on the first
attempt -- an XML comment containing `--`, which is illegal -- and neither the HTTP status
nor the page render gave any sign of it.
"""

from __future__ import annotations

import re
import xml.dom.minidom
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "site" / "public"


def test_the_favicon_is_well_formed_xml():
    """SVG is XML, so a browser rejects the whole document on any parse error."""
    xml.dom.minidom.parse(str(PUBLIC / "favicon.svg"))


def test_the_page_references_an_icon_that_exists():
    """Named relatively, because the site is reachable at the apex domain and under a
    project path; an absolute /favicon.svg would 404 on the latter."""
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<link[^>]*rel="icon"[^>]*href="([^"]+)"', html)
    assert match, "index.html declares no icon"
    href = match.group(1)
    assert href.startswith("./"), href
    assert (PUBLIC / href[2:]).exists(), href
