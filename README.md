# AMEX AIR

AMEX AIR (AI Insights & Responses) is an independent web application for collecting Google AI Overview responses from uploaded query batches and exporting the results to Excel.

## Inputs

- `.xlsx` or `.csv`: a `Prompt` column is preferred; otherwise the first column is used.
- `.txt`: one query per line.
- Maximum 500 unique queries and 25 MB per upload.

## Local setup

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

Open `http://127.0.0.1:5001`.

## Container hosting

The Dockerfile installs Chromium and all required Linux dependencies. `render.yaml` defines a Docker-based Render service. A paid/starter instance is recommended because Chromium generally exceeds free-tier memory during concurrent web and worker activity.

## Important operational note

Google may vary AI Overviews by query, region, account state, timing, and automated-access controls. AMEX AIR does not bypass CAPTCHAs or access restrictions; it records these as understandable failed rows.
