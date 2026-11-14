"""Readers for outside sources (kind: file | api). Each returns only the case merchant's rows, capped at max_rows,
as (columns, rows) with short plain cells. Nothing here writes anywhere. Errors are raised as SourceError with a
message that is safe to show (never a header value or key)."""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx

HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None   # tests inject httpx.MockTransport; None = real network
MAX_CELL = 200
MAX_COLS = 20
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class SourceError(Exception):
    pass


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def _table(records: Iterable[Any], merchant_column: str, merchant_id: str, max_rows: int) -> tuple[list[str], list[list[Any]]]:
    """Keep the merchant's records (all of them when there is no merchant column), at most max_rows."""
    picked: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if merchant_column and str(r.get(merchant_column, "")).strip() != merchant_id:
            continue
        picked.append(r)
        if len(picked) >= max_rows:
            break
    cols: list[str] = []
    for r in picked:
        for k in r:
            if k not in cols and len(cols) < MAX_COLS:
                cols.append(k)
    return [str(c)[:60] for c in cols], [[_cell(r.get(c)) for c in cols] for r in picked]


def _dig(data: Any, path: str) -> list:
    for part in [p for p in (path or "").split(".") if p]:
        if not isinstance(data, dict) or part not in data:
            raise SourceError(f"'{path}' was not found in the data")
        data = data[part]
    if not isinstance(data, list):
        raise SourceError("expected a list of records" + (f" at '{path}'" if path else ""))
    return data


# ───────────────────────────── file ─────────────────────────────

def file_path(spec, base: Path) -> Path:
    root = Path(base).resolve()
    p = (root / spec.file.path).resolve()
    if not p.is_relative_to(root):
        raise SourceError("the file must be inside the sources folder")
    return p


def read_file(spec, merchant_id: str, base: Path) -> tuple[list[str], list[list[Any]]]:
    p = file_path(spec, base)
    if not p.is_file():
        raise SourceError(f"file not found: {spec.file.path}")
    col = spec.file.merchant_column
    fmt = spec.file.format or ("json" if p.suffix.lower() == ".json" else "csv")
    if fmt == "csv":
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if col not in (reader.fieldnames or []):
                raise SourceError(f"column '{col}' is not in the file")
            return _table(reader, col, merchant_id, spec.max_rows)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SourceError(f"the file is not valid JSON ({e.msg})") from None
    return _table(_dig(data, spec.file.rows_at), col, merchant_id, spec.max_rows)


# ───────────────────────────── api ─────────────────────────────

def missing_env(spec) -> list[str]:
    """Names of ${VARS} used in an api source's headers that are not set (backend/.env is loaded into the environment)."""
    if spec.api is None:
        return []
    names = {m.group(1) for v in spec.api.headers.values() for m in ENV_REF.finditer(v)}
    return sorted(n for n in names if not os.environ.get(n))


def _expand(value: str) -> str:
    def sub(m: re.Match) -> str:
        v = os.environ.get(m.group(1))
        if not v:
            raise SourceError(f"set {m.group(1)} in backend/.env")
        return v
    return ENV_REF.sub(sub, value)


async def read_api(spec, merchant_id: str, timeout_s: float) -> tuple[list[str], list[list[Any]]]:
    url = spec.api.url.replace("{merchant_id}", quote(merchant_id, safe=""))
    headers = {k: _expand(v) for k, v in spec.api.headers.items()}
    async with httpx.AsyncClient(timeout=timeout_s, transport=HTTP_TRANSPORT, follow_redirects=False) as client:
        r = await client.get(url, headers=headers)
    if r.status_code >= 400:
        raise SourceError(f"the API answered HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise SourceError("the API reply is not JSON") from None
    return _table(_dig(data, spec.api.rows_at), spec.api.merchant_column, merchant_id, spec.max_rows)
