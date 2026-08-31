from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from air.collector import GoogleAIOverviewCollector
from air.excel_output import write_results


def write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(job_root):
    job_root = Path(job_root).resolve()
    status_path = job_root / "status.json"
    try:
        job = json.loads((job_root / "job.json").read_text(encoding="utf-8"))
        queries = job["queries"]
        collector = GoogleAIOverviewCollector()
        rows = []
        delay = max(0, float(os.getenv("AIR_QUERY_DELAY_SECONDS", "2")))
        for index, query in enumerate(queries, start=1):
            write_json(status_path, {"state": "running", "completed": index - 1, "total": len(queries), "current_query": query, "message": f"Collecting query {index} of {len(queries)}..."})
            result = collector.collect(query)
            rows.append({"prompt": query, **result})
            if delay and index < len(queries):
                time.sleep(delay)
        output_name = "amex_air_results.xlsx"
        write_results(job_root / output_name, rows)
        write_json(status_path, {"state": "completed", "completed": len(queries), "total": len(queries), "success_count": sum(row["status"] == "Success" for row in rows), "failed_count": sum(row["status"] == "Failed" for row in rows), "output": output_name, "message": "AMEX AIR collection complete."})
    except BaseException as error:
        (job_root / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        write_json(status_path, {"state": "failed", "error": f"AMEX AIR could not complete the collection. Reason: {type(error).__name__}: {str(error)[:400]}"})
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m air.worker <job-directory>")
    main(sys.argv[1])
