#!/usr/bin/env python3
"""
imn_alerts.py — pulls the IMN CAP alert feed, keeps the advisories that name
the Pacifico Sur / Baru catchment, and upserts them into Supabase.

Runs from GitHub Actions on a schedule. Stdlib only, no pip install.

Secrets come from the environment:
    SUPABASE_URL       e.g. https://xxxx.supabase.co
    SUPABASE_ANON_KEY

This drives the ticker only. It must never feed the flood score: IMN's
rainfall figures come from the same models as Open-Meteo, so scoring on
both would double-count the same rain.
"""

import json
import os
import re
import sys
import unicodedata
import urllib.request
import urllib.error
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

FEED_URL = "https://cap-sources.s3.amazonaws.com/cr-imn-es/rss.xml"
MAX_ITEMS = 25
TIMEOUT = 30
UA = "elbaru-imn-bot/1.0 (+https://elbaru.com)"


# ----------------------------------------------------------------- helpers

def strip_accents(s):
    """lowercase + drop accents so 'Pacifico' and 'Pacífico' both match"""
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


REGION_RX = re.compile(
    r"pacifico sur|pacifico central y sur|central y sur|zona sur"
    r"|vertiente del pacifico"
)

SEVERITY_RX = [
    ("HIGH", re.compile(r"inundacion|desbordamiento|deslizamiento|crecida|caudales")),
    ("MODERATE", re.compile(r"saturacion|aguacero|tormenta electrica|atencion especial")),
    ("LOW", re.compile(r"lluvia|precaucion")),
]

SOIL_RX = re.compile(r"saturaci[oó]n[^.]{0,45}?(\d{2,3})\s?%", re.IGNORECASE)


def is_relevant(normed):
    return bool(REGION_RX.search(normed))


def severity(normed):
    for label, rx in SEVERITY_RX:
        if rx.search(normed):
            return label
    return "INFO"


RAIN_RANGE = re.compile(
    r"acumulad\w*[^.]{0,70}?(\d{1,3})\s*(?:mm)?\s*(?:[-\u2013]|\sy\s|\sa\s)\s*(\d{1,3})\s*mm",
    re.IGNORECASE)
RAIN_PEAK = re.compile(
    r"m[a\u00e1]ximos?[^.]{0,50}?(\d{1,3})\s*(?:[-\u2013]\s*(\d{1,3}))?\s*mm",
    re.IGNORECASE)


def rain_figures(desc):
    """IMN states forecast accumulations — the most actionable number they give."""
    out = []
    r = RAIN_RANGE.search(desc)
    if r:
        out.append("rain %s\u2013%s mm" % (r.group(1), r.group(2)))
    p = RAIN_PEAK.search(desc)
    if p:
        out.append("peaks to %s mm" % (p.group(2) or p.group(1)))
    return ", ".join(out)


HAZARDS = [
    (re.compile(r"inundacion"), "flooding"),
    (re.compile(r"deslizamiento"), "landslides"),
    (re.compile(r"caudales|quebradas|rios pequenos"), "rising streams"),
    (re.compile(r"alcantarillado"), "storm drains backing up"),
    (re.compile(r"tormenta electrica"), "thunderstorms"),
    (re.compile(r"aguacero"), "heavy downpours"),
    (re.compile(r"rafagas"), "strong wind gusts"),
]


def summary_en(normed, severity, soil, desc=""):
    """Build a plain-English line from what we detected, rather than machine
    translating a safety message. Deterministic: no hallucination risk."""
    hz = []
    for rx, en in HAZARDS:
        if rx.search(normed) and en not in hz:
            hz.append(en)
    if not hz:
        hz = ["rain"]
    if len(hz) == 1:
        h = hz[0]
    elif len(hz) == 2:
        h = hz[0] + " and " + hz[1]
    else:
        h = ", ".join(hz[:-1]) + " and " + hz[-1]
    out = "IMN advisory \u2014 South Pacific watersheds: " + h + "."
    rain = rain_figures(desc)
    if rain:
        out += " Forecast " + rain + "."
    if soil is not None:
        out += " Soil saturation %d%%." % int(soil)
    if severity == "HIGH":
        out += " Do not cross moving water."
    return out


def section(desc, label):
    """Pull 'Label: ...' out of the description, up to the next blank line
    or the next section heading."""
    rx = re.compile(
        label + r":\s*(.+?)(?=\n\s*\n|Diagn[oó]stico:|Pron[oó]stico:|Advertencia:|$)",
        re.S | re.IGNORECASE,
    )
    m = rx.search(desc)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def parse_feed(xml_text):
    """RSS -> list of row dicts ready for Supabase."""
    root = ElementTree.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("no <channel> in feed")

    rows, seen = [], set()

    for item in channel.findall("item")[:MAX_ITEMS]:
        def txt(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        title, desc, guid = txt("title"), txt("description"), txt("guid")
        if not guid or not title:
            continue

        # IMN republishes the same advisory minutes apart — keep the newest
        fingerprint = (title, desc[:400])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        normed = strip_accents(desc + " " + title)

        try:
            published = parsedate_to_datetime(txt("pubDate")).isoformat()
        except Exception:
            published = None
        if not published:
            continue

        soil = SOIL_RX.search(desc)

        sev = severity(normed)
        soil_v = float(soil.group(1)) if soil else None

        rows.append({
            "summary_en": summary_en(normed, sev, soil_v, desc),
            "guid": guid,
            "published": published,
            "title": title,
            "severity": sev,
            "relevant": is_relevant(normed),
            "soil_pct": soil_v,
            "advertencia": section(desc, "Advertencia"),
            "diagnostico": section(desc, r"Diagn[oó]stico"),
            "pronostico": section(desc, r"Pron[oó]stico"),
            "link": txt("link"),
        })

    return rows


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def upsert(rows, base_url, key):
    url = base_url.rstrip("/") + "/rest/v1/imn_alerts?on_conflict=guid"
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status


# ----------------------------------------------------------------- main

def main():
    base_url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not base_url or not key:
        print("::error::SUPABASE_URL / SUPABASE_ANON_KEY not set", file=sys.stderr)
        return 1

    try:
        xml_text = get(FEED_URL)
    except Exception as e:
        print(f"::error::feed fetch failed: {e}", file=sys.stderr)
        return 1

    try:
        rows = parse_feed(xml_text)
    except Exception as e:
        print(f"::error::feed parse failed: {e}", file=sys.stderr)
        return 1

    if not rows:
        print("::warning::feed parsed but produced no rows")
        return 0

    try:
        status = upsert(rows, base_url, key)
    except urllib.error.HTTPError as e:
        print(f"::error::supabase {e.code}: {e.read()[:400]}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"::error::supabase upsert failed: {e}", file=sys.stderr)
        return 1

    relevant = sum(1 for r in rows if r["relevant"])
    newest = max(rows, key=lambda r: r["published"])
    print(f"ok  http={status}  {len(rows)} alerts, {relevant} relevant to the Baru")
    print(f"newest: [{newest['severity']}] {newest['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
