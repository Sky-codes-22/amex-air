from __future__ import annotations

import json
import os
import time
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright

from air.diagnostics import explain


AI_SELECTORS = ("div[jsname='KFl8ub']", "[data-attrid*='AIOverview']")


class GoogleAIOverviewCollector:
    def __init__(self, headless=None):
        self.headless = (os.getenv("AIR_HEADLESS", "true").lower() != "false") if headless is None else headless

    def collect(self, query):
        started = time.monotonic()
        stage = "starting browser"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
                context = browser.new_context(
                    locale="en-US",
                    timezone_id="America/New_York",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                )
                page = context.new_page()
                stage = "loading Google search results"
                page.goto(
                    f"https://www.google.com/search?q={quote_plus(query)}&hl=en&gl=us",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                body_text = page.locator("body").inner_text(timeout=10000)
                if "unusual traffic" in body_text.lower() or "captcha" in page.url.lower():
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
                if overview is None:
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
                browser.close()
                return {"status": "Success", "response": response, "parsed_json": json.dumps(document, ensure_ascii=False, indent=2), "execution_time": round(time.monotonic() - started, 2)}
        except Exception as error:
            return {"status": "Failed", "response": explain(error, stage), "parsed_json": "", "execution_time": round(time.monotonic() - started, 2)}
