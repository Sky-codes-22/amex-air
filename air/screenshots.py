from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


MAX_SCREENSHOT_FILENAME = 180
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_screenshot_stem(prompt, max_filename=MAX_SCREENSHOT_FILENAME):
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(prompt))
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = "prompt"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    max_stem = max(1, max_filename - len(".png"))
    stem = stem[:max_stem].rstrip(" .") or "prompt"
    return stem


class ScreenshotRun:
    def __init__(self, output_dir, timestamp=None):
        screenshots_root = Path(output_dir).resolve() / "screenshots"
        screenshots_root.mkdir(parents=True, exist_ok=True)
        base_name = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = screenshots_root / base_name
        suffix = 2
        while run_dir.exists():
            run_dir = screenshots_root / f"{base_name}_{suffix}"
            suffix += 1
        run_dir.mkdir()
        self.directory = run_dir
        self._used_names = set()
        self._manifest = {}

    def path_for(self, prompt):
        base = sanitize_screenshot_stem(prompt)
        candidate = f"{base}.png"
        suffix = 2
        while candidate.casefold() in self._used_names:
            addition = f"_{suffix}"
            max_stem = MAX_SCREENSHOT_FILENAME - len(".png") - len(addition)
            shortened = base[:max_stem].rstrip(" .") or "prompt"
            candidate = f"{shortened}{addition}.png"
            suffix += 1
        self._used_names.add(candidate.casefold())
        return self.directory / candidate

    def captcha_path(self, timestamp=None):
        base = f"CAPTCHA_{timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')}"
        candidate = f"{base}.png"
        suffix = 2
        while candidate.casefold() in self._used_names or (self.directory / candidate).exists():
            candidate = f"{base}_{suffix}.png"
            suffix += 1
        self._used_names.add(candidate.casefold())
        return self.directory / candidate

    def record(self, screenshot_path, prompt):
        self._manifest[Path(screenshot_path).name] = str(prompt)
        manifest_path = self.directory / "screenshot_manifest.json"
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)


def save_qc_screenshot(page, screenshot_path):
    try:
        page.screenshot(path=str(screenshot_path), full_page=True, timeout=15000)
        return True
    except Exception as error:
        print(
            f"Warning: QC screenshot unavailable for {Path(screenshot_path).name}: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        return False
