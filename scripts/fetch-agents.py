import json
import os
from pathlib import Path

import requests

# The backoffice catalog is read through the Gluesync Courier inbound REST API (reachable from the HQ runners only).
DEFAULT_COURIER_BACKOFFICE_URL = "https://gluesync.hq.molo17.com/api-inbound/backoffice"
# Courier serves at most 1000 rows per request (and only 20 without _limit)
PAGE_SIZE = 1000

# Courier returns every value as a string. These columns go back to the JSON types the Budibase search used to
# return, so agents.json keeps its layout: bit columns as booleans and the id as a number. Everything else stays a
# string (Budibase also returned the bigint and date columns as strings).
BOOL_COLUMNS = {"isTarget", "isSource", "availableGA", "availableBeta", "availableAlpha", "deleted"}
INT_COLUMNS = {"id"}


def _required_env(name):
    """Return the value of a required environment variable, exiting when it is missing."""
    value = os.getenv(name)
    if not value:
        print(f"Error: {name} environment variable is required")
        exit(1)
    return value


def _rename_module_type(items):
    """Replace each item's 'type' with its 'moduleType' (a 'type' without 'moduleType' is dropped)."""
    for item in items:
        if 'type' in item:
            del item['type']
        if 'moduleType' in item:
            item['type'] = item['moduleType']
            del item['moduleType']


def _typed_row(row):
    """A Courier row with the JSON types of a Budibase row; null columns are left out, as Budibase did."""
    typed = {}
    for column, value in row.items():
        if value is None:
            continue
        if column in BOOL_COLUMNS:
            value = str(value).strip().lower() in ("true", "1")
        elif column in INT_COLUMNS:
            value = int(value)
        typed[column] = value
    return typed


def _fetch_rows(base_url, token):
    """Every AvailableAgents row (soft-deleted ones included, as the Budibase search returned them)."""
    headers = {
        'Authorization': f"Bearer {token}",
        'Accept': 'application/json'
    }
    rows = []
    offset = 0
    while True:
        response = requests.get(f"{base_url}/available-agents", headers=headers,
                                params={"_limit": PAGE_SIZE, "_offset": offset}, timeout=60)
        if response.status_code != 200:
            print("Failed to fetch data: Status code", response.status_code)
            exit(1)
        page = response.json().get("rows") or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def fetch_and_save_data():
    token = _required_env('COURIER_BACKOFFICE_TOKEN')
    base_url = os.getenv('COURIER_BACKOFFICE_URL') or DEFAULT_COURIER_BACKOFFICE_URL

    # Courier does not sort: keep the id order of the Budibase search
    rows = sorted((_typed_row(row) for row in _fetch_rows(base_url.rstrip('/'), token)), key=lambda row: row['id'])
    _rename_module_type(rows)
    # Same layout as the Budibase search response that agents.json used to be
    data = {"data": rows, "hasNextPage": False}

    # Save to agents.json in the project root
    output_path = Path(__file__).resolve().parent.parent / "agents.json"
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"{len(rows)} agents saved to {output_path} with 'moduleType' renamed to 'type'")

# Run the function
if __name__ == '__main__':
    fetch_and_save_data()
