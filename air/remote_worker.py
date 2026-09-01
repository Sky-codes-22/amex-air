from __future__ import annotations

import argparse
import os
import random
import socket
import tempfile
import time
import traceback
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from air.collector import GoogleAIOverviewCollector
from air.excel_output import write_results
from air.inputs import BATCH_SIZE


class RemoteWorker:
    def __init__(self, server_url, token, worker_id=None, poll_seconds=5):
        self.server_url = server_url.rstrip("/")
        self.worker_id = worker_id or f"{socket.gethostname()}-air"
        self.poll_seconds = max(2, poll_seconds)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def post(self, path, **kwargs):
        response = self.session.post(f"{self.server_url}{path}", timeout=120, **kwargs)
        response.raise_for_status()
        return response

    def heartbeat(self):
        self.post("/worker/heartbeat", json={"worker_id": self.worker_id})

    def claim(self):
        response = self.post("/worker/claim", json={"worker_id": self.worker_id})
        return None if response.status_code == 204 else response.json()["job"]

    def update(self, run_id, completed, current_query, message, cooldown_until=None):
        response = self.post(
            f"/worker/jobs/{run_id}/progress",
            json={
                "worker_id": self.worker_id,
                "completed": completed,
                "current_query": current_query,
                "message": message,
                "cooldown_until": cooldown_until,
            },
        )
        return response.json().get("cancel_requested", False)

    def upload_results(self, run_id, rows, terminal_state="completed", terminal_message=None):
        with tempfile.TemporaryDirectory(prefix="amex_air_") as directory:
            directory = Path(directory)
            output = directory / "amex_air_all_batches.xlsx"
            write_results(output, rows)
            batch_paths = []
            for offset in range(0, len(rows), BATCH_SIZE):
                number = offset // BATCH_SIZE + 1
                batch_path = directory / f"amex_air_batch_{number}.xlsx"
                write_results(batch_path, rows[offset:offset + BATCH_SIZE])
                batch_paths.append(batch_path)
            combined_bytes = output.read_bytes()
            files = [(
                "workbook",
                ("amex_air_all_batches.xlsx", BytesIO(combined_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            )]
            files.extend((
                "batches",
                (path.name, BytesIO(path.read_bytes()), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ) for path in batch_paths)
            self.post(
                f"/worker/jobs/{run_id}/complete",
                data={
                    "worker_id": self.worker_id,
                    "success_count": sum(row["status"] == "Success" for row in rows),
                    "failed_count": sum(row["status"] == "Failed" for row in rows),
                    "terminal_state": terminal_state,
                    "terminal_message": terminal_message or "",
                },
                files=files,
            )

    @staticmethod
    def is_google_block(result):
        detail = str(result.get("response", "")).lower()
        return result.get("status") == "Failed" and (
            "captcha" in detail or "unusual-traffic" in detail or "unusual traffic" in detail
        )

    def wait_with_cancel(self, run_id, completed, query, seconds, message):
        deadline = time.monotonic() + max(0, seconds)
        cooldown_until = (
            datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))
        ).isoformat()
        if self.update(run_id, completed, query, message, cooldown_until=cooldown_until):
            return True
        while time.monotonic() < deadline:
            time.sleep(min(30, max(0, deadline - time.monotonic())))
            if time.monotonic() < deadline and self.update(
                run_id, completed, query, message, cooldown_until=cooldown_until
            ):
                return True
        return False

    def process(self, job):
        run_id = job["run_id"]
        queries = job["queries"]
        collector = GoogleAIOverviewCollector()
        rows = []
        legacy_setting = os.getenv("AIR_QUERY_DELAY_SECONDS")
        legacy_delay = float(legacy_setting) if legacy_setting is not None else 10
        delay_min = max(0, float(os.getenv("AIR_QUERY_DELAY_MIN_SECONDS", str(legacy_delay))))
        default_delay_max = legacy_delay if legacy_setting is not None else 15
        delay_max = max(delay_min, float(os.getenv("AIR_QUERY_DELAY_MAX_SECONDS", str(default_delay_max))))
        rest_every = max(0, int(os.getenv("AIR_BATCH_REST_EVERY", "20")))
        rest_seconds = max(0, float(os.getenv("AIR_BATCH_REST_SECONDS", "90")))
        captcha_retries = max(0, int(os.getenv("AIR_CAPTCHA_RETRIES", "2")))
        captcha_cooldown = max(0, float(os.getenv("AIR_CAPTCHA_COOLDOWN_SECONDS", "1800")))
        inter_batch_cooldown = max(
            0, float(os.getenv("AIR_INTER_BATCH_COOLDOWN_SECONDS", "1800"))
        )
        try:
            for index, query in enumerate(queries, start=1):
                cancelled = self.update(run_id, index - 1, query, f"Collecting query {index} of {len(queries)} on the laptop...")
                if cancelled:
                    self.upload_results(run_id, rows)
                    print(f"[{run_id}] Cancelled before query {index}; uploaded {len(rows)} partial results.", flush=True)
                    return
                result = collector.collect(query)
                retry = 0
                while self.is_google_block(result) and retry < captcha_retries:
                    retry += 1
                    message = (
                        f"Google temporarily blocked automated searches. Pausing for "
                        f"{int(captcha_cooldown)} seconds before retry {retry} of {captcha_retries}; "
                        "no additional searches are being sent."
                    )
                    if self.wait_with_cancel(run_id, index - 1, query, captcha_cooldown, message):
                        self.upload_results(run_id, rows)
                        print(f"[{run_id}] Cancelled during Google cooldown; uploaded {len(rows)} partial results.", flush=True)
                        return
                    result = collector.collect(query)
                if self.is_google_block(result):
                    rows.append({"prompt": query, **result})
                    self.update(
                        run_id, index, query,
                        "Google CAPTCHA remained after 2 retries. Processing paused; partial files are ready."
                    )
                    self.upload_results(
                        run_id,
                        rows,
                        terminal_state="paused",
                        terminal_message=(
                            "Google CAPTCHA remained after 2 retries. Processing ended "
                            "and all results collected so far are ready to download."
                        ),
                    )
                    print(f"[{run_id}] CAPTCHA persisted after {captcha_retries} retries; uploaded partial results.", flush=True)
                    return
                rows.append({"prompt": query, **result})
                cancelled = self.update(run_id, index, query, f"Completed query {index} of {len(queries)}.")
                if cancelled:
                    self.upload_results(run_id, rows)
                    print(f"[{run_id}] Cancelled after query {index}; uploaded {len(rows)} partial results.", flush=True)
                    return
                if index < len(queries):
                    if index % BATCH_SIZE == 0 and self.wait_with_cancel(
                        run_id,
                        index,
                        query,
                        inter_batch_cooldown,
                        f"Batch {index // BATCH_SIZE} complete. Cooling down for 30 minutes before the next batch.",
                    ):
                        self.upload_results(run_id, rows)
                        print(f"[{run_id}] Cancelled between batches; uploaded {len(rows)} partial results.", flush=True)
                        return
                    if rest_every and index % rest_every == 0:
                        if self.wait_with_cancel(
                            run_id,
                            index,
                            query,
                            rest_seconds,
                            f"Resting for {int(rest_seconds)} seconds after {index} queries to reduce Google traffic.",
                        ):
                            self.upload_results(run_id, rows)
                            print(f"[{run_id}] Cancelled during batch rest; uploaded {len(rows)} partial results.", flush=True)
                            return
                    delay = random.uniform(delay_min, delay_max)
                    if delay and self.wait_with_cancel(
                        run_id,
                        index,
                        query,
                        delay,
                        f"Waiting {int(round(delay))} seconds before the next query to reduce Google traffic.",
                    ):
                        self.upload_results(run_id, rows)
                        print(f"[{run_id}] Cancelled between queries; uploaded {len(rows)} partial results.", flush=True)
                        return
            self.upload_results(run_id, rows)

            print(f"[{run_id}] Completed {len(queries)} queries.", flush=True)
        except BaseException as error:
            detail = f"{type(error).__name__}: {error}"
            print(f"[{run_id}] Failed: {detail}\n{traceback.format_exc()}", flush=True)
            try:
                terminal_state = "timed_out" if isinstance(error, TimeoutError) else "failed"
                self.upload_results(
                    run_id,
                    rows,
                    terminal_state=terminal_state,
                    terminal_message=f"Processing ended early ({detail}). Partial results are ready to download.",
                )
            except Exception as upload_error:
                print(f"[{run_id}] Could not upload partial results: {upload_error}", flush=True)
                try:
                    self.post(f"/worker/jobs/{run_id}/fail", json={"worker_id": self.worker_id, "error": detail})
                except Exception as report_error:
                    print(f"[{run_id}] Could not report failure: {report_error}", flush=True)

    def run_forever(self):
        print(f"AMEX AIR laptop worker '{self.worker_id}' connecting to {self.server_url}", flush=True)
        cdp_url = os.getenv("AIR_CDP_URL", "").rstrip("/")
        chrome_warning_shown = False
        while True:
            try:
                if cdp_url:
                    try:
                        response = requests.get(f"{cdp_url}/json/version", timeout=3)
                        response.raise_for_status()
                        chrome_warning_shown = False
                    except requests.RequestException:
                        if not chrome_warning_shown:
                            print(f"Chrome is not available at {cdp_url}. Requests will remain queued until it starts.", flush=True)
                            chrome_warning_shown = True
                        time.sleep(self.poll_seconds)
                        continue
                self.heartbeat()
                job = self.claim()
                if job:
                    print(f"[{job['run_id']}] Claimed {len(job['queries'])} queries from {job['filename']}.", flush=True)
                    self.process(job)
                else:
                    time.sleep(self.poll_seconds)
            except KeyboardInterrupt:
                print("AMEX AIR laptop worker stopped.", flush=True)
                return
            except requests.RequestException as error:
                print(f"Worker connection error: {error}. Retrying in {self.poll_seconds} seconds.", flush=True)
                time.sleep(self.poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Run the AMEX AIR laptop processing engine.")
    parser.add_argument("--server", default=os.getenv("AIR_SERVER_URL", "https://amex-air.onrender.com"))
    parser.add_argument("--token", default=os.getenv("AIR_WORKER_TOKEN"))
    parser.add_argument("--worker-id", default=os.getenv("AIR_WORKER_ID"))
    parser.add_argument("--poll-seconds", type=int, default=int(os.getenv("AIR_WORKER_POLL_SECONDS", "5")))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("Set AIR_WORKER_TOKEN before starting the laptop worker.")
    RemoteWorker(args.server, args.token, args.worker_id, args.poll_seconds).run_forever()


if __name__ == "__main__":
    main()
