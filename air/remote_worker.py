from __future__ import annotations

import argparse
import os
import socket
import tempfile
import time
import traceback
from pathlib import Path

import requests

from air.collector import GoogleAIOverviewCollector
from air.excel_output import write_results


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

    def update(self, run_id, completed, current_query, message):
        response = self.post(
            f"/worker/jobs/{run_id}/progress",
            json={
                "worker_id": self.worker_id,
                "completed": completed,
                "current_query": current_query,
                "message": message,
            },
        )
        return response.json().get("cancel_requested", False)

    def process(self, job):
        run_id = job["run_id"]
        queries = job["queries"]
        collector = GoogleAIOverviewCollector()
        rows = []
        delay = max(0, float(os.getenv("AIR_QUERY_DELAY_SECONDS", "2")))
        try:
            for index, query in enumerate(queries, start=1):
                cancelled = self.update(run_id, index - 1, query, f"Collecting query {index} of {len(queries)} on the laptop...")
                if cancelled:
                    print(f"[{run_id}] Cancelled before query {index}.", flush=True)
                    return
                result = collector.collect(query)
                rows.append({"prompt": query, **result})
                cancelled = self.update(run_id, index, query, f"Completed query {index} of {len(queries)}.")
                if cancelled:
                    print(f"[{run_id}] Cancelled after query {index}.", flush=True)
                    return
                if delay and index < len(queries):
                    time.sleep(delay)
            with tempfile.TemporaryDirectory(prefix="amex_air_") as directory:
                output = Path(directory) / "amex_air_results.xlsx"
                write_results(output, rows)
                with output.open("rb") as workbook:
                    self.post(
                        f"/worker/jobs/{run_id}/complete",
                        data={
                            "worker_id": self.worker_id,
                            "success_count": sum(row["status"] == "Success" for row in rows),
                            "failed_count": sum(row["status"] == "Failed" for row in rows),
                        },
                        files={"workbook": ("amex_air_results.xlsx", workbook, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    )
            print(f"[{run_id}] Completed {len(queries)} queries.", flush=True)
        except BaseException as error:
            detail = f"{type(error).__name__}: {error}"
            print(f"[{run_id}] Failed: {detail}\n{traceback.format_exc()}", flush=True)
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