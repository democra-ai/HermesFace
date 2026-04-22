#!/usr/bin/env python3
"""
DNS-over-HTTPS resolver for HF Spaces.

HF Spaces containers cannot resolve certain domains (e.g. api.telegram.org,
web.whatsapp.com) via the default DNS resolver. This script resolves key
domains using Cloudflare / Google DoH and writes results to a JSON file
plus /etc/hosts so both Python and Node.js processes benefit.

Usage: python3 dns-resolve.py [output-file]
"""

import json
import socket
import ssl
import sys
import urllib.request

DOH_ENDPOINTS = [
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
    "https://dns.google/resolve",
]

DOMAINS = [
    # Telegram Bot API
    "api.telegram.org",
    # Discord gateway / API
    "discord.com",
    "gateway.discord.gg",
    "cdn.discordapp.com",
    # Slack
    "slack.com",
    "api.slack.com",
    # WhatsApp / Baileys
    "web.whatsapp.com",
    "g.whatsapp.net",
    "mmg.whatsapp.net",
    "pps.whatsapp.net",
    "static.whatsapp.net",
    "media.fmed1-1.fna.whatsapp.net",
]


def resolve_via_doh(domain: str, endpoint: str, timeout: int = 10) -> list[str]:
    url = f"{endpoint}?name={domain}&type=A"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    ctx = ssl.create_default_context()
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    data = json.loads(resp.read().decode())
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]


def resolve_domain(domain: str) -> list[str]:
    for endpoint in DOH_ENDPOINTS:
        try:
            ips = resolve_via_doh(domain, endpoint)
            if ips:
                return ips
        except Exception:
            continue
    return []


def main() -> None:
    output_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dns-resolved.json"

    # If system DNS works for the most-commonly-blocked domain, DoH isn't needed.
    try:
        socket.getaddrinfo("api.telegram.org", 443, socket.AF_INET)
        print("[dns] System DNS works for api.telegram.org — DoH not needed")
        with open(output_file, "w") as f:
            json.dump({}, f)
        return
    except (socket.gaierror, OSError) as e:
        print(f"[dns] System DNS failed ({e}) — using DoH fallback")

    results = {}
    for domain in DOMAINS:
        ips = resolve_domain(domain)
        if ips:
            results[domain] = ips[0]
            print(f"[dns] {domain} -> {ips[0]}")
        else:
            print(f"[dns] WARNING: could not resolve {domain}")

    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    if results:
        try:
            with open("/etc/hosts", "a") as f:
                f.write("\n# === HermesFace DoH resolved domains ===\n")
                for domain, ip in results.items():
                    f.write(f"{ip} {domain}\n")
            print(f"[dns] Wrote {len(results)} entries to /etc/hosts")
        except PermissionError:
            print("[dns] WARNING: cannot write /etc/hosts (permission denied)")

    print(f"[dns] Resolved {len(results)}/{len(DOMAINS)} domains -> {output_file}")


if __name__ == "__main__":
    main()
