import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook, load_workbook

import app as air_app
from air.inputs import InputError, read_queries
from air.worker import main as run_worker


class AirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_output = air_app.OUTPUT_ROOT
        air_app.OUTPUT_ROOT = Path(self.temp.name) / "output"
        self.app = air_app.create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        air_app.OUTPUT_ROOT = self.original_output
        self.temp.cleanup()

    def test_brand_and_upload_interface(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn(b"AMEX AIR", response.data)
        self.assertIn(b"AI Insights &amp; Responses", response.data)
        self.assertIn(b".xlsx,.csv,.txt", response.data)

    def test_all_supported_input_formats(self):
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
        with self.assertRaises(InputError):
            read_queries("q.pdf", b"queries")

    @patch("app.subprocess.Popen")
    def test_job_upload_is_queued(self, popen):
        popen.return_value = MagicMock()
        response = self.client.post(
            "/jobs",
            data={"file": (io.BytesIO(b"first query\nsecond query"), "queries.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(202, response.status_code, response.get_data(as_text=True))
        payload = response.get_json()
        status = self.client.get(payload["status_url"]).get_json()
        self.assertEqual("queued", status["state"])
        self.assertEqual(2, status["total"])

    def test_missing_job_explains_restart_and_is_retryable(self):
        run_id = "a" * air_app.RUN_ID_LENGTH
        response = self.client.get(f"/jobs/{run_id}")
        self.assertEqual(404, response.status_code)
        payload = response.get_json()
        self.assertEqual("job_state_lost", payload["code"])
        self.assertTrue(payload["retryable"])
        self.assertIn("restarted", payload["error"])

    def test_worker_creates_expected_excel(self):
        job_root = Path(self.temp.name) / "job"
        job_root.mkdir()
        (job_root / "job.json").write_text(json.dumps({"queries": ["one", "two"]}), encoding="utf-8")

        class FakeCollector:
            def collect(self, query):
                return {"status": "Success" if query == "one" else "Failed", "response": f"result: {query}", "parsed_json": "{}" if query == "one" else "", "execution_time": 0.1}

        with patch("air.worker.GoogleAIOverviewCollector", FakeCollector), patch.dict("os.environ", {"AIR_QUERY_DELAY_SECONDS": "0"}):
            run_worker(job_root)

        status = json.loads((job_root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", status["state"])
        self.assertEqual(1, status["success_count"])
        self.assertEqual(1, status["failed_count"])
        workbook = load_workbook(job_root / "amex_air_results.xlsx", read_only=True, data_only=True)
        sheet = workbook["Responses"]
        self.assertEqual(("Prompt", "Status", "Response", "Parsed JSON", "Execution Time (sec)"), tuple(cell.value for cell in sheet[1]))
        self.assertEqual(3, sheet.max_row)
        workbook.close()


if __name__ == "__main__":
    unittest.main()
