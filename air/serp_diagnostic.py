from __future__ import annotations

import argparse
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from air.collector import AI_SELECTORS


DEFAULT_QUERIES = [
    "best travel credit card",
    "apply for travel credit card",
    "best rewards credit card",
    "best cashback credit card",
    "credit card offers",
    "best credit card signup bonus",
    "premium travel credit card",
    "airline credit card",
    "hotel credit card",
    "credit card with lounge access",
    "compare travel credit cards",
    "best business credit card",
    "best credit card for international travel",
    "no annual fee credit card",
    "best balance transfer credit card",
    "best credit card for groceries",
    "best dining credit card",
    "American Express credit card",
    "Chase Sapphire Preferred",
    "Capital One Venture X",
]


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:80] or "query"


def is_google_block(page):
    body = page.locator("body").inner_text(timeout=10000).lower()
    return "unusual traffic" in body or "captcha" in page.url.lower()


def wait_for_manual_verification(page, timeout):
    if not is_google_block(page) or timeout <= 0:
        return
    print(
        f"  Google verification is open. Complete it within {timeout} seconds; "
        "diagnostics will resume automatically.",
        flush=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if not is_google_block(page):
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            return


def inspect_dom(page):
    return page.evaluate(
        r"""
        (aiSelectors) => {
          const clean = value => (value || '').replace(/\s+/g, ' ').trim();
          const visible = el => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          const info = (el, selectorHint = '') => {
            const rect = el.getBoundingClientRect();
            const attributes = Object.fromEntries(Array.from(el.attributes || [])
              .filter(attribute => attribute.name === 'id' || attribute.name === 'class' ||
                attribute.name === 'role' || attribute.name === 'href' ||
                attribute.name === 'jsname' || attribute.name.startsWith('aria-') ||
                attribute.name.startsWith('data-'))
              .map(attribute => [attribute.name, attribute.value]));
            return {
              selector_hint: selectorHint,
              tag: el.tagName.toLowerCase(),
              id: el.id || null,
              classes: Array.from(el.classList || []),
              jsname: el.getAttribute('jsname'),
              data_attrid: el.getAttribute('data-attrid'),
              data_hveid: el.getAttribute('data-hveid'),
              role: el.getAttribute('role'),
              aria_label: el.getAttribute('aria-label'),
              y: Math.round(rect.top + scrollY),
              x: Math.round(rect.left + scrollX),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
              attributes,
              text: clean(el.innerText).slice(0, 2000),
              outer_html: el.outerHTML.slice(0, 50000),
            };
          };
          const unique = elements => Array.from(new Set(elements)).filter(visible);
          const aioNodes = unique(aiSelectors.flatMap(selector => Array.from(document.querySelectorAll(selector))));
          const primaryAio = aioNodes[0] || null;
          const insideAio = el => aioNodes.some(aio => aio.contains(el));
          const exactAdLabel = el => /^(Sponsored|Ad)$/i.test(clean(el.innerText));
          const exactAdAria = el => /^(Sponsored|Ad)$/i.test(clean(el.getAttribute('aria-label')));
          const allAdLabels = unique(Array.from(document.querySelectorAll('span, div, [aria-label]'))
            .filter(el => exactAdLabel(el) || exactAdAria(el)));
          const adContainer = label =>
            label.closest('[data-text-ad], [data-hveid], li, article') ||
            label.closest('div[role="listitem"]') || label.parentElement;;
          const sponsoredContainers = unique([
            ...document.querySelectorAll('#tads, #tadsb, [data-text-ad]'),
            ...allAdLabels.filter(label => !insideAio(label)).map(adContainer).filter(Boolean),
          ]);
          const aioAdContainers = unique(
            allAdLabels.filter(insideAio).map(adContainer).filter(Boolean)
          );
          const relationToAio = el => {
            if (!primaryAio) return 'no_aio';
            if (primaryAio.contains(el)) return 'inside';
            const aioRect = primaryAio.getBoundingClientRect();
            const rect = el.getBoundingClientRect();
            const verticalOverlap = rect.bottom > aioRect.top && rect.top < aioRect.bottom;
            if (verticalOverlap && rect.left >= aioRect.right - 10) return 'right';
            if (rect.top >= aioRect.bottom - 10 && rect.top - aioRect.bottom <= 800) return 'below';
            if (verticalOverlap) return 'alongside';
            return 'other';
          };
          const nearAio = el => ['inside', 'right', 'below', 'alongside'].includes(relationToAio(el));
          const aioSponsorshipSignals = allAdLabels.filter(nearAio);
          const semanticCardNodes = primaryAio ? unique(Array.from(document.querySelectorAll(
            '[role="list"], [role="listitem"], [aria-roledescription*="carousel" i], article, li'
          )).filter(nearAio)) : [];
          const aioLinkedCards = primaryAio ? unique(Array.from(document.querySelectorAll('a[href]'))
            .filter(anchor => nearAio(anchor))
            .map(anchor => anchor.closest('[role="listitem"], article, li, [data-hveid]') || anchor)
            .filter(Boolean)) : [];
          const resultAnchors = unique(Array.from(document.querySelectorAll('#search a:has(h3)')));
          const organicAnchors = resultAnchors.filter(anchor =>
            !insideAio(anchor) &&
            !anchor.closest('#tads, #tadsb, [data-text-ad]') &&
            !allAdLabels.some(label => {
              const container = adContainer(label);
              return container && container.contains(anchor);
            })
          );
          const resultContainer = anchor =>
            anchor.closest('div.MjjYud, [data-snhf], [data-hveid], article, li') ||
            anchor.closest('div');
          const organicContainers = unique(organicAnchors.map(resultContainer).filter(Boolean));
          const links = anchor => ({
            headline: clean(anchor.querySelector('h3, [role="heading"]')?.innerText || anchor.innerText),
            raw_href: anchor.getAttribute('href'),
            resolved_href: anchor.href,
            y: Math.round(anchor.getBoundingClientRect().top + scrollY),
          });
          const major = [
            ...sponsoredContainers.map(el => ({type: 'sponsored', ...info(el)})),
            ...aioNodes.map(el => ({type: 'ai_overview', ...info(el)})),
            ...organicContainers.map(el => ({type: 'organic', ...info(el)})),
          ].sort((a, b) => a.y - b.y);
          return {
            page: {url: location.href, title: document.title, captured_at: new Date().toISOString()},
            matched_ai_selectors: aiSelectors.filter(selector => document.querySelector(selector)),
            ai_overview: aioNodes.map(el => info(el)),
            organic_containers: organicContainers.slice(0, 10).map(el => info(el)),
            sponsored_containers: sponsoredContainers.slice(0, 10).map(el => info(el)),
            aio_ad_containers: aioAdContainers.slice(0, 10).map(el => info(el)),
            aio_sponsorship_signals: aioSponsorshipSignals.slice(0, 50).map(el => ({
              ...info(el),
              label_text: exactAdLabel(el) ? clean(el.innerText) : clean(el.getAttribute('aria-label')),
              dom_relationship: insideAio(el) ? 'inside' : 'outside',
              visual_relationship: relationToAio(el),
              parent: el.parentElement ? info(el.parentElement) : null,
              closest_link: el.closest('a[href]') ? links(el.closest('a[href]')) : null,
              container: adContainer(el) ? info(adContainer(el)) : null,
            })),
            aio_semantic_card_structures: semanticCardNodes.slice(0, 50).map(el => ({
              ...info(el), visual_relationship: relationToAio(el),
            })),
            aio_linked_card_structures: aioLinkedCards.slice(0, 50).map(el => ({
              ...info(el), visual_relationship: relationToAio(el),
              explicit_sponsorship: allAdLabels.some(label => el.contains(label)),
            })),
            organic_links: organicAnchors.slice(0, 20).map(links),
            sponsored_links: unique(sponsoredContainers.flatMap(el => Array.from(el.querySelectorAll('a[href]')))).slice(0, 20).map(links),
            aio_links: unique(aioNodes.flatMap(el => Array.from(el.querySelectorAll('a[href]')))).slice(0, 30).map(links),
            sponsored_labels: allAdLabels.slice(0, 50).map(el => ({
              text: clean(el.innerText),
              aria_label: el.getAttribute('aria-label'),
              inside_ai_overview: insideAio(el),
              y: Math.round(el.getBoundingClientRect().top + scrollY),
              outer_html: el.outerHTML.slice(0, 5000),
            })),
            vertical_order: major.slice(0, 30).map(({outer_html, text, ...entry}) => ({
              ...entry,
              text: text.slice(0, 300),
            })),
          };
        }
        """,
        list(AI_SELECTORS),
    )


def run(
    queries,
    output_dir,
    headless=False,
    captcha_timeout=300,
    delay_min=10,
    delay_max=15,
):
    root = Path(output_dir).expanduser().resolve()
    run_dir = root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    profile_dir = root / ".browser-profile"

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for index, query in enumerate(queries, start=1):
                slug = f"{index:02d}_{slugify(query)}"
                print(f"Inspecting {index}/{len(queries)}: {query}", flush=True)
                page.goto(
                    f"https://www.google.com/search?q={quote_plus(query)}&hl=en&gl=us",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                wait_for_manual_verification(page, 0 if headless else captcha_timeout)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(3000)

                (run_dir / f"{slug}.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=run_dir / f"{slug}.png", full_page=True)
                summary = {"query": query, "google_block_detected": is_google_block(page)}
                summary.update(inspect_dom(page))
                (run_dir / f"{slug}.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  Saved HTML, screenshot, and JSON for {slug}", flush=True)
                if index < len(queries) and delay_max:
                    delay = random.uniform(max(0, delay_min), max(delay_min, delay_max))
                    print(f"  Waiting {int(round(delay))} seconds before the next query...", flush=True)
                    page.wait_for_timeout(delay * 1000)
        finally:
            context.close()

    print(f"Diagnostic complete: {run_dir}", flush=True)
    return run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture Google SERP DOM evidence for AMEX AIR.")
    parser.add_argument("--output-dir", default="debug/serp_inspection")
    parser.add_argument("--query", action="append", dest="queries", help="Query to inspect; repeat for multiple queries.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--captcha-timeout-seconds", type=int, default=300)
    parser.add_argument("--delay-min-seconds", type=float, default=10)
    parser.add_argument("--delay-max-seconds", type=float, default=15)
    args = parser.parse_args(argv)
    run(
        args.queries or DEFAULT_QUERIES,
        args.output_dir,
        args.headless,
        args.captcha_timeout_seconds,
        args.delay_min_seconds,
        args.delay_max_seconds,
    )


if __name__ == "__main__":
    main()
