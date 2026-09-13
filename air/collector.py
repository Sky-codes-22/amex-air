from __future__ import annotations

import json
import os
import random
import time
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.sync_api import sync_playwright

from air.ad_ranking import analyze_sponsored_ads
from air.diagnostics import explain
from air.screenshots import save_qc_screenshot


AI_SELECTORS = ("div[jsname='KFl8ub']", "[data-attrid*='AIOverview']")


def _is_google_owned(url):
    if str(url or "").startswith("/"):
        return True
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "google.com" or hostname.startswith("google.") or ".google." in hostname


def _serp_snapshot(page):
    return page.evaluate(
        r"""
        (aiSelectors) => {
          const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
          const visible = element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
              style.display !== 'none' && style.visibility !== 'hidden';
          };
          const aio = aiSelectors
            .flatMap(selector => Array.from(document.querySelectorAll(selector)))
            .find(visible);
          const insideAio = element => Boolean(aio && aio.contains(element));
          const exactSponsorLabel = element =>
            visible(element) && /^(sponsored|ad)$/i.test(normalize(element.innerText));
          const sponsorLabels = Array.from(document.querySelectorAll('span, div'))
            .filter(exactSponsorLabel);
          const hasAssociatedSponsorLabel = anchor => {
            for (let node = anchor; node && node.id !== 'search'; node = node.parentElement) {
              const headings = node.querySelectorAll('a:has(h3), a:has([role="heading"])');
              if (headings.length > 1) continue;
              if (sponsorLabels.some(label => node.contains(label))) return true;
            }
            return false;
          };
          const selectors = [
            '#search a:has(h3)',
            '#tads a:has(h3)',
            '#tads a:has([role="heading"])',
            '#tadsb a:has(h3)',
            '#tadsb a:has([role="heading"])',
            '[data-text-ad] a:has(h3)',
            '[data-text-ad] a:has([role="heading"])',
          ].join(', ');
          const mappedCandidates = Array.from(document.querySelectorAll(selectors)).map((anchor, domOrder) => {
            if (!visible(anchor) || insideAio(anchor)) return null;
            const headlineNode = anchor.querySelector('h3, [role="heading"]');
            const headline = normalize(headlineNode?.innerText);
            if (!headline || !anchor.href) return null;
            const sponsored = Boolean(
              anchor.closest('#tads, #tadsb, [data-text-ad]') ||
              hasAssociatedSponsorLabel(anchor)
            );
            const organic = Boolean(anchor.closest('#search')) && !sponsored;
            if (!organic && !sponsored) return null;
            const rect = anchor.getBoundingClientRect();
            return {
              headline,
              raw_url: anchor.href,
              sponsored,
              y: Math.round(rect.top + scrollY),
              dom_order: domOrder,
              element: anchor,
            };
          }).filter(Boolean).sort((left, right) =>
            left.y - right.y || left.dom_order - right.dom_order
          );
          const seenResults = new Set();
          const candidates = mappedCandidates.filter(candidate => {
            const key = `${candidate.sponsored}|${candidate.headline.toLocaleLowerCase()}|${candidate.raw_url}`;
            if (seenResults.has(key)) return false;
            seenResults.add(key);
            return true;
          });
          const majorBlocks = candidates.map(candidate => ({
            type: candidate.sponsored ? 'Sponsored' : 'Organic',
            headline: candidate.headline,
            raw_url: candidate.raw_url,
            sponsored: candidate.sponsored,
            y: candidate.y,
            element: candidate.element,
          }));
          if (aio) {
            const rect = aio.getBoundingClientRect();
            majorBlocks.push({
              type: 'AI Overview',
              y: Math.round(rect.top + scrollY),
              element: aio,
            });
          }
          const aioAds = [];
          if (aio) {
            const aioRect = aio.getBoundingClientRect();
            const midpoint = aioRect.left + aioRect.width * 0.52;
            const seenAioAds = new Set();
            for (const anchor of document.querySelectorAll('a[href]')) {
              if (!visible(anchor)) continue;
              const anchorRect = anchor.getBoundingClientRect();
              const overlapsAio = anchorRect.bottom > aioRect.top && anchorRect.top < aioRect.bottom;
              if (!aio.contains(anchor) && !(overlapsAio && anchorRect.left >= midpoint)) continue;

              let card = anchor;
              let explicitLabel = '';
              for (let node = anchor; node && node !== aio.parentElement; node = node.parentElement) {
                const label = Array.from(node.querySelectorAll('span, div'))
                  .find(exactSponsorLabel);
                if (label) explicitLabel = normalize(label.innerText);
                const rect = node.getBoundingClientRect();
                if (rect.width >= 160 && rect.height >= 35 && rect.height <= aioRect.height) {
                  card = node;
                  break;
                }
                if (node === aio) break;
              }
              const cardRect = card.getBoundingClientRect();
              const isRightCard = cardRect.left >= midpoint && cardRect.width >= 160 && cardRect.height >= 35;
              if (!explicitLabel && !isRightCard) continue;

              const headlineNode = card.querySelector('h3, [role="heading"]');
              const cardText = normalize(card.innerText);
              const headline = normalize(headlineNode?.innerText) || cardText.slice(0, 300);
              if (!headline) continue;
              const rawUrl = anchor.href;
              const key = `${rawUrl}|${headline.toLocaleLowerCase()}`;
              if (seenAioAds.has(key)) continue;
              seenAioAds.add(key);
              let position = 'inside';
              if (cardRect.left >= midpoint) position = 'right';
              else if (cardRect.top >= aioRect.bottom - 5) position = 'below';
              aioAds.push({
                headline,
                url: rawUrl,
                raw_url: rawUrl,
                position,
                label: explicitLabel || 'AIO side card',
              });
            }
            const sideCandidates = Array.from(document.querySelectorAll('[data-hveid]'))
              .filter(node => {
                if (!visible(node) || !node.querySelector('a[href]')) return false;
                const rect = node.getBoundingClientRect();
                const overlaps = rect.bottom > aioRect.top && rect.top < aioRect.bottom;
                return overlaps && rect.left >= aioRect.right + 10 &&
                  rect.width >= 240 && rect.height >= 60 && rect.height <= 180;
              });
            const sideCards = sideCandidates.filter(node =>
              !sideCandidates.some(parent => parent !== node && parent.contains(node))
            );
            for (const card of sideCards) {
              const anchors = Array.from(card.querySelectorAll('a[href]')).filter(visible);
              const anchor = anchors.find(link => normalize(link.innerText)) || anchors[0];
              if (!anchor) continue;
              const lines = (card.innerText || '').split(/\n+/).map(normalize).filter(Boolean);
              const heading = normalize(card.querySelector('h3, [role="heading"]')?.innerText);
              const headline = heading || lines[1] || lines[0] || '';
              if (!headline) continue;
              const rawUrl = anchor.href;
              const cardRect = card.getBoundingClientRect();
              const duplicate = aioAds.some(item =>
                item.raw_url === rawUrl || item.headline.toLocaleLowerCase() === headline.toLocaleLowerCase()
              );
              if (duplicate) continue;
              aioAds.push({
                headline,
                url: rawUrl,
                raw_url: rawUrl,
                position: 'right',
                label: 'AIO side card',
              });
            }
          }
          majorBlocks.sort((left, right) => {
            if (left.y !== right.y) return left.y - right.y;
            if (left.element === right.element) return 0;
            return left.element.compareDocumentPosition(right.element) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
          });
          return {
            blue_links: candidates.map(({element, ...candidate}) => candidate),
            major_blocks: majorBlocks.map(({element, ...block}) => block),
            aio_present: Boolean(aio),
            aio_ads: aioAds,
          };
        }
        """,
        list(AI_SELECTORS),
    )


