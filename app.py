from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from air.inputs import InputError, read_queries

ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.getenv("AIR_OUTPUT_ROOT", ROOT / "output"))
RUN_ID_LENGTH = 32
PROCESSES = {}
LOCK = threading.Lock()


def atomic_json(path, payload):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def valid_root(run_id):
    if len(run_id) != RUN_ID_LENGTH or any(c not in "0123456789abcdef" for c in run_id):
        abort(404)
    return OUTPUT_ROOT / run_id


def cleanup():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    if not OUTPUT_ROOT.exists():
        return
    with LOCK:
        active = {key for key, process in PROCESSES.items() if process.poll() is None}
    for directory in OUTPUT_ROOT.iterdir():
        if directory.is_dir() and directory.name not in active:
            modified = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                shutil.rmtree(directory, ignore_errors=True)


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=25 * 1024 * 1024, TESTING=False)
    if test_config:
        app.config.update(test_config)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/jobs")
    def start_job():
        cleanup()
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify(error="Choose an Excel, CSV, or text file containing queries."), 400
        try:
            queries = read_queries(upload.filename, upload.read())
        except (InputError, UnicodeDecodeError, OSError) as error:
            return jsonify(error=str(error)), 400
        run_id = uuid.uuid4().hex
        job_root = OUTPUT_ROOT / run_id
        job_root.mkdir(parents=True)
        atomic_json(job_root / "job.json", {"run_id": run_id, "filename": upload.filename, "queries": queries})
        atomic_json(job_root / "status.json", {"state": "queued", "completed": 0, "total": len(queries), "message": "Preparing hosted browser..."})
        log = (job_root / "worker.log").open("a", encoding="utf-8")
        try:
            process = subprocess.Popen([sys.executable, "-m", "air.worker", str(job_root)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        except Exception as error:
            shutil.rmtree(job_root, ignore_errors=True)
            return jsonify(error=f"AMEX AIR could not start the worker. Reason: {type(error).__name__}: {str(error)[:300]}"), 500
        finally:
            log.close()
        with LOCK:
            PROCESSES[run_id] = process
        return jsonify(run_id=run_id, status_url=url_for("job_status", run_id=run_id), cancel_url=url_for("cancel", run_id=run_id)), 202

    @app.get("/jobs/<run_id>")
    def job_status(run_id):
        root = valid_root(run_id)
        path = root / "status.json"
        if not path.is_file():
            abort(404)
        status = json.loads(path.read_text(encoding="utf-8"))
        if status.get("state") in {"completed", "failed", "cancelled"}:
            with LOCK:
                PROCESSES.pop(run_id, None)
        if status.get("state") == "completed":
            status["download_url"] = url_for("download", run_id=run_id)
        return jsonify(status)

    @app.post("/jobs/<run_id>/cancel")
    def cancel(run_id):
        root = valid_root(run_id)
        with LOCK:
            process = PROCESSES.pop(run_id, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        output = root / "amex_air_results.xlsx"
        if output.exists():
            output.unlink()
        atomic_json(root / "status.json", {"state": "cancelled", "message": "AMEX AIR collection was stopped."})
        return jsonify(state="cancelled", message="AMEX AIR collection was stopped.")

    @app.get("/jobs/<run_id>/download")
    def download(run_id):
        path = valid_root(run_id) / "amex_air_results.xlsx"
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name="amex_air_results.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return jsonify(error="The upload exceeds the 25 MB limit."), 413

    return app


app = create_app()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, threaded=True)
