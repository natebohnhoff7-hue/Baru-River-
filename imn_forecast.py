#!/usr/bin/env python3
"""
imn_forecast.py — scrapes IMN's regional forecast table and stores the
Pacifico Sur row (the Baru's region) plus the daily general comment.

This is a SCRAPE, not a feed. It will break when IMN redesigns the page.
It is built to fail soft: on any parse failure it writes nothing, logs a
warning, and exits 0 so it never takes the alert job down with it. The
site hides the forecast strip when the row is stale.

Env:
    SUPABASE_URL
    SUPABASE_ANON_KEY
"""

import datetime
import json
import os
import re
import sys
import urllib.request

URL = "https://www.imn.ac.cr/reporte-pronostico-regional"
REGION = "Pac\u00edfico Sur"
TIMEOUT = 30
UA = "elbaru-imn-bot/1.0 (+https://elbaru.com)"

# IMN uses a small, fixed vocabulary of sky states — safe to map directly
CONDITIONS = {
    "despejado": "clear",
    "soleado": "sunny",
    "poca nubosidad": "few clouds",
    "pocas nubes": "few clouds",
    "parcialmente nublado": "partly cloudy",
    "mayormente nublado": "mostly cloudy",
    "nublado": "cloudy",
    "lluvia": "rain",
    "lluvias": "rain",
    "aguaceros": "downpours",
    "chubascos": "showers",
    "tormenta": "thunderstorms",
    "ventoso": "windy",
    "neblina": "mist",
    "niebla": "fog",
}


def to_english(es_text):
    """Map IMN's phrase to English by matching known fragments, longest first."""
    low = es_text.lower()
    hits = []
    for es in sorted(CONDITIONS, key=len, reverse=True):
        if es in low and CONDITIONS[es] not in hits:
            if any(es in other and es != other for other in CONDITIONS if other in low):
                continue
            hits.append(CONDITIONS[es])
    return ", ".join(hits) if hits else es_text


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(html):
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&aacute;", "\u00e1")
                .replace("&eacute;", "\u00e9").replace("&iacute;", "\u00ed")
                .replace("&oacute;", "\u00f3").replace("&uacute;", "\u00fa")
                .replace("&ntilde;", "\u00f1").replace("&amp;", "&"))
    return re.sub(r"\s+", " ", html).strip()


def parse(html):
    out = {"id": 1}

    m = re.search(r"V\u00e1lido para:\s*([^<\n]{5,60})", html)
    if m:
        out["valid_for"] = strip_tags(m.group(1))

    m = re.search(
        r"Comentario\s*General\s*</[^>]+>\s*(.*?)(?:<h\d|Efem\u00e9rides)",
        html, re.S | re.I)
    if m:
        c = strip_tags(m.group(1))
        if 40 < len(c) < 1500:
            out["comment_es"] = c

    # the Pacifico Sur table row
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    target = None
    for r in rows:
        if REGION.lower() in strip_tags(r).lower()[:40]:
            target = r
            break
    if target is None:
        return None

    cells = re.findall(r"<td[^>]*>(.*?)</td>", target, re.S | re.I)
    if len(cells) < 5:
        return None

    # each period cell carries the condition in the icon's title/alt attribute
    periods = ["madrugada", "manana", "tarde", "noche"]
    for i, key in enumerate(periods, start=1):
        cell = cells[i]
        t = re.search(r'title="([^"]{3,80})"', cell) or \
            re.search(r'alt="([^"]{3,80})"', cell)
        text = strip_tags(t.group(1)) if t else strip_tags(cell)
        text = re.sub(r"\s*\.\s*$", "", text).strip()
        if not text:
            return None
        out[key + "_es"] = text
        out[key + "_en"] = to_english(text)

    return out


def upsert(row, base_url, key):
    body = json.dumps([row], ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/rest/v1/imn_forecast?on_conflict=id",
        data=body, method="POST",
        headers={"apikey": key, "Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status


def main():
    base_url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not base_url or not key:
        print("::error::SUPABASE_URL / SUPABASE_ANON_KEY not set", file=sys.stderr)
        return 1

    try:
        html = get(URL)
    except Exception as e:
        print("::warning::forecast fetch failed: %s" % e)
        return 0                      # never break the alert job

    row = None
    try:
        row = parse(html)
    except Exception as e:
        print("::warning::forecast parse raised: %s" % e)

    if not row:
        print("::error::could not find the %s row \u2014 IMN likely changed "
              "the page. Forecast NOT updated; the site will hide the strip "
              "once the last stamp ages out." % REGION, file=sys.stderr)
        return 1

    # The site hides the forecast strip when this stamp is missing or older
    # than its freshness window. Only a genuinely fresh parse gets stamped.
    row["updated_at"] = (datetime.datetime.now(datetime.timezone.utc)
                         .replace(microsecond=0).isoformat())

    try:
        status = upsert(row, base_url, key)
    except Exception as e:
        print("::warning::forecast upsert failed: %s" % e)
        return 0

    print("ok http=%s  valid: %s" % (status, row.get("valid_for", "?")))
    print("  stamped: %s" % row["updated_at"])
    for k in ("madrugada", "manana", "tarde", "noche"):
        print("  %-10s %-34s %s" % (k, row.get(k + "_es", ""), row.get(k + "_en", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
