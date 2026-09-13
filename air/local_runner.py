from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from air.collector import GoogleAIOverviewCollector
from air.excel_output import write_results
from air.inputs import BATCH_SIZE, InputError, read_queries
from air.screenshots import ScreenshotRun


DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[1] / "debug" / "local_browser_profile"
SYSTEM_CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def chrome_executable_candidates(local_app_data=None):
    candidates = list(SYSTEM_CHROME_PATHS)
    local_root = local_app_data if local_app_data is not None else os.getenv("LOCALAPPDATA", "")
    if local_root:
        candidates.append(Path(local_root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return candidates


def find_chrome_executable(local_app_data=None):
    candidates = chrome_executable_candidates(local_app_data)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    checked = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(
        "Google Chrome could not be found. Install Google Chrome in one of these locations:\n"
        f"{checked}"
    )


def _number_setting(name, default, override, minimum=0, integer=False):
    value = os.getenv(name, str(default)) if override is None else override
    parsed = int(value) if integer else float(value)
    return max(minimum, parsed)


def _write_checkpoint(destination, run_timestamp, rows):
    def save(path, selected_rows):
        temporary = path.with_name(f".{path.stem}.checkpoint{path.suffix}")
        write_results(temporary, selected_rows)
        temporary.replace(path)

    combined_path = destination / f"amex_air_all_batches_{run_timestamp}.xlsx"
    save(combined_path, rows)
    for offset in range(0, len(rows), BATCH_SIZE):
        batch_number = offset // BATCH_SIZE + 1
        save(
            destination / f"amex_air_batch_{batch_number}_{run_timestamp}.xlsx",
            rows[offset:offset + BATCH_SIZE],
        )
    return combined_path


def _record_screenshot(screenshot_run, path, prompt, label=None):
    if path.is_file():
        screenshot_run.record(path, label or prompt)
        print(f"  Screenshot saved: {path.name}", flush=True)


def _add_navigation_metrics(total, result):
    metrics = result.get("navigation_metrics") or {}
    for key in total:
        total[key] += int(metrics.get(key, 0) or 0)


def run(
    prompts_path,
    output_dir=None,
    delay_min=None,
    delay_max=None,
    break_every=None,
    break_min=None,
    break_max=None,
    captcha_wait_seconds=60,
    profile_dir=None,
    chrome_executable=None,
):
    prompts_path = Path(prompts_path).expanduser().resolve()
    if not prompts_path.is_file():
        raise InputError(f"Prompt file was not found: {prompts_path}")

    queries = read_queries(prompts_path.name, prompts_path.read_bytes())
    chrome_path = (
        Path(chrome_executable).expanduser().resolve()
        if chrome_executable is not None
        else find_chrome_executable()
    )
    if not chrome_path.is_file():
        raise FileNotFoundError(f"Google Chrome executable was not found: {chrome_path}")

    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else prompts_path.parent / "output"
    )
    destination.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    screenshot_run = ScreenshotRun(destination)
    print(f"QC screenshots: {screenshot_run.directory}", flush=True)

    profile_path = Path(profile_dir or DEFAULT_PROFILE_DIR).expanduser().resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    print("Browser: Google Chrome", flush=True)
    print(f"Chrome executable: {chrome_path}", flush=True)
    print(f"Profile: {profile_path}", flush=True)
    collector = GoogleAIOverviewCollector(
        headless=False,
        use_cdp=False,
        user_data_dir=str(profile_path),
        manual_captcha_timeout=0,
        executable_path=str(chrome_path),
        resolve_top_links_only=True,
        screenshot_delay_min=5,
        screenshot_delay_max=10,
    )
    delay_min = _number_setting("AIR_LOCAL_MIN_DELAY", 25, delay_min)
    delay_max = max(delay_min, _number_setting("AIR_LOCAL_MAX_DELAY", 45, delay_max))
    break_every = _number_setting(
        "AIR_LOCAL_BREAK_EVERY", 10, break_every, integer=True
    )
    break_min = _number_setting("AIR_LOCAL_BREAK_MIN", 180, break_min)
    break_max = max(break_min, _number_setting("AIR_LOCAL_BREAK_MAX", 300, break_max))

    rows = []
    total = len(queries)
    combined_path = destination / f"amex_air_all_batches_{run_timestamp}.xlsx"
    stopped_for_captcha = False
    run_metrics = {
        "google_serp_navigations": 0,
        "external_url_resolution_navigations": 0,
        "resolution_cache_hits": 0,
    }
    collector.start()
    try:
        for index, query in enumerate(queries, start=1):
            print(f"Processing {index}/{total}: {query}", flush=True)
            screenshot_path = screenshot_run.path_for(query)
            captcha_path = screenshot_run.captcha_path()
            query_metrics = {key: 0 for key in run_metrics}
            result = collector.collect(
                query,
                screenshot_path=screenshot_path,
                captcha_screenshot_path=captcha_path,
            )
            _add_navigation_metrics(query_metrics, result)
            _record_screenshot(
                screenshot_run, captcha_path, query, f"CAPTCHA: {query} (attempt 1)"
            )

            if result.get("google_blocked"):
                print(f"CAPTCHA detected on prompt {index}.", flush=True)
                print("Waiting 60 seconds before retry...", flush=True)
                time.sleep(captcha_wait_seconds)
                print(f"Retrying prompt {index}...", flush=True)
                retry_captcha_path = screenshot_run.captcha_path()
                result = collector.collect(
                    query,
                    screenshot_path=screenshot_path,
                    captcha_screenshot_path=retry_captcha_path,
                )
                _add_navigation_metrics(query_metrics, result)
                _record_screenshot(
                    screenshot_run,
                    retry_captcha_path,
                    query,
                    f"CAPTCHA: {query} (attempt 2)",
                )
                if result.get("google_blocked"):
                    print("CAPTCHA detected again.", flush=True)
                    print("Stopping AMEX AIR run.", flush=True)
                    stopped_for_captcha = True

            _record_screenshot(screenshot_run, screenshot_path, query)
            for key, value in query_metrics.items():
                run_metrics[key] += value
            rows.append({"prompt": query, **result})
            combined_path = _write_checkpoint(destination, run_timestamp, rows)
            print(f"  {result['status']} ({result['execution_time']:.2f} sec)", flush=True)
            print(f"  Google page navigations: {query_metrics['google_serp_navigations']}", flush=True)
            print(
                "  External URL resolutions: "
                f"{query_metrics['external_url_resolution_navigations']}",
                flush=True,
            )
            print(f"  Cache hits: {query_metrics['resolution_cache_hits']}", flush=True)

            if stopped_for_captcha:
                print(f"Partial results saved to:\n{combined_path}", flush=True)
                break

            if index < total:
                if break_every and index % break_every == 0:
                    pause = random.uniform(break_min, break_max)
                    print(
                        f"  Taking a longer break for {int(round(pause))} seconds...",
                        flush=True,
                    )
                else:
                    pause = random.uniform(delay_min, delay_max)
                    print(
                        f"  Waiting {int(round(pause))} seconds before the next prompt...",
                        flush=True,
                    )
                if pause:
                    time.sleep(pause)
    finally:
        collector.close()

    label = "Stopped. Partial output" if stopped_for_captcha else "Complete. Combined output"
    print(f"{label}: {combined_path}", flush=True)
    print(f"QC screenshots: {screenshot_run.directory}", flush=True)
    print(f"Total prompts: {len(rows)}", flush=True)
    print(f"Google SERP navigations: {run_metrics['google_serp_navigations']}", flush=True)
    print(
        "External URL resolution navigations: "
        f"{run_metrics['external_url_resolution_navigations']}",
        flush=True,
    )
    print(f"Resolution cache hits: {run_metrics['resolution_cache_hits']}", flush=True)
    return combined_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run AMEX AIR locally from an Excel, CSV, or text prompt file."
    )
    parser.add_argument("--prompts", required=True, help="Path to the .xlsx, .csv, or .txt prompt file.")
    parser.add_argument(
        "--output-dir",
        help="Output folder (default: an output folder beside the prompt file).",
    )
    parser.add_argument("--delay-min-seconds", type=float, help="Override AIR_LOCAL_MIN_DELAY.")
    parser.add_argument("--delay-max-seconds", type=float, help="Override AIR_LOCAL_MAX_DELAY.")
    args = parser.parse_args(argv)
    try:
        run(
            args.prompts,
            args.output_dir,
            delay_min=args.delay_min_seconds,
            delay_max=args.delay_max_seconds,
        )
    except (InputError, UnicodeDecodeError, OSError, ValueError) as error:
        parser.exit(1, f"AMEX AIR local run failed: {error}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