def _blue_link_candidates(page):
    return _serp_snapshot(page)["blue_links"]


def _serp_position(page):
    blocks = _serp_snapshot(page)["major_blocks"]
    first = blocks[0]["type"] if blocks else ""
    aio_index = next(
        (index for index, block in enumerate(blocks) if block["type"] == "AI Overview"),
        None,
    )
    return {
        "serp_first_element": first,
        "ai_overview_position": aio_index + 1 if aio_index is not None else None,
        "ai_overview_on_top": aio_index == 0 if aio_index is not None else None,
    }


def _position_from_structure(structure):
    first = structure[0]["type"] if structure else ""
    aio_index = next(
        (index for index, block in enumerate(structure) if block["type"] == "AI Overview"),
        None,
    )
    return {
        "serp_first_element": first,
        "ai_overview_position": aio_index + 1 if aio_index is not None else None,
        "ai_overview_on_top": aio_index == 0 if aio_index is not None else None,
    }


class DestinationURLResolver:
    def __init__(self, context, cache=None):
        self.context = context
        self.cache = cache if cache is not None else {}
        self.page = None
        self.navigation_count = 0
        self.cache_hits = 0

    def _page(self):
        is_closed = getattr(self.page, "is_closed", lambda: False)
        if self.page is None or is_closed():
            self.page = self.context.new_page()
        return self.page

    def resolve(self, raw_url, timeout=10000):
        raw_url = str(raw_url or "").strip()
        if not _is_google_owned(raw_url):
            return raw_url
        if raw_url in self.cache:
            self.cache_hits += 1
            return self.cache[raw_url]

        destination = raw_url
        deadline = time.monotonic() + timeout / 1000
        try:
            page = self._page()
            self.navigation_count += 1
            wrapper_url = urljoin("https://www.google.com", raw_url)
            try:
                page.goto(wrapper_url, wait_until="commit", timeout=timeout)
            except Exception:
                pass
            while time.monotonic() < deadline:
                current_url = page.url
                if current_url.startswith(("http://", "https://")) and not _is_google_owned(current_url):
                    destination = current_url
                    break
                page.wait_for_timeout(100)
        except Exception:
            pass
        self.cache[raw_url] = destination
        return destination

    def close(self):
        if self.page is not None:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None


