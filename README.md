# AMEX AIR

AMEX AIR (AI Insights & Responses) is an independent web application for queueing Google AI Overview query batches and exporting results to Excel.

## User flow

1. A user uploads `.xlsx`, `.csv`, or `.txt` with up to 500 unique queries.
2. The hosted application stores the request in a durable database and shows it under **Request status**.
3. The laptop processing engine claims queued requests whenever it is online.
4. Progress is sent back to the website after every query.
5. AIR processes at most 35 queries per batch. Larger uploads are split automatically.
6. Every terminal path provides a combined workbook for all processed rows plus a separate workbook for each processed batch.
7. If a request is stopped, paused by persistent CAPTCHA, times out, or encounters a worker error, the available partial results remain downloadable.

Request history is associated with a random identity stored in that browser. It is not an account system, so clearing browser storage or using another browser/device will not show the same history.

## Laptop processing engine

The laptop needs Python dependencies and Playwright Chromium once:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Copy `.env.example` to `.env`, configure the same `AIR_WORKER_TOKEN` used by the hosted service, then start:

```powershell
.\start_laptop_worker.ps1
```

The checked-in launcher reads `.env` and runs `python -m air.remote_worker`. The laptop only makes outbound HTTPS requests; no router port or inbound connection is required. When the laptop is off, requests remain queued. When `AIR_CDP_URL=http://127.0.0.1:9222` is configured, the worker uses the existing YesJohnny-compatible Chrome session. If that Chrome session is unavailable, the worker stays offline and leaves requests safely queued.

## Hosted application

The hosted service requires:

- `DATABASE_URL`: PostgreSQL connection string.
- `AIR_WORKER_TOKEN`: a long private random value shared only with the laptop worker.

The web container does not install or run Chromium. `render.yaml` declares the web service and Postgres dependency.

## Inputs and output

- `.xlsx` or `.csv`: a `Prompt` column is preferred; otherwise the first column is used.
- `.txt`: one query per line.
- Maximum 500 unique queries and 25 MB per upload; processing batches contain at most 35 queries.
- Output keeps the `Responses` worksheet and adds `Top 3 Blue Links`, containing headline and sponsored status without changing `Parsed JSON`.
- Completed uploads provide `amex_air_batch_1.xlsx`, subsequent batch files as needed, and `amex_air_all_batches.xlsx`.

## Tests

```powershell
python -m unittest discover -s tests -v
node --check static\air.js
```

## Operational notes

Google can vary AI Overviews by query, region, account state, timing, and automated-access controls. AMEX AIR does not bypass CAPTCHAs. The laptop worker spaces searches by a random 10-15 seconds, rests for 90 seconds after every 20 queries, and pauses for 30 minutes before each of two retries when Google displays an unusual-traffic or CAPTCHA block. If the block remains after both retries, AIR ends that request and publishes the partial workbooks. The UI shows a small live countdown for cooldown periods. These values can be adjusted with the `AIR_QUERY_DELAY_MIN_SECONDS`, `AIR_QUERY_DELAY_MAX_SECONDS`, `AIR_BATCH_REST_EVERY`, `AIR_BATCH_REST_SECONDS`, `AIR_CAPTCHA_RETRIES`, and `AIR_CAPTCHA_COOLDOWN_SECONDS` environment variables.

Render free Postgres databases expire after 30 days. Upgrade or migrate the database before its expiry to keep queued jobs, history, and completed workbooks permanently.
