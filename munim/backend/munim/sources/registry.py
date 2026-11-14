"""Connected sources: one YAML file per system in backend/sources/ (see backend/sources/README.md).

A source says what it holds, WHEN Munim should reach it (always / complaint topic / claim / word), how to read it
(database tables, a CSV/JSON file, or a GET API) and which fixes it carries out. Munim re-reads the folder before every
case. Rules:
  * database sources: their tables are what the improvise step may read (schema card + probe allowlist).
    Turned off (enabled: false) = the AI can neither see nor query those tables.
  * file / api sources: read only when `when` matches the complaint, only the case merchant's rows, capped.
  * actions: a fix owned by a source that is turned off is refused by the Action Firewall.
The built-in known-cause checks (suspects, trail, statements) are code and read Munim's own tables directly.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from munim import config
from munim.forensics.facts import INTENT_AREA
from munim.forensics.probe import BUSINESS_TABLES
from munim.models import ActionType, ClaimKind, IntakeResult, Intent, SourceHit, SourcesReport

from . import connectors

TOPICS = {"rent": "RENT", "loan": "LOAN", "settlement": "SETTLEMENT", "travel": "TRAVEL"}
UNOWNABLE = {ActionType.ESCALATE, ActionType.NO_ACTION}       # always available: a human takes over / nothing to do
KIND_ORDER = {"database": 0, "file": 1, "api": 2}
_SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|password", re.I)


# ───────────────────────────── the file format ─────────────────────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class When(_Strict):
    always: bool = False
    topics: list[str] = []          # stored upper case: RENT | LOAN | SETTLEMENT | TRAVEL
    claims: list[str] = []          # stored upper case ClaimKind values
    words: list[str] = []           # stored lower case

    @field_validator("topics", mode="before")
    @classmethod
    def _topics(cls, v: Any) -> list[str]:
        out = []
        for t in _as_list(v):
            key = str(t).strip().lower()
            if key not in TOPICS:
                raise ValueError(f"'{t}' is not a topic (use rent, loan, settlement or travel)")
            out.append(TOPICS[key])
        return out

    @field_validator("claims", mode="before")
    @classmethod
    def _claims(cls, v: Any) -> list[str]:
        known = {k.value for k in ClaimKind}
        out = []
        for c in _as_list(v):
            key = str(c).strip().upper()
            if key not in known:
                raise ValueError(f"'{c}' is not a claim (use: {', '.join(sorted(k.lower() for k in known))})")
            out.append(key)
        return out

    @field_validator("words", mode="before")
    @classmethod
    def _words(cls, v: Any) -> list[str]:
        return [w for w in (str(x).strip().lower() for x in _as_list(v)) if w]


class TableSpec(_Strict):
    name: str
    about: str = ""

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in BUSINESS_TABLES:
            raise ValueError(f"'{v}' is not one of Munim's business tables ({', '.join(BUSINESS_TABLES)})")
        return v


class FileSpec(_Strict):
    path: str
    format: Literal["csv", "json"] | None = None      # default: from the file extension
    merchant_column: str = "merchant_id"              # empty = the whole file belongs to every merchant (rare)
    rows_at: str = ""                                 # json only: where the list of records sits ("data.items")


class ApiSpec(_Strict):
    url: str
    headers: dict[str, str] = {}
    rows_at: str = ""
    merchant_column: str = "merchant_id"

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if urlsplit(v).scheme not in ("http", "https"):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("headers")
    @classmethod
    def _no_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if _SECRET_HEADER.search(k) and not connectors.ENV_REF.search(str(val)):
                raise ValueError(f"header '{k}' looks like a secret: put the value in backend/.env and write ${{NAME}} here")
        return {str(k): str(val) for k, val in v.items()}


class SourceSpec(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["database", "file", "api"]
    enabled: bool = True
    holds: str = ""
    when: When = When()
    max_rows: int = Field(20, ge=1, le=200)
    tables: list[TableSpec] = []
    file: FileSpec | None = None
    api: ApiSpec | None = None
    actions: list[ActionType] = []

    @field_validator("actions")
    @classmethod
    def _ownable(cls, v: list[ActionType]) -> list[ActionType]:
        bad = [a.value for a in v if a in UNOWNABLE]
        if bad:
            raise ValueError(f"{', '.join(bad)} is always available and cannot belong to a source")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _shape(self) -> "SourceSpec":
        need = {"database": "tables", "file": "file", "api": "api"}[self.kind]
        given = {"tables": bool(self.tables), "file": self.file is not None, "api": self.api is not None}
        if not given[need]:
            raise ValueError(f"a {self.kind} source needs '{need}:'")
        extra = [k for k, v in given.items() if v and k != need]
        if extra:
            raise ValueError(f"a {self.kind} source cannot have '{extra[0]}:'")
        names = [t.name for t in self.tables]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"table '{sorted(dup)[0]}' is listed twice")
        return self


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


# ───────────────────────────── the loaded folder ─────────────────────────────

@dataclass
class Registry:
    dir: Path
    sources: list[SourceSpec] = field(default_factory=list)   # every valid file, on or off; database → file → api, "always" first
    files: dict[str, str] = field(default_factory=dict)       # source id → file name
    problems: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> list[SourceSpec]:
        return [s for s in self.sources if s.enabled]

    def enabled_tables(self) -> frozenset[str]:
        return frozenset(t.name for s in self.enabled if s.kind == "database" for t in s.tables)

    def table_about(self) -> dict[str, str]:
        return {t.name: t.about for s in self.enabled if s.kind == "database" for t in s.tables}

    def owner(self, action: ActionType | str) -> SourceSpec | None:
        """The source that carries out this fix (on or off); None = built into Munim."""
        for s in self.sources:
            if action in s.actions:
                return s
        return None

    def action_allowed(self, action: ActionType | str) -> bool:
        o = self.owner(action)
        return o is None or o.enabled


def _spec_files(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in (".yaml", ".yml") and not p.name.startswith(("_", ".")))


def _short(e: ValidationError) -> str:
    out = []
    for err in e.errors()[:3]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(out)


def load(d: Path | str | None = None) -> Registry:
    """Read every source file. A broken file is skipped and reported in .problems; it never stops the others."""
    d = Path(d or config.SOURCES_DIR)
    reg = Registry(dir=d)
    if not d.is_dir():
        reg.problems.append(f"Sources folder not found: {d}")
        return reg
    specs: list[tuple[SourceSpec, str, int]] = []
    for p in _spec_files(d):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            reg.problems.append(f"{p.name}: not readable YAML ({str(e).splitlines()[0][:120]})")
            continue
        if not isinstance(raw, dict):
            reg.problems.append(f"{p.name}: expected 'key: value' lines")
            continue
        try:
            specs.append((SourceSpec.model_validate(raw), p.name, p.stat().st_mtime_ns))
        except ValidationError as e:
            reg.problems.append(f"{p.name}: {_short(e)}")
    # each table / fix belongs to one source: on a clash the older file keeps it and the newer file is skipped
    specs.sort(key=lambda x: (x[2], x[1]))
    tables: dict[str, str] = {}
    actions: dict[str, str] = {}
    for spec, fname, _ in specs:
        if spec.id in reg.files:
            reg.problems.append(f"{fname}: id '{spec.id}' is already used by {reg.files[spec.id]}")
            continue
        clash = next((f"table '{t.name}' already belongs to {tables[t.name]}" for t in spec.tables if t.name in tables), None) \
            or next((f"fix {a.value} already belongs to {actions[a.value]}" for a in spec.actions if a.value in actions), None)
        if clash:
            reg.problems.append(f"{fname}: {clash}")
            continue
        reg.files[spec.id] = fname
        tables.update({t.name: fname for t in spec.tables})
        actions.update({a.value: fname for a in spec.actions})
        reg.sources.append(spec)
    reg.sources.sort(key=lambda s: (KIND_ORDER[s.kind], not s.when.always, reg.files[s.id]))
    if not reg.enabled_tables():
        reg.problems.append("No database source is turned on: the AI cannot read any table.")
    return reg


_cache: tuple[Any, Registry] | None = None
_lock = threading.Lock()


def _signature(d: Path) -> Any:
    try:
        return (str(d.resolve()), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in _spec_files(d)))
    except OSError:
        return (str(d), time.time())          # unreadable folder: never cache


def current() -> Registry:
    """The registry for config.SOURCES_DIR, re-read when a file in the folder changes (checked on every call)."""
    global _cache
    d = Path(config.SOURCES_DIR)
    sig = _signature(d)
    with _lock:
        if _cache is not None and _cache[0] == sig:
            return _cache[1]
        reg = load(d)
        _cache = (sig, reg)
        return reg


# ───────────────────────────── per case: when to reach what ─────────────────────────────

@dataclass
class Routed:
    spec: SourceSpec
    why: list[str]

    @property
    def reached(self) -> bool:
        return bool(self.why)


def _label(code: str) -> str:
    return code.lower().replace("_", " ")


def _word_hit(word: str, text: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word), text) is not None


def route(reg: Registry, intake: IntakeResult, complaint_text: str = "") -> list[Routed]:
    """Why each turned-on source is (or is not) relevant to this complaint. Any match is enough."""
    text = " ".join([complaint_text or "", intake.text_en or "", intake.summary_en or ""]).lower()
    area = INTENT_AREA.get(intake.intent)
    claim_kinds = [str(c.kind) for c in intake.claims]
    out: list[Routed] = []
    for s in reg.enabled:
        why: list[str] = []
        if s.when.always:
            why.append("always")
        if area and area in s.when.topics:
            why.append(f"{_label(area)} complaint")
        if intake.intent == Intent.OTHER and s.kind == "database":
            why.append("topic unclear")
        why += [f"claim: {_label(k)}" for k in dict.fromkeys(claim_kinds) if k in s.when.claims]
        words = [w for w in s.when.words if _word_hit(w, text)]
        why += [f"word: {w}" for w in words[:2]]
        out.append(Routed(s, list(dict.fromkeys(why))))
    return out


async def _read_outside(spec: SourceSpec, merchant_id: str, base: Path, timeout_s: float) -> tuple[list[str], list[list[Any]]]:
    if spec.kind == "file":
        return await asyncio.wait_for(asyncio.to_thread(connectors.read_file, spec, merchant_id, base), timeout_s)
    return await asyncio.wait_for(connectors.read_api(spec, merchant_id, timeout_s), timeout_s + 1)


async def _hit(r: Routed, merchant_id: str, reg: Registry, timeout_s: float) -> SourceHit:
    s = r.spec
    hit = SourceHit(id=s.id, name=s.name, kind=s.kind, reached=r.reached, why=r.why,
                    status="ok" if r.reached else "not_needed", tables=[t.name for t in s.tables])
    if s.kind == "database" or not r.reached:
        return hit
    t0 = time.perf_counter()
    try:
        cols, rows = await _read_outside(s, merchant_id, reg.dir, timeout_s)
        hit.columns, hit.rows, hit.records = cols, rows, len(rows)
    except connectors.SourceError as e:
        hit.status, hit.error = "error", str(e)
    except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
        hit.status, hit.error = "error", f"no answer within {timeout_s:g} s"
    except httpx.HTTPError as e:
        hit.status, hit.error = "error", f"could not reach it ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001 - an outside source must never break the case
        hit.status, hit.error = "error", f"could not read it ({type(e).__name__})"
    hit.ms = int((time.perf_counter() - t0) * 1000)
    return hit


async def reach(reg: Registry, intake: IntakeResult, complaint_text: str, merchant_id: str,
                timeout_s: float | None = None) -> SourcesReport:
    """Route, then read the outside sources that matched (in parallel). Never raises."""
    timeout = float(config.SOURCE_TIMEOUT_S if timeout_s is None else timeout_s)
    routed = route(reg, intake, complaint_text)
    hits = list(await asyncio.gather(*(_hit(r, merchant_id, reg, timeout) for r in routed)))
    hits.sort(key=lambda h: not h.reached)          # stable: reached first, in registry order
    return SourcesReport(sources=hits, problems=list(reg.problems))


def count_records(report: SourcesReport, trail_tables: Iterable[str]) -> SourcesReport:
    """Database sources: how many trail records came from their tables. A source the money trail followed links into
    counts as reached even if its `when` did not match (the trail is code and follows ids wherever they lead)."""
    counts: dict[str, int] = {}
    for t in trail_tables:
        counts[t] = counts.get(t, 0) + 1
    for h in report.sources:
        if h.kind == "database":
            h.records = sum(counts.get(t, 0) for t in h.tables)
            if h.records and not h.reached:
                h.reached, h.status, h.why = True, "ok", ["linked from the money trail"]
    report.sources.sort(key=lambda h: not h.reached)
    return report


# ───────────────────────────── listing for the app (GET /api/sources) ─────────────────────────────

def _public_url(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path}" + ("?…" if u.query else "")


def listing(reg: Registry) -> dict[str, Any]:
    """What is connected, for the Sources page. Never includes header values or query strings."""
    items = []
    for s in reg.sources:
        warnings: list[str] = []
        detail: dict[str, Any] = {}
        if s.kind == "file" and s.file:
            detail = {"path": s.file.path, "merchant_column": s.file.merchant_column}
            try:
                if not connectors.file_path(s, reg.dir).is_file():
                    warnings.append(f"file not found: {s.file.path}")
            except connectors.SourceError as e:
                warnings.append(str(e))
        elif s.kind == "api" and s.api:
            detail = {"url": _public_url(s.api.url), "headers": sorted(s.api.headers), "rows_at": s.api.rows_at,
                      "merchant_column": s.api.merchant_column}
            warnings += [f"set {n} in backend/.env" for n in connectors.missing_env(s)]
        owner_off = [] if s.enabled else [a.value for a in s.actions]
        items.append({
            "id": s.id, "name": s.name, "kind": s.kind, "enabled": s.enabled, "file": reg.files.get(s.id, ""),
            "holds": s.holds, "when": s.when.model_dump(), "tables": [t.name for t in s.tables],
            "actions": [a.value for a in s.actions], "blocked_actions": owner_off, "max_rows": s.max_rows,
            "detail": detail, "warnings": warnings,
        })
    return {"dir": str(reg.dir), "sources": items, "problems": list(reg.problems)}
