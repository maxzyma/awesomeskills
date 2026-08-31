#!/usr/bin/env python3
"""Shared index access for the awesomeskills finder scripts.

Standard library only. Honors HTTPS_PROXY for remote URLs.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

HOSTED_DEFAULT = "https://raw.githubusercontent.com/maxzyma/awesomeskills/main/registry/index.json"
LOCAL_FALLBACK = Path(__file__).resolve().parents[3] / "registry" / "index.json"

ENV_VAR = "AWESOMESKILLS_INDEX_URL"
# The variable shipped misspelled in 0.1. Still honored so existing setups keep working.
LEGACY_ENV_VAR = "AWSOMESKILLS_INDEX_URL"

RAW_BASE = "https://raw.githubusercontent.com"


def resolve_index_url(cli_url: str | None) -> str:
    return (
        cli_url
        or os.environ.get(ENV_VAR)
        or os.environ.get(LEGACY_ENV_VAR)
        or HOSTED_DEFAULT
    )


def fetch_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "awesomeskills"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def load_index(url: str, allow_local_fallback: bool = True) -> tuple[dict, str]:
    """Load the index, returning it with the source actually used.

    The hosted index is authoritative. The local repo copy is a genuine fallback for
    offline use -- an earlier version listed it in the resolution chain behind a constant
    that is never empty, so the branch could not be reached and any network failure was
    fatal.
    """
    if not url.startswith(("http://", "https://")):
        return json.loads(Path(url).read_text(encoding="utf-8")), url
    try:
        return json.loads(fetch_text(url)), url
    except Exception as error:  # noqa: BLE001 — any transport/parse failure means fall back
        if not (allow_local_fallback and LOCAL_FALLBACK.is_file()):
            raise
        payload = json.loads(LOCAL_FALLBACK.read_text(encoding="utf-8"))
        return payload, f"{LOCAL_FALLBACK} (offline fallback after: {error})"


def raw_file_url(repo: str, ref: str, path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    return f"{RAW_BASE}/{repo}/{urllib.parse.quote(ref, safe='')}/{quoted}"
