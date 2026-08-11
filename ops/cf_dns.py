#!/usr/bin/env python3
"""Cloudflare DNS automation for awesomeskills (and any CF-hosted zone).

Reusable agent capability: manage DNS records programmatically via the Cloudflare
API — token-based, NO IP allowlist (unlike Namecheap), so it works from any host
including behind a proxy, and can be run by an agent every time without re-whitelisting.

Credentials are read from the environment or a local, git-ignored file — they are
NEVER hardcoded (this is a public repo):
  CF_API_TOKEN + CF_ZONE from env, else from ~/.config/awesomeskills/cf.env

Usage:
  python ops/cf_dns.py list
  python ops/cf_dns.py upsert A @ 185.199.108.153        # idempotent
  python ops/cf_dns.py set-github-pages                  # apex A x4 -> GitHub Pages (DNS-only)

Standard library only. Honors HTTPS_PROXY.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CF_API = "https://api.cloudflare.com/client/v4"
GH_PAGES_IPS = ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"]
CREDS_FILE = Path.home() / ".config" / "awesomeskills" / "cf.env"


def load_creds() -> tuple[str, str]:
    token = os.environ.get("CF_API_TOKEN")
    zone = os.environ.get("CF_ZONE")
    if (not token or not zone) and CREDS_FILE.exists():
        for line in CREDS_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("CF_API_TOKEN=") and not token:
                token = line.split("=", 1)[1].strip()
            elif line.startswith("CF_ZONE=") and not zone:
                zone = line.split("=", 1)[1].strip()
    if not token or not zone:
        sys.exit(f"missing CF_API_TOKEN / CF_ZONE (set env or {CREDS_FILE})")
    return token, zone


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CF_API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def zone_id(token: str, zone: str) -> str:
    d = api("GET", f"/zones?name={zone}", token)
    result = d.get("result") or []
    if not result:
        sys.exit(f"zone not found: {zone} (errors: {d.get('errors')})")
    return result[0]["id"]


def list_records(token: str, zid: str) -> list[dict]:
    return api("GET", f"/zones/{zid}/dns_records?per_page=100", token).get("result") or []


def upsert(token: str, zid: str, rtype: str, name: str, content: str, proxied: bool = False) -> None:
    """Idempotent: no-op if an identical (type, name, content) record exists; else create.
    Never deletes other records (so multiple A records on the apex coexist safely)."""
    for rec in list_records(token, zid):
        if rec["type"] == rtype and rec["name"] == name and rec["content"] == content:
            print(f"  = {rtype} {name} {content} (already present)")
            return
    body = {"type": rtype, "name": name, "content": content, "ttl": 1, "proxied": proxied}
    d = api("POST", f"/zones/{zid}/dns_records", token, body)
    if d.get("success"):
        print(f"  + {rtype} {name} {content} created")
    else:
        print(f"  ! {rtype} {name} {content} FAILED: {d.get('errors')}")


def delete(token: str, zid: str, rtype: str, name: str, content: str) -> None:
    """Delete a record matching (type, name, content) exactly. No-op if absent."""
    for rec in list_records(token, zid):
        if rec["type"] == rtype and rec["name"] == name and rec["content"] == content:
            d = api("DELETE", f"/zones/{zid}/dns_records/{rec['id']}", token)
            print(f"  - {rtype} {name} {content} {'deleted' if d.get('success') else d.get('errors')}")
            return
    print(f"  ? {rtype} {name} {content} (not found)")


def main(argv: list[str]) -> int:
    token, zone = load_creds()
    zid = zone_id(token, zone)
    cmd = argv[0] if argv else "list"

    if cmd == "list":
        for r in sorted(list_records(token, zid), key=lambda x: (x["type"], x["name"])):
            print(f"  {r['type']:6} {r['name']:32} {r['content']:22} proxied={r.get('proxied')}")
    elif cmd == "upsert":
        rtype, name, content = argv[1], argv[2], argv[3]
        upsert(token, zid, rtype, zone if name == "@" else name, content)
    elif cmd == "set-github-pages":
        print(f"setting GitHub Pages apex A records on {zone} (DNS-only):")
        for ip in GH_PAGES_IPS:
            upsert(token, zid, "A", zone, ip, proxied=False)
    elif cmd == "delete":
        rtype, name, content = argv[1], argv[2], argv[3]
        delete(token, zid, rtype, zone if name == "@" else name, content)
    elif cmd == "unset-github-pages":
        print(f"removing GitHub Pages apex A records on {zone}:")
        for ip in GH_PAGES_IPS:
            delete(token, zid, "A", zone, ip)
    else:
        sys.exit(f"unknown command: {cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
