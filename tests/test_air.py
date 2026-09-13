import io
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from air.ad_ranking import analyze_sponsored_ads, identify_brand
from air.collector import (
    DestinationURLResolver,
    _blue_link_candidates,
    _resolve_destination,
    _resolve_structure_urls,
    _serp_snapshot,
    _serp_position,
    _serp_structure,
    _top_blue_links_from_structure,
)
from air.inputs import InputError, read_queries
from air.local_runner import (
    chrome_executable_candidates,
    find_chrome_executable,
    run as run_local,
)
from air.remote_worker import RemoteWorker
from air.screenshots import ScreenshotRun, sanitize_screenshot_stem, save_qc_screenshot
from air.serp_diagnostic import inspect_dom
from air.worker import main as run_legacy_worker
from app import create_app


def uploaded_file(files, field):
    entries = files.items() if isinstance(files, dict) else files
    return next(value for key, value in entries if key == field)


class AirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chrome_executable = Path(self.temp.name) / "chrome.exe"
        self.chrome_executable.write_bytes(b"test chrome")
        database = Path(self.temp.name) / "air-test.db"
        self.app = create_app({
            "TESTING": True,
            "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "AIR_WORKER_TOKEN": "test-worker-secret",
        })
        self.client = self.app.test_client()
        self.client_id = "a" * 32
        self.client_headers = {"X-AIR-Client-ID": self.client_id}
        self.worker_headers = {"Authorization": "Bearer test-worker-secret"}

    def tearDown(self):
        self.app.extensions["air_store"].engine.dispose()
        self.temp.cleanup()

    def upload(self, text=b"first query\nsecond query", filename="queries.txt"):
        return self.client.post(
            "/jobs",
            data={"file": (io.BytesIO(text), filename)},
            content_type="multipart/form-data",
            headers=self.client_headers,
        )

    def test_brand_queue_interface_and_limit(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn(b"AMEX AIR", response.data)
        self.assertIn(b"AI Insights &amp; Responses", response.data)
        self.assertIn(b"35 queries", response.data)
        self.assertIn(b"Request status", response.data)

    def test_supported_inputs_and_upload_limit(self):
        self.assertEqual(["one", "two"], read_queries("q.txt", b"one\ntwo\none\n"))
        self.assertEqual(["one", "two"], read_queries("q.csv", b"Prompt,Other\none,x\ntwo,y\n"))
        book = Workbook()
        sheet = book.active
        sheet.append(["Prompt"])
        sheet.append(["one"])
        sheet.append(["two"])
        stream = io.BytesIO()
        book.save(stream)
        self.assertEqual(["one", "two"], read_queries("q.xlsx", stream.getvalue()))
        with self.assertRaisesRegex(InputError, "at most 500"):
            read_queries("q.txt", "\n".join(f"query {i}" for i in range(501)).encode())

    def test_upload_is_durable_and_visible_to_same_browser(self):
        response = self.upload()
        self.assertEqual(202, response.status_code, response.get_data(as_text=True))
        job = response.get_json()["job"]
        self.assertEqual("queued", job["state"])
        self.assertEqual(2, job["total"])
        returning_browser = self.app.test_client()
        listing = returning_browser.get("/jobs", headers=self.client_headers).get_json()
        self.assertEqual(job["run_id"], listing["jobs"][0]["run_id"])
        self.assertFalse(listing["worker"]["online"])
        other = returning_browser.get("/jobs", headers={"X-AIR-Client-ID": "b" * 32}).get_json()
        self.assertEqual([], other["jobs"])

    def test_worker_claim_progress_complete_and_download(self):
        run_id = self.upload().get_json()["job"]["run_id"]
        denied = self.client.post("/worker/claim", json={"worker_id": "laptop"})
        self.assertEqual(401, denied.status_code)
        claim = self.client.post("/worker/claim", json={"worker_id": "laptop"}, headers=self.worker_headers)
        self.assertEqual(run_id, claim.get_json()["job"]["run_id"])
        progress = self.client.post(
            f"/worker/jobs/{run_id}/progress",
            json={"worker_id": "laptop", "completed": 1, "current_query": "second query", "message": "One done."},
            headers=self.worker_headers,
        )
        self.assertFalse(progress.get_json()["cancel_requested"])
        completed = self.client.post(
            f"/worker/jobs/{run_id}/complete",
            data={
                "worker_id": "laptop",
                "success_count": "1",
                "failed_count": "1",
                "workbook": (io.BytesIO(b"test-workbook"), "results.xlsx"),
            },
            content_type="multipart/form-data",
            headers=self.worker_headers,
        )
        self.assertEqual(200, completed.status_code)
        job = self.client.get(f"/jobs/{run_id}", headers=self.client_headers).get_json()
        self.assertEqual("completed", job["state"])
        self.assertEqual(2, job["completed"])
        download = self.client.get(job["download_url"])
        self.assertEqual(b"test-workbook", download.data)
        self.assertEqual(404, self.client.get(f"/jobs/{run_id}/download?token=wrong").status_code)

    def test_cancel_queued_and_processing_jobs(self):
        queued_id = self.upload(b"queued").get_json()["job"]["run_id"]
        response = self.client.post(f"/jobs/{queued_id}/cancel", headers=self.client_headers)
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        queued = response.get_json()["job"]
        self.assertEqual("cancelled", queued["state"])
        self.assertIn("download_url", queued)
        empty_output = self.client.get(queued["download_url"])
        self.assertEqual(200, empty_output.status_code)
        empty_book = load_workbook(io.BytesIO(empty_output.data), read_only=True, data_only=True)
        self.assertEqual(1, empty_book["Responses"].max_row)
        empty_book.close()
        processing_id = self.upload(b"processing").get_json()["job"]["run_id"]
        self.client.post("/worker/claim", json={"worker_id": "laptop"}, headers=self.worker_headers)
        self.client.post(
            f"/worker/jobs/{processing_id}/progress",
            json={"worker_id": "laptop", "completed": 1, "current_query": "processing", "message": "One done."},
            headers=self.worker_headers,
        )
        processing = self.client.post(f"/jobs/{processing_id}/cancel", headers=self.client_headers).get_json()["job"]
        self.assertTrue(processing["cancel_requested"])
        partial = self.client.post(
            f"/worker/jobs/{processing_id}/complete",
            data={
                "worker_id": "laptop",
                "success_count": "1",
                "failed_count": "0",
                "workbook": (io.BytesIO(b"partial-workbook"), "results.xlsx"),
            },
            content_type="multipart/form-data",
            headers=self.worker_headers,
        )
        self.assertEqual("cancelled", partial.get_json()["state"])
        job = self.client.get(f"/jobs/{processing_id}", headers=self.client_headers).get_json()
        self.assertEqual("cancelled", job["state"])
        self.assertEqual(1, job["completed"])
        self.assertIn("Partial results", job["message"])
        self.assertEqual(b"partial-workbook", self.client.get(job["download_url"]).data)

    def test_laptop_worker_builds_and_uploads_workbook(self):
        from unittest.mock import MagicMock, patch

        class FakeCollector:
            def collect(self, query):
                return {"status": "Success", "response": f"answer: {query}", "parsed_json": "{}", "execution_time": 0.1}

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        progress = []
        uploads = []
        worker.update = lambda run_id, completed, current_query, message: progress.append((completed, current_query)) or False

        def capture(path, **kwargs):
            workbook = uploaded_file(kwargs["files"], "workbook")
            batches = [value for key, value in kwargs["files"] if key == "batches"]
            uploads.append((path, kwargs["data"], workbook[1].read(), batches))
            return MagicMock()

        worker.post = capture
        with patch("air.remote_worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", {"AIR_QUERY_DELAY_SECONDS": "0"}):
            worker.process({"run_id": "c" * 32, "filename": "queries.txt", "queries": ["one", "two"]})
        self.assertEqual([0, 1, 1, 2], [item[0] for item in progress])
        self.assertEqual("/worker/jobs/cccccccccccccccccccccccccccccccc/complete", uploads[0][0])
        self.assertEqual(["amex_air_batch_1.xlsx"], [batch[0] for batch in uploads[0][3]])
        workbook = load_workbook(io.BytesIO(uploads[0][2]), read_only=True, data_only=True)
        self.assertEqual("answer: one", workbook["Responses"]["C2"].value)
        workbook.close()

    def test_laptop_worker_retries_transient_render_errors(self):
        import requests
        from unittest.mock import MagicMock, patch

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        unavailable = MagicMock()
        unavailable.status_code = 502
        transient_error = requests.HTTPError(response=unavailable)
        failed_response = MagicMock()
        failed_response.raise_for_status.side_effect = transient_error
        recovered_response = MagicMock()
        worker.session.post = MagicMock(
            side_effect=[failed_response, recovered_response]
        )

        with patch("air.remote_worker.time.sleep") as sleep:
            response = worker.post("/worker/heartbeat", json={"worker_id": "laptop"})

        self.assertIs(recovered_response, response)
        self.assertEqual(2, worker.session.post.call_count)
        sleep.assert_called_once_with(2)

    def test_laptop_worker_uploads_partial_workbook_when_cancelled(self):
        from unittest.mock import MagicMock, patch

        collected = []

        class FakeCollector:
            def collect(self, query):
                collected.append(query)
                return {"status": "Success", "response": f"answer: {query}", "parsed_json": "{}", "execution_time": 0.1}

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        uploads = []
        worker.update = lambda _run_id, completed, _query, _message, **_kwargs: completed == 1

        def capture(path, **kwargs):
            workbook = uploaded_file(kwargs["files"], "workbook")
            uploads.append((path, kwargs["data"], workbook[1].read()))
            return MagicMock()

        worker.post = capture
        with patch("air.remote_worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", {"AIR_QUERY_DELAY_SECONDS": "0"}):
            worker.process({"run_id": "p" * 32, "filename": "queries.txt", "queries": ["one", "two", "three"]})
        self.assertEqual(["one"], collected)
        self.assertEqual("/worker/jobs/pppppppppppppppppppppppppppppppp/complete", uploads[0][0])
        self.assertEqual("1", str(uploads[0][1]["success_count"]))
        workbook = load_workbook(io.BytesIO(uploads[0][2]), read_only=True, data_only=True)
        self.assertEqual(2, workbook["Responses"].max_row)
        self.assertEqual("one", workbook["Responses"]["A2"].value)
        workbook.close()

    def test_laptop_worker_cools_down_and_retries_same_query_after_captcha(self):
        from unittest.mock import MagicMock, patch

        collected = []

        class FakeCollector:
            def collect(self, query):
                collected.append(query)
                if len(collected) == 1:
                    return {
                        "status": "Failed",
                        "response": "Google displayed a CAPTCHA or unusual-traffic block.",
                        "parsed_json": "",
                        "execution_time": 0.1,
                    }
                return {"status": "Success", "response": f"answer: {query}", "parsed_json": "{}", "execution_time": 0.1}

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        updates = []
        uploads = []
        worker.update = lambda _run_id, completed, query, message, **_kwargs: updates.append((completed, query, message)) or False

        def capture(path, **kwargs):
            workbook = uploaded_file(kwargs["files"], "workbook")
            uploads.append((path, kwargs["data"], workbook[1].read()))
            return MagicMock()

        worker.post = capture
        settings = {
            "AIR_QUERY_DELAY_SECONDS": "0",
            "AIR_CAPTCHA_COOLDOWN_SECONDS": "0",
            "AIR_CAPTCHA_RETRIES": "1",
            "AIR_BATCH_REST_EVERY": "0",
        }
        with patch("air.remote_worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", settings):
            worker.process({"run_id": "r" * 32, "filename": "queries.txt", "queries": ["one"]})

        self.assertEqual(["one", "one"], collected)
        self.assertTrue(any("temporarily blocked" in message for _, _, message in updates))
        self.assertEqual("1", str(uploads[0][1]["success_count"]))
        self.assertEqual("0", str(uploads[0][1]["failed_count"]))

    def test_36_queries_create_two_batches_and_one_combined_workbook(self):
        from unittest.mock import MagicMock, patch

        class FakeCollector:
            def collect(self, query):
                return {
                    "status": "Success", "response": query, "parsed_json": "{}",
                    "top_blue_links": "[]", "execution_time": 0.1,
                }

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        worker.update = lambda *_args, **_kwargs: False
        uploads = []

        def capture(path, **kwargs):
            uploads.append((path, kwargs))
            return MagicMock()

        worker.post = capture
        settings = {
            "AIR_QUERY_DELAY_SECONDS": "0", "AIR_BATCH_REST_EVERY": "0",
            "AIR_INTER_BATCH_COOLDOWN_SECONDS": "0",
        }
        with patch("air.remote_worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", settings):
            worker.process({"run_id": "b" * 32, "filename": "queries.txt", "queries": [f"q{i}" for i in range(36)]})

        files = uploads[0][1]["files"]
        batches = [value for key, value in files if key == "batches"]
        self.assertEqual(["amex_air_batch_1.xlsx", "amex_air_batch_2.xlsx"], [item[0] for item in batches])
        combined = uploaded_file(files, "workbook")
        book = load_workbook(io.BytesIO(combined[1].read()), read_only=True, data_only=True)
        self.assertEqual(37, book["Responses"].max_row)
        book.close()

    def test_persistent_captcha_stops_after_two_retries_with_partial_output(self):
        from unittest.mock import MagicMock, patch

        attempts = []

        class BlockedCollector:
            def collect(self, query):
                attempts.append(query)
                return {
                    "status": "Failed",
                    "response": "Google displayed a CAPTCHA or unusual-traffic block.",
                    "parsed_json": "", "top_blue_links": "[]", "execution_time": 0.1,
                }

        worker = RemoteWorker("https://example.test", "secret", worker_id="laptop")
        worker.update = lambda *_args, **_kwargs: False
        uploads = []
        worker.post = lambda path, **kwargs: uploads.append((path, kwargs)) or MagicMock()
        settings = {
            "AIR_QUERY_DELAY_SECONDS": "0", "AIR_CAPTCHA_COOLDOWN_SECONDS": "0",
            "AIR_CAPTCHA_RETRIES": "2", "AIR_BATCH_REST_EVERY": "0",
        }
        with patch("air.remote_worker.GoogleAIOverviewCollector", BlockedCollector), patch.dict("os.environ", settings):
            worker.process({"run_id": "x" * 32, "filename": "queries.txt", "queries": ["blocked", "must not run"]})

        self.assertEqual(["blocked", "blocked", "blocked"], attempts)
        self.assertEqual("paused", uploads[0][1]["data"]["terminal_state"])
        self.assertEqual("1", str(uploads[0][1]["data"]["failed_count"]))

    def test_server_exposes_batch_and_combined_downloads(self):
        run_id = self.upload().get_json()["job"]["run_id"]
        self.client.post("/worker/claim", json={"worker_id": "laptop"}, headers=self.worker_headers)
        completed = self.client.post(
            f"/worker/jobs/{run_id}/complete",
            data={
                "worker_id": "laptop", "success_count": "2", "failed_count": "0",
                "workbook": (io.BytesIO(b"combined"), "amex_air_all_batches.xlsx"),
                "batches": [
                    (io.BytesIO(b"batch-one"), "amex_air_batch_1.xlsx"),
                    (io.BytesIO(b"batch-two"), "amex_air_batch_2.xlsx"),
                ],
            },
            content_type="multipart/form-data",
            headers=self.worker_headers,
        )
        self.assertEqual(200, completed.status_code)
        job = self.client.get(f"/jobs/{run_id}", headers=self.client_headers).get_json()
        self.assertEqual(2, len(job["batch_downloads"]))
        self.assertEqual(b"combined", self.client.get(job["download_url"]).data)
        self.assertEqual(b"batch-one", self.client.get(job["batch_downloads"][0]["download_url"]).data)

    def test_legacy_excel_writer_still_matches_output_contract(self):
        job_root = Path(self.temp.name) / "legacy-job"
        job_root.mkdir()
        (job_root / "job.json").write_text(json.dumps({"queries": ["one"]}), encoding="utf-8")

        class FakeCollector:
            def collect(self, query):
                return {"status": "Success", "response": query, "parsed_json": "{}", "execution_time": 0.1}

        from unittest.mock import patch
        with patch("air.worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", {"AIR_QUERY_DELAY_SECONDS": "0"}):
            run_legacy_worker(job_root)
        workbook = load_workbook(job_root / "amex_air_results.xlsx", read_only=True, data_only=True)
        self.assertEqual(
            (
                "Prompt", "Status", "Response", "Parsed JSON", "Top 3 Blue Links",
                "Execution Time (sec)", "SERP First Element", "AI Overview Position",
                "AI Overview On Top", "SERP Order JSON", "AIO Ad Present", "AIO Ad Count",
                "AIO Ads JSON", "Sponsored Ads JSON", "Sponsored Ad Count",
                "AMEX Sponsored Ad Present", "AMEX Sponsored Ad Rank", "AMEX SERP Rank",
                "Brands in Sponsored Ads", "AMEX Ad Competitive Position",
            ),
            tuple(cell.value for cell in workbook["Responses"][1]),
        )
        workbook.close()

    def test_excel_keeps_blue_link_headline_url_and_sponsored_status(self):
        from air.excel_output import results_bytes

        links = [{
            "headline": "Example result",
            "url": "https://example.com/result",
            "sponsored": False,
        }]
        workbook = load_workbook(
            io.BytesIO(results_bytes([{
                "prompt": "query", "status": "Success", "response": "answer",
                "parsed_json": "{}", "top_blue_links": json.dumps(links),
                "serp_first_element": "AI Overview", "ai_overview_position": 1,
                "ai_overview_on_top": True, "serp_order_json": '[{"rank": 1, "type": "AI Overview"}]',
                "aio_ad_present": False, "aio_ad_count": 0, "aio_ads_json": "[]",
                "sponsored_ads_json": "[]", "sponsored_ad_count": 0,
                "amex_sponsored_ad_present": False, "amex_sponsored_ad_rank": None,
                "amex_serp_rank": None, "brands_in_sponsored_ads": "",
                "amex_ad_competitive_position": "No Sponsored Ads",
                "execution_time": 0.1,
            }])),
            read_only=True,
            data_only=True,
        )
        self.assertEqual(links, json.loads(workbook["Responses"]["E2"].value))
        self.assertEqual("AI Overview", workbook["Responses"]["G2"].value)
        self.assertEqual(1, workbook["Responses"]["H2"].value)
        self.assertIs(True, workbook["Responses"]["I2"].value)
        self.assertEqual([{"rank": 1, "type": "AI Overview"}], json.loads(workbook["Responses"]["J2"].value))
        self.assertIs(False, workbook["Responses"]["K2"].value)
        self.assertEqual(0, workbook["Responses"]["L2"].value)
        self.assertEqual([], json.loads(workbook["Responses"]["M2"].value))
        self.assertEqual([], json.loads(workbook["Responses"]["N2"].value))
        self.assertEqual(0, workbook["Responses"]["O2"].value)
        self.assertIs(False, workbook["Responses"]["P2"].value)
        self.assertEqual("No Sponsored Ads", workbook["Responses"]["T2"].value)
        workbook.close()

    def test_excel_derives_amex_serp_rank_from_organic_americanexpress_url(self):
        from air.excel_output import results_bytes

        wrapper = "https://www.google.com/goto?url=opaque-amex"
        structure = [
            {"rank": 1, "type": "AI Overview"},
            {
                "rank": 2, "type": "Organic", "headline": "American Express",
                "url": wrapper, "raw_url": wrapper, "sponsored": False,
            },
            {
                "rank": 3, "type": "Sponsored", "headline": "American Express ad",
                "url": "https://www.americanexpress.com/ad", "raw_url": "https://www.americanexpress.com/ad",
                "sponsored": True,
            },
        ]
        top_links = [{
            "headline": "American Express", "url": "https://www.americanexpress.com/us/credit-cards/",
            "raw_url": wrapper, "sponsored": False,
        }]
        row = {
            "prompt": "american express card", "status": "Success", "response": "answer",
            "parsed_json": "{}", "top_blue_links": json.dumps(top_links),
            "serp_order_json": json.dumps(structure), "execution_time": 0.1,
        }
        workbook = load_workbook(
            io.BytesIO(results_bytes([row])), read_only=True, data_only=True
        )
        self.assertEqual(2, workbook["Responses"]["R2"].value)
        workbook.close()

    def test_production_snapshot_captures_aio_side_cards_separately(self):
        from playwright.sync_api import sync_playwright

        markup = """
        <main id="search">
          <div jsname="KFl8ub" style="position:relative;width:800px;height:280px">
            <div style="position:absolute;left:0;top:20px;width:380px;height:120px">
              AI response <a href="https://citation.example">Citation</a>
            </div>
            <a href="https://www.americanexpress.com/card" style="position:absolute;display:block;left:500px;top:20px;width:250px;height:80px">
              <h3>American Express Credit Cards</h3><span>Rewards and banking</span>
            </a>
            <a href="https://www.americanexpress.com/login" style="position:absolute;display:block;left:500px;top:115px;width:250px;height:80px">
              <h3>Log in to My Account</h3>
            </a>
          </div>
          <a href="https://organic.example" style="position:absolute;display:block;top:320px;width:400px;height:50px"><h3>Organic result</h3></a>
        </main>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup)
                snapshot = _serp_snapshot(page)
            finally:
                browser.close()

        self.assertTrue(snapshot["aio_present"])
        self.assertEqual(2, len(snapshot["aio_ads"]))
        self.assertEqual(
            ["American Express Credit Cards", "Log in to My Account"],
            [item["headline"] for item in snapshot["aio_ads"]],
        )
        self.assertTrue(all(item["position"] == "right" for item in snapshot["aio_ads"]))
        self.assertTrue(all(item["label"] == "AIO side card" for item in snapshot["aio_ads"]))

    def test_sponsored_ad_brand_ranking_scenarios(self):
        def result(rank, brand_url, headline, result_type="Sponsored"):
            return {
                "rank": rank,
                "type": result_type,
                "headline": headline,
                "url": brand_url,
                "raw_url": brand_url,
                "sponsored": result_type == "Sponsored",
            }

        chase_amex_capital_one = analyze_sponsored_ads([
            result(1, "https://creditcards.chase.com/card", "Chase card"),
            result(2, "https://www.americanexpress.com/card", "American Express card"),
            result(3, "https://www.capitalone.com/card", "Capital One card"),
        ])
        self.assertEqual(3, chase_amex_capital_one["sponsored_ad_count"])
        self.assertEqual(2, chase_amex_capital_one["amex_sponsored_ad_rank"])
        self.assertEqual(2, chase_amex_capital_one["amex_serp_rank"])
        self.assertEqual("2nd", chase_amex_capital_one["amex_ad_competitive_position"])
        self.assertEqual(
            "Chase | American Express | Capital One",
            chase_amex_capital_one["brands_in_sponsored_ads"],
        )

        amex_first = analyze_sponsored_ads([
            result(1, "https://americanexpress.com/one", "AMEX one"),
            result(2, "https://chase.com/two", "Chase two"),
        ])
        self.assertEqual(1, amex_first["amex_sponsored_ad_rank"])
        self.assertEqual("1st", amex_first["amex_ad_competitive_position"])

        competitors_only = analyze_sponsored_ads([
            result(1, "https://chase.com", "Chase"),
            result(2, "https://citi.com", "Citi"),
        ])
        self.assertFalse(competitors_only["amex_sponsored_ad_present"])
        self.assertEqual("No AMEX Ad", competitors_only["amex_ad_competitive_position"])

        no_ads = analyze_sponsored_ads([
            result(1, "https://americanexpress.com", "American Express", "Organic"),
            {"rank": 2, "type": "AI Overview"},
        ])
        self.assertEqual(0, no_ads["sponsored_ad_count"])
        self.assertFalse(no_ads["amex_sponsored_ad_present"])
        self.assertIsNone(no_ads["amex_sponsored_ad_rank"])
        self.assertEqual("No Sponsored Ads", no_ads["amex_ad_competitive_position"])
        self.assertEqual("American Express", no_ads["serp_structure"][0]["brand"])
        self.assertNotIn("brand", no_ads["serp_structure"][1])

        unknown = analyze_sponsored_ads([
            result(1, "https://unknown-advertiser.example", "Excellent card offer"),
        ])
        self.assertEqual("UNKNOWN", unknown["sponsored_ads"][0]["brand"])
        self.assertEqual("UNKNOWN", unknown["brands_in_sponsored_ads"])

        multiple_amex = analyze_sponsored_ads([
            result(1, "https://americanexpress.com/first", "First AMEX"),
            result(2, "https://chase.com", "Chase"),
            result(3, "https://americanexpress.com/second", "Second AMEX"),
        ])
        self.assertEqual(1, multiple_amex["amex_sponsored_ad_rank"])
        self.assertEqual(1, multiple_amex["amex_serp_rank"])

    def test_brand_matching_domain_priority_and_publisher_exclusion(self):
        self.assertEqual(
            "UNKNOWN",
            identify_brand({
                "headline": "Best American Express credit cards",
                "url": "https://www.nerdwallet.com/credit-cards/amex",
            }),
        )
        self.assertEqual(
            "American Express",
            identify_brand({
                "headline": "Compare Chase card offers",
                "url": "https://www.americanexpress.com/us/cards",
            }),
        )

    def test_serp_position_uses_visible_rendered_order(self):
        from playwright.sync_api import sync_playwright

        def aio(top, extra=""):
            return f'<div jsname="KFl8ub" style="position:absolute;top:{top}px;width:400px;height:40px;{extra}">AI Overview</div>'

        def organic(top, headline="Organic", extra=""):
            return (
                f'<div style="position:absolute;top:{top}px;{extra}">'
                f'<a href="https://example.com/{top}" style="display:block;width:400px;height:40px"><h3>{headline}</h3></a>'
                "</div>"
            )

        def sponsored(top, headline="Sponsored"):
            return (
                f'<div data-text-ad style="position:absolute;top:{top}px">'
                f'<span> Sponsored </span><a href="https://ads.example/{top}" '
                f'style="display:block;width:400px;height:40px"><h3>{headline}</h3></a></div>'
            )

        cases = [
            ("aio first", aio(10) + organic(100), "AI Overview", 1, True),
            ("organic first", organic(10) + aio(100), "Organic", 2, False),
            ("sponsored first", sponsored(10) + aio(100), "Sponsored", 2, False),
            (
                "multiple before aio",
                sponsored(10, "Ad one") + organic(50) + sponsored(90, "Ad two") + aio(130),
                "Sponsored", 4, False,
            ),
            ("no aio", organic(10), "Organic", None, None),
            ("invisible ignored", organic(10, extra="display:none") + aio(100), "AI Overview", 1, True),
            ("visual differs from dom", aio(200) + organic(20), "Organic", 2, False),
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                for name, contents, first, aio_position, on_top in cases:
                    with self.subTest(name=name):
                        page.set_content(f'<div id="search">{contents}</div>')
                        position = _serp_position(page)
                        self.assertEqual(first, position["serp_first_element"])
                        self.assertEqual(aio_position, position["ai_overview_position"])
                        self.assertEqual(on_top, position["ai_overview_on_top"])
            finally:
                browser.close()

    def test_aio_ad_diagnostic_fixtures_require_positive_sponsorship(self):
        from playwright.sync_api import sync_playwright

        fixtures = [
            (
                "normal citation card",
                '<div jsname="KFl8ub"><div role="listitem"><a href="https://source.example">Citation source</a></div></div>',
                0,
            ),
            (
                "explicit sponsored label",
                '<div jsname="KFl8ub"><div role="listitem"><span> Sponsored </span><a href="https://ad.example">Offer</a></div></div>',
                1,
            ),
            (
                "explicit aria ad label",
                '<div jsname="KFl8ub"><div role="listitem"><span aria-label="Ad">Promotion</span><a href="https://ad.example">Offer</a></div></div>',
                1,
            ),
            (
                "multiple aio ads",
                '<div jsname="KFl8ub"><div role="listitem"><span>Sponsored</span><a href="https://one.example">One</a></div>'
                '<div role="listitem"><span>Ad</span><a href="https://two.example">Two</a></div></div>',
                2,
            ),
            ("aio with no cards", '<div jsname="KFl8ub">AI response only</div>', 0),
            (
                "no aio",
                '<div data-text-ad><span>Sponsored</span><a href="https://outside.example">Outside ad</a></div>',
                0,
            ),
            (
                "ad-like unlabeled product card",
                '<div jsname="KFl8ub"><div role="listitem" data-product-id="card-1">'
                '<a href="https://issuer.example/apply">Apply now</a></div></div>',
                0,
            ),
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                for name, markup, expected_signals in fixtures:
                    with self.subTest(name=name):
                        page.set_content(f'<main id="search">{markup}</main>')
                        evidence = inspect_dom(page)
                        self.assertEqual(expected_signals, len(evidence["aio_sponsorship_signals"]))
                        explicit_cards = [
                            card for card in evidence["aio_linked_card_structures"]
                            if card["explicit_sponsorship"]
                        ]
                        self.assertEqual(expected_signals, len(explicit_cards))
            finally:
                browser.close()

    def test_serp_structure_is_unified_deduplicated_and_visually_ordered(self):
        from playwright.sync_api import sync_playwright
        from unittest.mock import patch

        markup = """
        <div id="search">
          <div jsname="KFl8ub" style="position:absolute;top:100px;width:400px;height:40px">
            AI Overview
            <a href="https://aio.example/source" style="display:block;width:200px;height:20px"><h3>AIO citation</h3></a>
          </div>
          <div style="position:absolute;top:140px"><a href="https://after.example" style="display:block;width:400px;height:40px"><h3>Organic after</h3></a></div>
          <div data-text-ad style="position:absolute;top:20px"><span>Sponsored</span><a href="https://www.google.com/goto?url=ad" style="display:block;width:400px;height:40px"><h3>Sponsored result</h3></a></div>
          <div style="position:absolute;top:40px"><a href="https://one.example" style="display:block;width:400px;height:40px"><h3>Organic one</h3></a></div>
          <div style="position:absolute;top:60px"><a href="https://two.example" style="display:block;width:400px;height:40px"><h3>Organic two</h3></a></div>
          <div style="display:none"><a href="https://hidden.example"><h3>Hidden result</h3></a></div>
          <div style="position:absolute;top:220px">
            <a href="https://duplicate.example" style="display:block;width:400px;height:40px"><h3>Duplicate result</h3></a>
            <a href="https://duplicate.example" style="display:block;width:400px;height:40px"><h3>Duplicate result</h3></a>
          </div>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup)
                class StubResolver:
                    def resolve(self, url):
                        return "https://ad.example" if "goto" in url else url

                structure = _serp_structure(page, object(), resolver=StubResolver())
            finally:
                browser.close()

        self.assertEqual(list(range(1, 7)), [entry["rank"] for entry in structure])
        self.assertEqual(
            ["Sponsored", "Organic", "Organic", "AI Overview", "Organic", "Organic"],
            [entry["type"] for entry in structure],
        )
        self.assertEqual("https://ad.example", structure[0]["url"])
        self.assertTrue(structure[0]["sponsored"])
        self.assertEqual("AI Overview", structure[3]["type"])
        self.assertNotIn("headline", structure[3])
        headlines = [entry.get("headline") for entry in structure]
        self.assertNotIn("AIO citation", headlines)
        self.assertNotIn("Hidden result", headlines)
        self.assertEqual(1, headlines.count("Duplicate result"))

    def test_serp_structure_without_aio_contains_only_results(self):
        from playwright.sync_api import sync_playwright

        markup = '<div id="search"><a href="https://example.com" style="display:block;width:400px;height:40px"><h3>Only result</h3></a></div>'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup)
                structure = _serp_structure(page, object())
            finally:
                browser.close()
        self.assertEqual([{
            "rank": 1,
            "type": "Organic",
            "headline": "Only result",
            "url": "https://example.com/",
            "raw_url": "https://example.com/",
            "sponsored": False,
        }], structure)

    def test_blue_link_dom_detection_excludes_aio_and_marks_sponsored(self):
        from playwright.sync_api import sync_playwright

        markup = """
        <style>a { display: block; height: 40px; }</style>
        <div id="search">
          <div jsname="KFl8ub"><a href="https://aio.example/source"><h3>AIO source</h3></a></div>
          <div><a href="https://organic.example/page"><h3>Organic result</h3></a></div>
          <div data-text-ad><span>Sponsored</span><a href="https://ad.example/page"><h3>Data ad</h3></a></div>
          <section><span>  AD  </span><a href="https://label-ad.example/page"><h3>Label ad</h3></a></section>
        </div>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(markup)
                candidates = _blue_link_candidates(page)
            finally:
                browser.close()

        self.assertEqual(
            ["Organic result", "Data ad", "Label ad"],
            [candidate["headline"] for candidate in candidates],
        )
        self.assertEqual([False, True, True], [candidate["sponsored"] for candidate in candidates])
        self.assertNotIn("AIO source", [candidate["headline"] for candidate in candidates])

    def test_blue_link_destination_resolution_and_fallback(self):
        class FakePage:
            def __init__(self, final_url):
                self.url = "about:blank"
                self.final_url = final_url
                self.closed = False

            def goto(self, *_args, **_kwargs):
                self.url = self.final_url

            def wait_for_timeout(self, _milliseconds):
                pass

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self, final_url):
                self.page = FakePage(final_url)
                self.calls = 0

            def new_page(self):
                self.calls += 1
                return self.page

        external_context = FakeContext("https://unused.example")
        external = "https://example.com/page"
        self.assertEqual(external, _resolve_destination(external_context, external))
        self.assertEqual(0, external_context.calls)

        wrapper = "https://www.google.com/goto?url=opaque"
        resolved_context = FakeContext("https://destination.example/page")
        self.assertEqual(
            "https://destination.example/page",
            _resolve_destination(resolved_context, wrapper),
        )
        self.assertTrue(resolved_context.page.closed)

        fallback_context = FakeContext(wrapper)
        self.assertEqual(wrapper, _resolve_destination(fallback_context, wrapper, timeout=0))
        self.assertTrue(fallback_context.page.closed)

    def test_resolver_skips_external_urls_reuses_one_page_and_caches_wrappers(self):
        class FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.goto_calls = []
                self.closed = False

            def is_closed(self):
                return self.closed

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                token = url.rsplit("=", 1)[-1]
                self.url = f"https://destination.example/{token}"

            def wait_for_timeout(self, _milliseconds):
                pass

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.page = FakePage()
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                return self.page

        context = FakeContext()
        resolver = DestinationURLResolver(context)
        self.assertEqual("https://external.example/page", resolver.resolve("https://external.example/page"))
        first = resolver.resolve("/goto?url=one")
        repeated = resolver.resolve("/goto?url=one")
        second = resolver.resolve("https://www.google.com/goto?url=two")

        self.assertEqual("https://destination.example/one", first)
        self.assertEqual(first, repeated)
        self.assertEqual("https://destination.example/two", second)
        self.assertEqual(1, context.new_page_calls)
        self.assertEqual(2, len(context.page.goto_calls))
        self.assertEqual(2, resolver.navigation_count)
        self.assertEqual(1, resolver.cache_hits)

    def test_only_top_three_structure_results_are_resolved(self):
        class FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.goto_calls = []

            def is_closed(self):
                return False

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                self.url = f"https://resolved.example/{len(self.goto_calls)}"

            def wait_for_timeout(self, _milliseconds):
                pass

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        structure = [{
            "rank": number,
            "type": "Organic",
            "headline": f"Result {number}",
            "url": f"https://www.google.com/goto?url={number}",
            "raw_url": f"https://www.google.com/goto?url={number}",
            "sponsored": False,
        } for number in range(1, 7)]
        context = FakeContext()
        resolver = DestinationURLResolver(context)
        resolved = _resolve_structure_urls(structure, resolver, limit=3)

        self.assertEqual(3, resolver.navigation_count)
        self.assertEqual(3, len(context.page.goto_calls))
        self.assertTrue(all(item["url"].startswith("https://resolved.example/") for item in resolved[:3]))
        self.assertTrue(all(item["url"] == item["raw_url"] for item in resolved[3:]))

    def test_collector_uses_one_serp_navigation_and_one_snapshot(self):
        from unittest.mock import patch
        from air.collector import GoogleAIOverviewCollector

        class FakeLocator:
            def __init__(self, kind):
                self.kind = kind

            @property
            def first(self):
                return self

            def count(self):
                return 1

            def is_visible(self):
                return True

            def inner_text(self, **_kwargs):
                return "ordinary results" if self.kind == "body" else "AI answer"

            def locator(self, _selector):
                return self

            def evaluate_all(self, _script):
                return []

        class FakePage:
            url = "about:blank"

            def __init__(self):
                self.goto_calls = []
                self.wait_calls = []

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)
                self.url = url

            def locator(self, selector):
                return FakeLocator("body" if selector == "body" else "overview")

            def wait_for_timeout(self, _milliseconds):
                self.wait_calls.append(_milliseconds)

            def close(self):
                pass

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        snapshot = {
            "major_blocks": [
                {"type": "AI Overview", "y": 10},
                {
                    "type": "Organic", "headline": "External", "y": 20,
                    "raw_url": "https://external.example", "sponsored": False,
                },
            ],
            "blue_links": [],
            "aio_present": True,
            "aio_ads": [],
        }
        collector = GoogleAIOverviewCollector(
            use_cdp=False,
            resolve_top_links_only=True,
            screenshot_delay_min=5,
            screenshot_delay_max=10,
        )
        context = FakeContext()
        with patch("air.collector._serp_snapshot", return_value=snapshot) as snapshot_call, \
                patch("air.collector.random.uniform", return_value=7.5), \
                patch("air.collector.save_qc_screenshot", return_value=True) as screenshot:
            result = collector._collect_in_context(context, "query", screenshot_path="qc.png")

        self.assertEqual("Success", result["status"])
        self.assertEqual(1, len(context.page.goto_calls))
        self.assertEqual(1, snapshot_call.call_count)
        self.assertEqual({
            "google_serp_navigations": 1,
            "external_url_resolution_navigations": 0,
            "resolution_cache_hits": 0,
        }, result["navigation_metrics"])
        self.assertEqual([7500], context.page.wait_calls)
        screenshot.assert_called_once_with(context.page, "qc.png")

    def test_top_blue_links_preserve_order_limit_and_raw_url(self):
        from unittest.mock import patch

        candidates = [{
            "headline": f"Result {index}",
            "raw_url": f"https://www.google.com/goto?url={index}",
            "sponsored": index == 1,
        } for index in range(1, 5)]
        structure = [
            {
                "rank": index,
                "type": "Sponsored" if candidate["sponsored"] else "Organic",
                "headline": candidate["headline"],
                "url": candidate["raw_url"].replace(
                    "https://www.google.com/goto?url=", "https://example.com/"
                ),
                "raw_url": candidate["raw_url"],
                "sponsored": candidate["sponsored"],
            }
            for index, candidate in enumerate(candidates, start=1)
        ]
        links = _top_blue_links_from_structure(structure)

        self.assertEqual(3, len(links))
        self.assertEqual(["Result 1", "Result 2", "Result 3"], [link["headline"] for link in links])
        self.assertEqual("https://example.com/1", links[0]["url"])
        self.assertEqual("https://www.google.com/goto?url=1", links[0]["raw_url"])
        self.assertTrue(links[0]["sponsored"])

    def test_local_runner_uses_local_browser_mode_and_writes_outputs(self):
        from unittest.mock import patch

        prompt_path = Path(self.temp.name) / "prompts.xlsx"
        prompt_book = Workbook()
        prompt_book.active.append(["Prompt"])
        prompt_book.active.append(["one"])
        prompt_book.active.append(["two"])
        prompt_book.save(prompt_path)

        collectors = []
        screenshot_paths = []
        lifecycle = []

        class FakeCollector:
            def __init__(self, **kwargs):
                collectors.append(kwargs)

            def start(self):
                lifecycle.append("start")

            def close(self):
                lifecycle.append("close")

            def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
                screenshot_paths.append(Path(screenshot_path))
                Path(screenshot_path).write_bytes(b"test-png")
                return {
                    "status": "Success", "response": f"answer: {query}",
                    "parsed_json": "{}", "top_blue_links": "[]",
                    "google_blocked": False,
                    "execution_time": 0.1,
                }

        output_dir = Path(self.temp.name) / "local-output"
        profile_dir = Path(self.temp.name) / "persistent-profile"
        from air.excel_output import write_results as real_write_results
        with patch("air.local_runner.GoogleAIOverviewCollector", FakeCollector), \
                patch("air.local_runner.write_results", wraps=real_write_results) as checkpoint_write:
            combined = run_local(
                prompt_path,
                output_dir,
                delay_min=0,
                delay_max=0,
                break_every=0,
                profile_dir=profile_dir,
                chrome_executable=self.chrome_executable,
            )

        self.assertEqual(
            [{
                "headless": False,
                "use_cdp": False,
                "user_data_dir": str(profile_dir),
                "manual_captcha_timeout": 0,
                "executable_path": str(self.chrome_executable),
                "resolve_top_links_only": True,
                "screenshot_delay_min": 5,
                "screenshot_delay_max": 10,
            }],
            collectors,
        )
        self.assertEqual(["start", "close"], lifecycle)
        self.assertEqual(4, checkpoint_write.call_count)
        self.assertTrue(combined.is_file())
        self.assertRegex(combined.name, r"^amex_air_all_batches_\d{8}_\d{6}_\d{6}\.xlsx$")
        batches = list(output_dir.glob("amex_air_batch_1_*.xlsx"))
        self.assertEqual(1, len(batches))
        result_book = load_workbook(combined, read_only=True, data_only=True)
        self.assertEqual(
            (
                "Prompt", "Status", "Response", "Parsed JSON", "Top 3 Blue Links",
                "Execution Time (sec)", "SERP First Element", "AI Overview Position",
                "AI Overview On Top", "SERP Order JSON", "AIO Ad Present", "AIO Ad Count",
                "AIO Ads JSON", "Sponsored Ads JSON", "Sponsored Ad Count",
                "AMEX Sponsored Ad Present", "AMEX Sponsored Ad Rank", "AMEX SERP Rank",
                "Brands in Sponsored Ads", "AMEX Ad Competitive Position",
            ),
            tuple(cell.value for cell in result_book["Responses"][1]),
        )
        self.assertEqual(3, result_book["Responses"].max_row)
        result_book.close()
        self.assertEqual(2, len(screenshot_paths))
        self.assertTrue(all(path.is_file() for path in screenshot_paths))
        screenshot_dirs = list((output_dir / "screenshots").iterdir())
        self.assertEqual(1, len(screenshot_dirs))
        self.assertRegex(screenshot_dirs[0].name, r"^\d{8}_\d{6}$")
        manifest = json.loads((screenshot_dirs[0] / "screenshot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({"one.png": "one", "two.png": "two"}, manifest)

    def test_chrome_executable_discovery_uses_required_fallback_order(self):
        from unittest.mock import patch

        local_root = Path(self.temp.name) / "LocalAppData"
        candidates = chrome_executable_candidates(local_root)
        self.assertEqual(
            [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                local_root / "Google" / "Chrome" / "Application" / "chrome.exe",
            ],
            candidates,
        )

        for expected in candidates:
            with self.subTest(expected=expected), patch.object(
                Path,
                "is_file",
                autospec=True,
                side_effect=lambda path, selected=expected: path == selected,
            ), patch.object(Path, "resolve", autospec=True, side_effect=lambda path: path):
                self.assertEqual(expected, find_chrome_executable(local_root))

    def test_chrome_discovery_failure_lists_checked_locations(self):
        from unittest.mock import patch

        local_root = Path(self.temp.name) / "LocalAppData"
        with patch.object(Path, "is_file", autospec=True, return_value=False):
            with self.assertRaisesRegex(FileNotFoundError, "Google Chrome could not be found") as raised:
                find_chrome_executable(local_root)
        self.assertIn("Program Files", str(raised.exception))
        self.assertIn(str(local_root), str(raised.exception))

    def test_local_runner_uses_configured_pacing_and_long_break(self):
        from unittest.mock import patch

        prompt_path = Path(self.temp.name) / "pacing.txt"
        prompt_path.write_text("\n".join(f"query {number}" for number in range(1, 12)), encoding="utf-8")

        class FakeCollector:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
                return {
                    "status": "Success", "response": query, "parsed_json": "{}",
                    "top_blue_links": "[]", "google_blocked": False,
                    "execution_time": 0.01,
                }

        settings = {
            "AIR_LOCAL_MIN_DELAY": "25", "AIR_LOCAL_MAX_DELAY": "45",
            "AIR_LOCAL_BREAK_EVERY": "10", "AIR_LOCAL_BREAK_MIN": "180",
            "AIR_LOCAL_BREAK_MAX": "300",
        }
        with patch("air.local_runner.GoogleAIOverviewCollector", FakeCollector), \
                patch("air.local_runner.random.uniform", side_effect=lambda low, high: (low + high) / 2) as uniform, \
                patch("air.local_runner.time.sleep") as sleep, \
                patch.dict("os.environ", settings):
            run_local(
                prompt_path,
                Path(self.temp.name) / "pacing-output",
                profile_dir=Path(self.temp.name) / "profile",
                chrome_executable=self.chrome_executable,
            )

        self.assertEqual(10, uniform.call_count)
        self.assertEqual([(25.0, 45.0)] * 9, [item.args for item in uniform.call_args_list[:9]])
        self.assertEqual((180.0, 300.0), uniform.call_args_list[9].args)
        self.assertEqual([35.0] * 9 + [240.0], [item.args[0] for item in sleep.call_args_list])

    def test_local_captcha_retries_same_prompt_once_then_continues(self):
        from unittest.mock import patch

        prompt_path = Path(self.temp.name) / "retry.txt"
        prompt_path.write_text("first\nsecond\n", encoding="utf-8")
        calls = []
        lifecycle = []
        outcomes = [True, False, False]

        class FakeCollector:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                lifecycle.append("start")

            def close(self):
                lifecycle.append("close")

            def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
                calls.append(query)
                Path(screenshot_path).write_bytes(b"png")
                blocked = outcomes.pop(0)
                if blocked:
                    Path(captcha_screenshot_path).write_bytes(b"captcha")
                return {
                    "status": "Failed" if blocked else "Success",
                    "response": "blocked" if blocked else f"answer: {query}",
                    "parsed_json": "", "top_blue_links": "[]",
                    "google_blocked": blocked, "execution_time": 0.01,
                }

        with patch("air.local_runner.GoogleAIOverviewCollector", FakeCollector), \
                patch("air.local_runner.time.sleep") as sleep:
            combined = run_local(
                prompt_path,
                Path(self.temp.name) / "retry-output",
                delay_min=0,
                delay_max=0,
                break_every=0,
                captcha_wait_seconds=60,
                profile_dir=Path(self.temp.name) / "profile",
                chrome_executable=self.chrome_executable,
            )

        self.assertEqual(["first", "first", "second"], calls)
        self.assertEqual(["start", "close"], lifecycle)
        sleep.assert_called_once_with(60)
        workbook = load_workbook(combined, read_only=True, data_only=True)
        self.assertEqual(["Success", "Success"], [workbook["Responses"].cell(row, 2).value for row in (2, 3)])
        workbook.close()
        screenshot_dir = next((Path(self.temp.name) / "retry-output" / "screenshots").iterdir())
        normal_screenshots = [
            path for path in screenshot_dir.glob("*.png")
            if not path.name.startswith("CAPTCHA_")
        ]
        self.assertEqual(["first.png", "second.png"], sorted(path.name for path in normal_screenshots))

    def test_persistent_captcha_stops_and_saves_partial_output_and_screenshots(self):
        from unittest.mock import patch

        prompt_path = Path(self.temp.name) / "stop.txt"
        prompt_path.write_text("completed\nblocked\nnever run\n", encoding="utf-8")
        calls = []
        outcomes = [False, True, True]

        class FakeCollector:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
                calls.append(query)
                Path(screenshot_path).write_bytes(b"png")
                blocked = outcomes.pop(0)
                if blocked:
                    Path(captcha_screenshot_path).write_bytes(b"captcha")
                return {
                    "status": "Failed" if blocked else "Success",
                    "response": "Google CAPTCHA" if blocked else "answer",
                    "parsed_json": "", "top_blue_links": "[]",
                    "google_blocked": blocked, "execution_time": 0.01,
                }

        output_dir = Path(self.temp.name) / "stop-output"
        with patch("air.local_runner.GoogleAIOverviewCollector", FakeCollector), \
                patch("air.local_runner.time.sleep") as sleep:
            combined = run_local(
                prompt_path,
                output_dir,
                delay_min=0,
                delay_max=0,
                break_every=0,
                captcha_wait_seconds=60,
                profile_dir=Path(self.temp.name) / "profile",
                chrome_executable=self.chrome_executable,
            )

        self.assertEqual(["completed", "blocked", "blocked"], calls)
        sleep.assert_called_once_with(60)
        workbook = load_workbook(combined, read_only=True, data_only=True)
        sheet = workbook["Responses"]
        self.assertEqual(3, sheet.max_row)
        self.assertEqual("completed", sheet.cell(2, 1).value)
        self.assertEqual("blocked", sheet.cell(3, 1).value)
        self.assertEqual("Failed", sheet.cell(3, 2).value)
        workbook.close()
        screenshot_dir = next((output_dir / "screenshots").iterdir())
        captcha_files = list(screenshot_dir.glob("CAPTCHA_*.png"))
        self.assertEqual(2, len(captcha_files))
        manifest = json.loads((screenshot_dir / "screenshot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len([name for name in manifest if name.startswith("CAPTCHA_")]))

    def test_local_profile_path_is_reused_across_runs(self):
        from unittest.mock import patch

        prompt_path = Path(self.temp.name) / "profile.txt"
        prompt_path.write_text("one\n", encoding="utf-8")
        profile_dir = Path(self.temp.name) / "shared-profile"
        profiles = []

        class FakeCollector:
            def __init__(self, **kwargs):
                profiles.append(kwargs["user_data_dir"])

            def start(self):
                pass

            def close(self):
                pass

            def collect(self, query, screenshot_path=None, captcha_screenshot_path=None):
                return {
                    "status": "Success", "response": query, "parsed_json": "{}",
                    "top_blue_links": "[]", "google_blocked": False,
                    "execution_time": 0.01,
                }

        with patch("air.local_runner.GoogleAIOverviewCollector", FakeCollector):
            for run_number in (1, 2):
                run_local(
                    prompt_path,
                    Path(self.temp.name) / f"profile-output-{run_number}",
                    delay_min=0,
                    delay_max=0,
                    break_every=0,
                    profile_dir=profile_dir,
                    chrome_executable=self.chrome_executable,
                )
        self.assertEqual([str(profile_dir), str(profile_dir)], profiles)
        self.assertTrue(profile_dir.is_dir())

    def test_screenshot_filenames_folders_duplicates_and_manifest(self):
        root = Path(self.temp.name) / "qc-output"
        screenshots = ScreenshotRun(root, timestamp="20260910_194500")
        self.assertEqual(root / "screenshots" / "20260910_194500", screenshots.directory)

        unsafe_prompt = ' best card: Amex / Chase? * "offer" <now> |. '
        first = screenshots.path_for(unsafe_prompt)
        duplicate = screenshots.path_for(unsafe_prompt)
        case_duplicate = screenshots.path_for("BEST CARD AMEX CHASE OFFER NOW")
        long_path = screenshots.path_for("x" * 500)

        self.assertEqual("best card Amex Chase offer now.png", first.name)
        self.assertEqual("best card Amex Chase offer now_2.png", duplicate.name)
        self.assertEqual("BEST CARD AMEX CHASE OFFER NOW_3.png", case_duplicate.name)
        self.assertLessEqual(len(long_path.name), 180)
        self.assertFalse(first.name.endswith(". .png"))

        first.write_bytes(b"png")
        screenshots.record(first, unsafe_prompt)
        manifest = json.loads((screenshots.directory / "screenshot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(unsafe_prompt, manifest[first.name])

        second_run = ScreenshotRun(root, timestamp="20260910_194500")
        self.assertEqual("20260910_194500_2", second_run.directory.name)

    def test_screenshot_sanitizer_handles_empty_reserved_and_trailing_periods(self):
        self.assertEqual("prompt", sanitize_screenshot_stem('\\/:*?"<>|'))
        self.assertEqual("_CON", sanitize_screenshot_stem("CON"))
        self.assertEqual("trailing", sanitize_screenshot_stem(" trailing... "))

    def test_screenshot_failure_does_not_raise(self):
        class BrokenPage:
            def screenshot(self, **_kwargs):
                raise RuntimeError("capture failed")

        path = Path(self.temp.name) / "unavailable.png"
        self.assertFalse(save_qc_screenshot(BrokenPage(), path))
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
