from __future__ import annotations

from pathlib import Path
from io import BytesIO
import json
from urllib.parse import urlparse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo


def _is_amex_url(url):
    try:
        hostname = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname == "americanexpress.com" or hostname.endswith(".americanexpress.com")


def _amex_blue_link_rank(row):
    try:
        structure = json.loads(row.get("serp_order_json") or "[]")
        top_links = json.loads(row.get("top_blue_links") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    resolved_by_raw = {
        str(link.get("raw_url") or ""): str(link.get("url") or "")
        for link in top_links
        if isinstance(link, dict)
    }
    for entry in structure:
        if not isinstance(entry, dict) or entry.get("type") != "Organic":
            continue
        raw_url = str(entry.get("raw_url") or "")
        urls = (entry.get("url"), raw_url, resolved_by_raw.get(raw_url))
        if any(_is_amex_url(url) for url in urls):
            return entry.get("rank")
    return None


def build_results(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Responses"
    headers = [
        "Prompt", "Status", "Response", "Parsed JSON", "Top 3 Blue Links",
        "Execution Time (sec)", "SERP First Element", "AI Overview Position",
        "AI Overview On Top", "SERP Order JSON", "AIO Ad Present", "AIO Ad Count",
        "AIO Ads JSON", "Sponsored Ads JSON", "Sponsored Ad Count",
        "AMEX Sponsored Ad Present", "AMEX Sponsored Ad Rank", "AMEX SERP Rank",
        "Brands in Sponsored Ads", "AMEX Ad Competitive Position",
    ]
    sheet.append(headers)
    for row in rows:
        sheet.append([
            row["prompt"], row["status"], row["response"], row["parsed_json"],
            row.get("top_blue_links", "[]"), row["execution_time"],
            row.get("serp_first_element", ""), row.get("ai_overview_position"),
            row.get("ai_overview_on_top"), row.get("serp_order_json", "[]"),
            row.get("aio_ad_present"), row.get("aio_ad_count", 0),
            row.get("aio_ads_json", "[]"), row.get("sponsored_ads_json", "[]"),
            row.get("sponsored_ad_count", 0),
            row.get("amex_sponsored_ad_present", False), row.get("amex_sponsored_ad_rank"),
            _amex_blue_link_rank(row), row.get("brands_in_sponsored_ads", ""),
            row.get("amex_ad_competitive_position", "No Sponsored Ads"),
        ])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="006FCF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for column, width in {
        "A": 42, "B": 12, "C": 90, "D": 100, "E": 80, "F": 20,
        "G": 22, "H": 22, "I": 22, "J": 100, "K": 18, "L": 16,
        "M": 100, "N": 100, "O": 20, "P": 26, "Q": 24, "R": 18,
        "S": 38, "T": 30,
    }.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    if rows:
        table = Table(displayName="AirResponses", ref=f"A1:T{len(rows) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)
    return workbook


def write_results(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    build_results(rows).save(path)


def results_bytes(rows):
    stream = BytesIO()
    build_results(rows).save(stream)
    return stream.getvalue()