def _resolve_destination(context, raw_url, timeout=10000):
    resolver = DestinationURLResolver(context)
    try:
        return resolver.resolve(raw_url, timeout=timeout)
    finally:
        resolver.close()


def _structure_from_snapshot(snapshot):
    blocks = snapshot["major_blocks"]
    structure = []
    for block in blocks:
        entry = {"rank": len(structure) + 1, "type": block["type"]}
        if block["type"] != "AI Overview":
            headline = str(block.get("headline", "")).strip()
            raw_url = str(block.get("raw_url", "")).strip()
            if not headline or not raw_url:
                continue
            entry.update({
                "headline": headline,
                "url": raw_url,
                "raw_url": raw_url,
                "sponsored": bool(block.get("sponsored", False)),
            })
        structure.append(entry)
    for rank, entry in enumerate(structure, start=1):
        entry["rank"] = rank
    return structure


def _resolve_structure_urls(structure, resolver, limit=None):
    result_count = 0
    for entry in structure:
        if entry["type"] == "AI Overview":
            continue
        result_count += 1
        if limit is None or result_count <= limit:
            entry["url"] = resolver.resolve(entry["raw_url"])
    return structure


def _serp_structure(page, context, resolver=None, resolve_limit=None):
    owned_resolver = resolver is None
    resolver = resolver or DestinationURLResolver(context)
    try:
        structure = _structure_from_snapshot(_serp_snapshot(page))
        return _resolve_structure_urls(structure, resolver, resolve_limit)
    finally:
        if owned_resolver:
            resolver.close()


def _top_blue_links_from_structure(structure, limit=3):
    return [
        {
            "headline": entry["headline"],
            "url": entry["url"],
            "raw_url": entry["raw_url"],
            "sponsored": entry["sponsored"],
        }
        for entry in structure
        if entry["type"] != "AI Overview"
    ][:limit]


def _top_blue_links(page, context, limit=3):
    return _top_blue_links_from_structure(_serp_structure(page, context), limit)


