#!/usr/bin/env python3
"""Shared index access for the awesomeskills finder scripts.

Standard library only. Honors HTTPS_PROXY for remote URLs.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The published endpoint rather than a raw path into the repo. The artifact used to be
# committed twice -- once under registry/ for this client, once under site/public/ for the
# site -- and naming the site is what lets the repo be rearranged without breaking installed
# copies of this skill.
HOSTED_DEFAULT = "https://awesomeskills.io/index.json"
LOCAL_FALLBACK = Path(__file__).resolve().parents[3] / "site" / "public" / "index.json"

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


def fetch_bytes(url: str, timeout: int = 30, attempts: int = 3) -> bytes:
    """Fetch a URL as raw bytes, retrying transient transport failures.

    Bytes rather than decoded text: a skill bundle can contain binaries, and decoding is
    both lossy for them and unnecessary for hashing.

    Concurrent fetches against raw.githubusercontent.com draw occasional connection
    resets. Without a retry those surface as verification failures, which is worse than
    being slow: a transient reset would read as "this file does not match".
    """
    request = urllib.request.Request(url, headers={"User-Agent": "awesomeskills"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise  # a real 404/403 is an answer, not a hiccup
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise OSError(f"exhausted retries for {url}")  # unreachable; keeps the return type honest


def fetch_text(url: str, timeout: int = 30, attempts: int = 3) -> str:
    return fetch_bytes(url, timeout, attempts).decode("utf-8")


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
