from __future__ import annotations

import re
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def explain(error, stage):
    raw = re.sub(r"\s+", " ", str(error or "")).strip()[:500]
    if isinstance(error, PlaywrightTimeoutError) and stage == "ai_overview":
        reason = "Google results loaded, but an AI Overview was not found within the allowed time. Google may not have generated one, it may be loading slowly, or the page structure may have changed."
    elif isinstance(error, PlaywrightTimeoutError):
        reason = f"The browser timed out while {stage.replace('_', ' ')}."
    elif "ERR_NAME_NOT_RESOLVED" in raw:
        reason = "DNS resolution failed while connecting to Google."
    elif any(value in raw for value in ("ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION", "ERR_NETWORK_CHANGED")):
        reason = "The browser could not maintain an internet connection to Google."
    elif "captcha" in raw.lower() or "unusual traffic" in raw.lower():
        reason = "Google displayed a CAPTCHA or unusual-traffic block."
    elif any(value in raw.lower() for value in ("browser has been closed", "target page", "page closed")):
        reason = "The hosted browser closed unexpectedly."
    elif isinstance(error, PlaywrightError):
        reason = f"Browser automation failed while {stage.replace('_', ' ')}."
    else:
        reason = f"An unexpected {type(error).__name__} occurred while {stage.replace('_', ' ')}."
    return f"Failure reason: {reason}" + (f" Technical detail: {raw}" if raw else "")