class GoogleAIOverviewCollector:
    def __init__(
        self,
        headless=None,
        use_cdp=True,
        user_data_dir=None,
        manual_captcha_timeout=0,
        executable_path=None,
        resolve_top_links_only=False,
        screenshot_delay_min=0,
        screenshot_delay_max=0,
    ):
        self.headless = (os.getenv("AIR_HEADLESS", "true").lower() != "false") if headless is None else headless
        self.use_cdp = use_cdp
        self.user_data_dir = user_data_dir
        self.manual_captcha_timeout = max(0, manual_captcha_timeout)
        self.executable_path = executable_path
        self.resolve_top_links_only = resolve_top_links_only
        self.screenshot_delay_min = max(0, float(screenshot_delay_min))
        self.screenshot_delay_max = max(
            self.screenshot_delay_min, float(screenshot_delay_max)
        )
        self._playwright = None
        self._browser = None
        self._context = None
        self._connected_browser = False
        self._resolution_cache = {}
        self._resolver = None

    def _resolver_for(self, context):
        if self._resolver is None or self._resolver.context is not context:
            if self._resolver is not None:
                self._resolver.close()
            self._resolver = DestinationURLResolver(context, self._resolution_cache)
        return self._resolver

    def _open_browser(self, playwright):
        cdp_url = os.getenv("AIR_CDP_URL", "").strip() if self.use_cdp else ""
        self._connected_browser = bool(cdp_url)
        if self._connected_browser:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError("The connected Chrome session has no browser context")
            return browser, browser.contexts[0]

        launch_options = {
            "headless": self.headless,
            "args": ["--disable-dev-shm-usage", "--no-sandbox"],
        }
        if self.executable_path:
            launch_options["executable_path"] = self.executable_path
        if self.user_data_dir:
            context = playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                **launch_options,
            )
            return context.browser, context

        browser = playwright.chromium.launch(**launch_options)
        return browser, browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        )

    def start(self):
        if self._context is not None:
            return self
        self._playwright = sync_playwright().start()
        try:
            self._browser, self._context = self._open_browser(self._playwright)
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

    def close(self):
        try:
            if self._resolver is not None:
                self._resolver.close()
            if self._browser is not None and not self._connected_browser:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._browser = None
            self._context = None
            self._playwright = None
            self._connected_browser = False
            self._resolver = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def _google_challenge(page, body_text=""):
        text = str(body_text or "").lower()
        url = str(page.url or "").lower()
        return any(
            signal in text
            for signal in (
                "unusual traffic",
                "verify you're not a robot",
                "verify you are not a robot",
            )
        ) or "captcha" in url or "/sorry/" in url

    def _collect_in_context(
        self,
        context,
        query,
        screenshot_path=None,
        captcha_screenshot_path=None,
    ):
        started = time.monotonic()
        stage = "starting browser"
        blue_links = []
        serp_order = []
        ad_analysis = analyze_sponsored_ads([])
        screenshot_attempted = False
        final_wait_applied = False
        google_blocked = False
        aio_ad_present = None
        aio_ads = []
        page = None
        google_serp_navigations = 0
        resolver = self._resolver_for(context)
        resolver_navigations_before = resolver.navigation_count
        cache_hits_before = resolver.cache_hits
        position = {
            "serp_first_element": "",
            "ai_overview_position": None,
            "ai_overview_on_top": None,
        }
        try:
            page = context.new_page()
            stage = "loading Google search results"
            google_serp_navigations += 1
            page.goto(
                f"https://www.google.com/search?q={quote_plus(query)}&hl=en&gl=us",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            body_text = page.locator("body").inner_text(timeout=10000)
            google_blocked = self._google_challenge(page, body_text)
            if google_blocked:
                raise RuntimeError("Google displayed unusual traffic or CAPTCHA verification")
            stage = "ai_overview"
            overview = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and overview is None:
                for selector in AI_SELECTORS:
                    candidate = page.locator(selector).first
                    if candidate.count() and candidate.is_visible():
                        overview = candidate
                        break
                if overview is None:
                    page.wait_for_timeout(500)
            if overview is not None and screenshot_path and self.screenshot_delay_max:
                delay = random.uniform(
                    self.screenshot_delay_min, self.screenshot_delay_max
                )
                print(
                    f"  Waiting {delay:.1f} seconds before final screenshot...",
                    flush=True,
                )
                page.wait_for_timeout(round(delay * 1000))
                final_wait_applied = True
            stage = "extracting SERP structure"
            try:
                snapshot = _serp_snapshot(page)
                if snapshot.get("aio_present"):
                    aio_ads = snapshot.get("aio_ads") or []
                    aio_ad_present = bool(aio_ads)
                serp_order = _structure_from_snapshot(snapshot)
                serp_order = _resolve_structure_urls(
                    serp_order,
                    resolver,
                    3 if self.resolve_top_links_only else None,
                )
                ad_analysis = analyze_sponsored_ads(serp_order)
                serp_order = ad_analysis["serp_structure"]
                blue_links = _top_blue_links_from_structure(serp_order)
                position = _position_from_structure(serp_order)
            except Exception:
                serp_order = []
                blue_links = []
            if overview is None:
                stage = "ai_overview"
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
                raise PlaywrightTimeoutError("AI Overview locator was not visible after 20 seconds")
            stage = "extracting AI Overview"
            response = overview.inner_text(timeout=10000).strip()
            if not response:
                raise RuntimeError("AI Overview was present but contained no readable text")
            links = overview.locator("a[href]").evaluate_all("els => els.map(a => ({url: a.href, title: (a.innerText || a.textContent || '').trim()}))")
            sources, seen = [], set()
            for link in links:
                url = str(link.get("url") or "").strip()
                if not url.startswith(("http://", "https://")) or "google.com" in urlparse(url).netloc.lower() or url in seen:
                    continue
                seen.add(url)
                sources.append({"url": url, "title": str(link.get("title") or "").strip(), "domain": urlparse(url).netloc.lower().removeprefix("www.")})
            document = {"query": query, "engine": "Google AI Overview", "blocks": [{"type": "intro", "text": response}], "sources": sources}
            return {
                "status": "Success", "response": response,
                "parsed_json": json.dumps(document, ensure_ascii=False, indent=2),
                "top_blue_links": json.dumps(blue_links, ensure_ascii=False),
                "serp_order_json": json.dumps(serp_order, ensure_ascii=False),
                "aio_ad_present": aio_ad_present,
                "aio_ad_count": len(aio_ads),
                "aio_ads_json": json.dumps(aio_ads, ensure_ascii=False),
                "sponsored_ads_json": json.dumps(ad_analysis["sponsored_ads"], ensure_ascii=False),
                "sponsored_ad_count": ad_analysis["sponsored_ad_count"],
                "amex_sponsored_ad_present": ad_analysis["amex_sponsored_ad_present"],
                "amex_sponsored_ad_rank": ad_analysis["amex_sponsored_ad_rank"],
                "amex_serp_rank": ad_analysis["amex_serp_rank"],
                "brands_in_sponsored_ads": ad_analysis["brands_in_sponsored_ads"],
                "amex_ad_competitive_position": ad_analysis["amex_ad_competitive_position"],
                **position,
                "google_blocked": False,
                "navigation_metrics": {
                    "google_serp_navigations": google_serp_navigations,
                    "external_url_resolution_navigations": resolver.navigation_count - resolver_navigations_before,
                    "resolution_cache_hits": resolver.cache_hits - cache_hits_before,
                },
                "execution_time": round(time.monotonic() - started, 2)
            }
        except Exception as error:
            return {
                "status": "Failed", "response": explain(error, stage), "parsed_json": "",
                "top_blue_links": json.dumps(blue_links, ensure_ascii=False),
                "serp_order_json": json.dumps(serp_order, ensure_ascii=False),
                "aio_ad_present": aio_ad_present,
                "aio_ad_count": len(aio_ads),
                "aio_ads_json": json.dumps(aio_ads, ensure_ascii=False),
                "sponsored_ads_json": json.dumps(ad_analysis["sponsored_ads"], ensure_ascii=False),
                "sponsored_ad_count": ad_analysis["sponsored_ad_count"],
                "amex_sponsored_ad_present": ad_analysis["amex_sponsored_ad_present"],
                "amex_sponsored_ad_rank": ad_analysis["amex_sponsored_ad_rank"],
                "amex_serp_rank": ad_analysis["amex_serp_rank"],
                "brands_in_sponsored_ads": ad_analysis["brands_in_sponsored_ads"],
                "amex_ad_competitive_position": ad_analysis["amex_ad_competitive_position"],
                **position,
                "google_blocked": google_blocked,
                "navigation_metrics": {
                    "google_serp_navigations": google_serp_navigations,
                    "external_url_resolution_navigations": resolver.navigation_count - resolver_navigations_before,
                    "resolution_cache_hits": resolver.cache_hits - cache_hits_before,
                },
                "execution_time": round(time.monotonic() - started, 2)
            }
        finally:
            if page is not None:
                if not final_wait_applied and (screenshot_path or (google_blocked and captcha_screenshot_path)) and self.screenshot_delay_max:
                    delay = random.uniform(
                        self.screenshot_delay_min, self.screenshot_delay_max
                    )
                    print(
                        f"  Waiting {delay:.1f} seconds before final screenshot...",
                        flush=True,
                    )
                    page.wait_for_timeout(round(delay * 1000))
                if screenshot_path:
                    screenshot_attempted = True
                    save_qc_screenshot(page, screenshot_path)
                if google_blocked and captcha_screenshot_path:
                    save_qc_screenshot(page, captcha_screenshot_path)
                try:
                    page.close()
                except Exception:
                    pass
            elif screenshot_path and not screenshot_attempted:
                print(
                    f"Warning: QC screenshot unavailable for {screenshot_path}: "
                    "the browser page could not be created.",
                    flush=True,
                )

    def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
        if self._context is not None:
            return self._collect_in_context(
                self._context,
                query,
                screenshot_path=screenshot_path,
                captcha_screenshot_path=captcha_screenshot_path,
            )

        with sync_playwright() as playwright:
            browser, context = self._open_browser(playwright)
            try:
                return self._collect_in_context(
                    context,
                    query,
                    screenshot_path=screenshot_path,
                    captcha_screenshot_path=captcha_screenshot_path,
                )
            finally:
                if self._resolver is not None:
                    self._resolver.close()
                    self._resolver = None
                self._resolution_cache.clear()
                if not self._connected_browser:
                    browser.close()
                self._connected_browser = False
