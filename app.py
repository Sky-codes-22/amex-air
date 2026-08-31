from __future__ import annotations

import hmac
import os
import re
import uuid
from io import BytesIO
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from air.inputs import InputError, read_queries
from air.store import JobStore

ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = f"sqlite:///{(ROOT / 'output' / 'air.db').as_posix()}"
CLIENT_ID_PATTERN = re.compile(r"^[a-f0-9]{32,64}$")
RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        TESTING=False,
        DATABASE_URL=os.getenv("DATABASE_URL", DEFAULT_DATABASE),
        AIR_WORKER_TOKEN=os.getenv("AIR_WORKER_TOKEN", ""),
    )
    if test_config:
        app.config.update(test_config)
    if app.config["DATABASE_URL"].startswith("sqlite"):
        (ROOT / "output").mkdir(parents=True, exist_ok=True)
    store = JobStore(app.config["DATABASE_URL"])
    app.extensions["air_store"] = store

    def client_id():
        value = request.headers.get("X-AIR-Client-ID", "").strip().lower()
        if not CLIENT_ID_PATTERN.fullmatch(value):
            abort(400, description="This browser does not have a valid AMEX AIR request identity.")
        return value

    def valid_run_id(run_id):
        if not RUN_ID_PATTERN.fullmatch(run_id):
            abort(404)
        return run_id

    def require_worker():
        expected = app.config.get("AIR_WORKER_TOKEN", "")
        supplied = request.headers.get("Authorization", "")
        if not expected or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
            return jsonify(error="Worker authentication failed."), 401
        return None

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/jobs")
    def jobs():
        owner = client_id()
        return jsonify(jobs=store.list_jobs(owner), worker=store.worker_status())

    @app.post("/jobs")
    def start_job():
        owner = client_id()
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify(error="Choose an Excel, CSV, or text file containing queries."), 400
        try:
            queries = read_queries(upload.filename, upload.read())
        except (InputError, UnicodeDecodeError, OSError) as error:
            return jsonify(error=str(error)), 400
        run_id = uuid.uuid4().hex
        job = store.create_job(
            run_id=run_id,
            client_id=owner,
            download_token=uuid.uuid4().hex,
            filename=upload.filename,
            queries=queries,
        )
        return jsonify(job=job, status_url=url_for("job_status", run_id=run_id), cancel_url=url_for("cancel", run_id=run_id)), 202

    @app.get("/jobs/<run_id>")
    def job_status(run_id):
        job = store.get_job(valid_run_id(run_id), client_id())
        if not job:
            abort(404)
        if job.get("download_token"):
            job["download_url"] = url_for("download", run_id=run_id, token=job.pop("download_token"))
        return jsonify(job)

    @app.post("/jobs/<run_id>/cancel")
    def cancel(run_id):
        job = store.cancel(valid_run_id(run_id), client_id())
        if not job:
            abort(404)
        return jsonify(job=job)

    @app.get("/jobs/<run_id>/download")
    def download(run_id):
        token = request.args.get("token", "")
        result = store.workbook(valid_run_id(run_id), token)
        if not result:
            abort(404)
        workbook, original_name = result
        stem = Path(original_name).stem[:80] or "results"
        return send_file(
            BytesIO(workbook),
            as_attachment=True,
            download_name=f"amex_air_{stem}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.post("/worker/heartbeat")
    def worker_heartbeat():
        failure = require_worker()
        if failure:
            return failure
        worker_id = str((request.get_json(silent=True) or {}).get("worker_id", "")).strip()[:80]
        if not worker_id:
            return jsonify(error="worker_id is required."), 400
        store.heartbeat(worker_id)
        return jsonify(status="online")

    @app.post("/worker/claim")
    def worker_claim():
        failure = require_worker()
        if failure:
            return failure
        worker_id = str((request.get_json(silent=True) or {}).get("worker_id", "")).strip()[:80]
        if not worker_id:
            return jsonify(error="worker_id is required."), 400
        store.heartbeat(worker_id)
        job = store.claim(worker_id)
        return (jsonify(job=job), 200) if job else ("", 204)

    @app.post("/worker/jobs/<run_id>/progress")
    def worker_progress(run_id):
        failure = require_worker()
        if failure:
            return failure
        data = request.get_json(silent=True) or {}
        result = store.progress(
            valid_run_id(run_id),
            str(data.get("worker_id", ""))[:80],
            completed=data.get("completed", 0),
            current_query=data.get("current_query"),
            message=data.get("message"),
        )
        if result is None:
            abort(409)
        return jsonify(result)

    @app.post("/worker/jobs/<run_id>/complete")
    def worker_complete(run_id):
        failure = require_worker()
        if failure:
            return failure
        upload = request.files.get("workbook")
        if not upload:
            return jsonify(error="A completed workbook is required."), 400
        state = store.complete(
            valid_run_id(run_id),
            request.form.get("worker_id", "")[:80],
            workbook=upload.read(),
            success_count=request.form.get("success_count", 0),
            failed_count=request.form.get("failed_count", 0),
        )
        if not state:
            abort(409)
        return jsonify(state=state)

    @app.post("/worker/jobs/<run_id>/fail")
    def worker_fail(run_id):
        failure = require_worker()
        if failure:
            return failure
        data = request.get_json(silent=True) or {}
        ok = store.fail(valid_run_id(run_id), str(data.get("worker_id", ""))[:80], data.get("error", "Unknown worker error."))
        if not ok:
            abort(409)
        return jsonify(state="failed")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return jsonify(error="The upload exceeds the 25 MB limit."), 413

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify(error=getattr(error, "description", "Invalid request.")), 400

    return app


app = create_app()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, threaded=True)