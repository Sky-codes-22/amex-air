from __future__ import annotations

import re
from urllib.parse import urlparse

from air.brand_config import BRAND_DEFINITIONS, NON_ISSUER_PUBLISHER_DOMAINS, UNKNOWN_BRAND


def _hostname(url):
    try:
        return (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _domain_matches(hostname, configured_domain):
    domain = configured_domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


def identify_brand(result):
    hostname = _hostname(result.get("url"))
    for definition in BRAND_DEFINITIONS:
        if any(_domain_matches(hostname, domain) for domain in definition["domains"]):
            return definition["name"]

    if any(_domain_matches(hostname, domain) for domain in NON_ISSUER_PUBLISHER_DOMAINS):
        return UNKNOWN_BRAND

    searchable_text = " ".join(
        str(result.get(field) or "") for field in ("headline", "text", "visible_text")
    )
    for definition in BRAND_DEFINITIONS:
        for alias in definition["aliases"]:
            pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
            if re.search(pattern, searchable_text, re.IGNORECASE):
                return definition["name"]
    return UNKNOWN_BRAND


def ordinal(number):
    remainder = number % 100
    if 11 <= remainder <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def analyze_sponsored_ads(serp_structure):
    enriched = []
    for item in serp_structure:
        entry = dict(item)
        if entry.get("type") in {"Organic", "Sponsored"}:
            entry["brand"] = identify_brand(entry)
        enriched.append(entry)

    sponsored = [
        entry for entry in enriched
        if entry.get("type") == "Sponsored" and entry.get("sponsored") is True
    ]
    ads = []
    for ad_rank, entry in enumerate(sponsored, start=1):
        ads.append({
            "ad_rank": ad_rank,
            "serp_rank": entry["rank"],
            "brand": entry["brand"],
            "headline": entry.get("headline", ""),
            "url": entry.get("url", ""),
            "raw_url": entry.get("raw_url", ""),
        })

    first_amex = next((ad for ad in ads if ad["brand"] == "American Express"), None)
    recognized_brands = []
    for ad in ads:
        brand = ad["brand"]
        if brand != UNKNOWN_BRAND and brand not in recognized_brands:
            recognized_brands.append(brand)
    if not recognized_brands and ads:
        recognized_brands = [UNKNOWN_BRAND]

    if not ads:
        competitive_position = "No Sponsored Ads"
    elif first_amex is None:
        competitive_position = "No AMEX Ad"
    else:
        competitive_position = ordinal(first_amex["ad_rank"])

    return {
        "serp_structure": enriched,
        "sponsored_ads": ads,
        "sponsored_ad_count": len(ads),
        "amex_sponsored_ad_present": first_amex is not None,
        "amex_sponsored_ad_rank": first_amex["ad_rank"] if first_amex else None,
        "amex_serp_rank": first_amex["serp_rank"] if first_amex else None,
        "brands_in_sponsored_ads": " | ".join(recognized_brands),
        "amex_ad_competitive_position": competitive_position,
    }
