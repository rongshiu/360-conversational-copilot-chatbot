"""Make the test suite runnable on a developer machine, not just in the image.

`settings.glossary_csv_path` and `metric_csv_path` default to `/app/data/...`,
which is correct inside the container and absent on a host. The glossary service
falls back to those CSVs when the database is unreachable, so without this a host
run of `pytest` died on FileNotFoundError inside the SQL validator's column check
-- an error about pandas file handles, with nothing pointing at the real cause.

Set before `app.core.settings` is imported, since Settings reads the environment
once at construction.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"

os.environ.setdefault("GLOSSARY_CSV_PATH", str(DATA / "v3_column_glossary.csv"))
os.environ.setdefault("METRIC_CSV_PATH", str(DATA / "v3_metric_definition_seed.csv"))
