"""Ingestion observability: run rows, health verdict, freshness, CLI exit codes.

No network, no database. Persistence is exercised through the fake cursor from
``test_flipp_persistence`` (extended to understand the run-row INSERT), and the
Flipp client through a fake HTTP session.
"""
import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import ingestion_runs as runs
from app.services.flipp_client import FlippSearchClient
from app.services.flipp_ingestion import (
    _DEAL_UPSERT,
    IngestionReport,
    collect_deals,
    normalize_items,
    run_ingestion,
)
from app.services.ingestion_runs import (
    _RUN_INSERT,
    assess_health,
    evaluate_freshness,
    latest_run,
    latest_successful_run,
    live_deal_count,
    record_run,
)
from app.tests.test_flipp_ingestion import FakeProvider
from app.tests.test_flipp_persistence import PLACEHOLDER, FakeCursor, FakeDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- helpers ----------------------------------------------------------------

def _item(i, **overrides):
    raw = {
        "name": f"CHICKEN {i}", "current_price": 4.99, "original_price": None,
        "merchant_name": "No Frills", "merchant_id": 2332, "flyer_item_id": 1000 + i,
        "post_price_text": "lb", "_L1": "Food, Beverages & Tobacco",
        "valid_from": "2026-10-15T04:00:00+00:00",
        "valid_to": "2026-10-22T03:59:59+00:00",
    }
    raw.update(overrides)
    return raw


class RunAwareCursor(FakeCursor):
    """FakeCursor that also records the run-row INSERT, and can be made to fail."""

    def __init__(self, fail_on=None, **kwargs):
        super().__init__(**kwargs)
        self.fail_on = fail_on  # a SQL constant that should raise when executed

    def execute(self, sql, params=None):
        if self.fail_on is not None and sql is self.fail_on:
            raise RuntimeError("simulated database failure")
        if sql is _RUN_INSERT:
            params = params or {}
            needed = set(PLACEHOLDER.findall(sql))
            assert needed == set(params), (needed ^ set(params))
            self.calls.append((sql, dict(params)))
            self._next = {"run_id": 77}
            return
        super().execute(sql, params)


def _run(provider, cursor=None, **kwargs):
    cursor = cursor or RunAwareCursor()
    report = run_ingestion(
        "M5V3A8", trigger=kwargs.pop("trigger", "cli"), provider=provider,
        terms=kwargs.pop("terms", ["chicken"]), database=FakeDatabase(cursor), **kwargs,
    )
    return cursor, report


def _report(**overrides):
    report = IngestionReport(postal_code="M5V3A8", source="flipp")
    report.raw_items = 100
    report.normalized = 80
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


# --- the skip-counter fix ----------------------------------------------------

def test_no_merchant_and_no_item_id_have_their_own_counters():
    report = IngestionReport(postal_code="M5V3A8", source="flipp")
    items = [
        _item(1, merchant_name=None),          # -> no_merchant
        _item(2, merchant_name="   "),         # -> no_merchant
        _item(3, flyer_item_id=None),          # -> no_item_id (no 'id' either)
        _item(4, name=None),                   # -> no_name
        _item(5, valid_from="nope"),           # -> bad_dates
    ]
    normalize_items(items, search_term="chicken", report=report)
    assert report.skipped_no_merchant == 2
    assert report.skipped_no_item_id == 1
    # ...and they no longer inflate the counters they used to hide in.
    assert report.skipped_no_name == 1
    assert report.skipped_bad_dates == 1
    assert report.skipped_total == 5


def test_skipped_total_and_unit_null_rate_are_in_as_dict():
    _, report = collect_deals(
        FakeProvider({"t": [_item(1), _item(2, post_price_text=None)]}),
        "M5V3A8", terms=["t"],
    )
    out = report.as_dict()
    assert out["unit_null_rate"] == 0.5
    assert out["skipped_total"] == 0
    json.dumps(out)


# --- the 'items' key missing from the payload --------------------------------

class _Resp:
    status_code = 200
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def get(self, *a, **k):
        return _Resp(self.payloads.pop(0))


def _client(payloads):
    return FlippSearchClient(
        session=_Session(payloads), min_interval=0, max_retries=0, sleep=lambda s: None,
    )


def test_client_counts_responses_with_no_items_list():
    client = _client([{"items": []}, {"ecom_items": []}, {"items": None}])
    assert client.search("a", "M5V3A8") == []
    assert client.missing_items_responses == 0   # an empty list is a real "no hits"
    assert client.search("b", "M5V3A8") == []
    assert client.search("c", "M5V3A8") == []
    assert client.missing_items_responses == 2


def test_collect_deals_surfaces_the_missing_items_counter():
    client = _client([{"items": [_item(1)]}, {"nope": 1}, {"nope": 2}])
    _, report = collect_deals(client, "M5V3A8", terms=["a", "b", "c"])
    assert report.missing_items_responses == 2
    # a second run on the same client reports only its own delta
    client._session.payloads = [{"items": [_item(2)]}]
    _, again = collect_deals(client, "M5V3A8", terms=["a"])
    assert again.missing_items_responses == 0


def test_provider_without_the_counter_reports_zero():
    _, report = collect_deals(FakeProvider({"t": [_item(1)]}), "M5V3A8", terms=["t"])
    assert report.missing_items_responses == 0


# --- assess_health ------------------------------------------------------------

def test_healthy_run_is_ok():
    assert assess_health(_report()) == ("ok", [])


def test_zero_normalised_is_unhealthy():
    status, reasons = assess_health(_report(raw_items=0, normalized=0))
    assert status == "unhealthy"
    assert any("zero deals" in r for r in reasons)


def test_failed_terms_threshold_default_is_five(monkeypatch):
    assert settings.ingestion_max_failed_terms == 5
    assert assess_health(_report(terms_failed=list("abcd")))[0] == "ok"
    status, reasons = assess_health(_report(terms_failed=list("abcde")))
    assert status == "unhealthy"
    assert any("5 search term(s) failed" in r for r in reasons)


def test_failed_terms_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_max_failed_terms", 2)
    assert assess_health(_report(terms_failed=["a", "b"]))[0] == "unhealthy"
    assert assess_health(_report(terms_failed=["a", "b"]), max_failed_terms=3)[0] == "ok"


def test_skip_rate_above_half_is_unhealthy_but_exactly_half_is_not():
    assert assess_health(_report(skipped_non_food=50))[0] == "ok"
    status, reasons = assess_health(_report(skipped_non_food=51))
    assert status == "unhealthy"
    assert any("skip rate" in r for r in reasons)


def test_skip_rate_counts_every_skip_counter():
    report = _report(skipped_no_merchant=20, skipped_no_item_id=20, skipped_bad_dates=11)
    assert assess_health(report)[0] == "unhealthy"


def test_missing_items_key_is_unhealthy():
    status, reasons = assess_health(_report(missing_items_responses=1))
    assert status == "unhealthy"
    assert any("'items'" in r for r in reasons)


def test_every_rule_that_fires_is_reported():
    report = _report(normalized=0, terms_failed=list("abcdef"), skipped_non_food=90,
                     missing_items_responses=3)
    status, reasons = assess_health(report)
    assert status == "unhealthy" and len(reasons) == 4


# --- the run row --------------------------------------------------------------

def test_a_real_run_writes_one_row_with_the_reports_counters():
    provider = FakeProvider({"chicken": [_item(i) for i in range(6)] + [_item(9, name=None)]})
    cursor, report = _run(provider, trigger="refresh_route")

    rows = cursor.params_for(_RUN_INSERT)
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger"] == "refresh_route"
    assert row["postal_code"] == "M5V3A8"
    assert row["source"] == "flipp"
    assert row["dry_run"] is False
    assert row["status"] == "ok" and row["health_reasons"] == []
    assert row["error"] is None
    assert row["raw_items"] == 7
    assert row["normalized"] == report.normalized == 6
    assert row["skipped_total"] == 1
    assert row["deals_inserted"] == report.deals_inserted == 6
    assert row["snapshots_inserted"] == 6
    assert row["terms_searched"] == 1 and row["terms_failed"] == 0
    assert row["regular_price_coverage"] == 0.0
    assert row["unit_null_rate"] == 0.0
    assert row["finished_at"] >= row["started_at"]
    counters = json.loads(row["counters"])
    assert counters["skipped_no_name"] == 1 and counters["normalized"] == 6


def test_run_row_is_written_after_the_ingestion_statements():
    cursor, _ = _run(FakeProvider({"chicken": [_item(1)]}))
    sqls = [s for s, _ in cursor.calls]
    assert sqls.index(_RUN_INSERT) > sqls.index(_DEAL_UPSERT)


def test_an_unhealthy_run_is_recorded_as_unhealthy_with_reasons():
    cursor, report = _run(FakeProvider({"chicken": []}))   # nothing at all
    row = cursor.params_for(_RUN_INSERT)[0]
    assert row["status"] == "unhealthy"
    assert report.status == "unhealthy"
    assert any("zero deals" in r for r in row["health_reasons"])
    assert cursor.count(_DEAL_UPSERT) == 0


def test_failed_run_row_write_does_not_fail_the_ingestion(caplog):
    cursor = RunAwareCursor(fail_on=_RUN_INSERT)
    provider = FakeProvider({"chicken": [_item(1), _item(2)]})
    with caplog.at_level("ERROR"):
        _, report = _run(provider, cursor=cursor)
    assert report.deals_inserted == 2          # the ingestion landed
    assert report.status == "ok"
    assert cursor.count(_DEAL_UPSERT) == 2
    assert "Could not record ingestion run" in caplog.text


def test_record_run_swallows_a_dead_connection():
    class Dead:
        def get_connection(self):
            raise ConnectionError("SSL connection has been closed unexpectedly")

    now = datetime.now(timezone.utc)
    assert record_run(_report(), trigger="cli", started_at=now, status="ok",
                      database=Dead()) is None


def test_an_exception_during_persistence_is_recorded_as_failed_and_reraised():
    cursor = RunAwareCursor(fail_on=_DEAL_UPSERT)
    with pytest.raises(RuntimeError, match="simulated"):
        _run(FakeProvider({"chicken": [_item(1)]}), cursor=cursor)
    row = cursor.params_for(_RUN_INSERT)[0]
    assert row["status"] == "failed"
    assert "simulated database failure" in row["error"]
    assert row["normalized"] == 1       # what the report knew is kept


def test_unknown_trigger_is_refused_before_any_work():
    provider = FakeProvider({"chicken": [_item(1)]})
    with pytest.raises(ValueError):
        run_ingestion("M5V3A8", trigger="cron", provider=provider, terms=["chicken"],
                      database=FakeDatabase(RunAwareCursor()))
    assert provider.calls == []


def test_trigger_is_required():
    with pytest.raises(TypeError):
        run_ingestion("M5V3A8", provider=FakeProvider({}), terms=[])


def test_run_row_sql_is_parameterised():
    assert PLACEHOLDER.search(_RUN_INSERT)
    assert "%s" not in PLACEHOLDER.sub("", _RUN_INSERT)


# --- 020 migration structure --------------------------------------------------

MIGRATION = REPO_ROOT / "migrations" / "020_ingestion_runs.sql"


def test_020_exists_and_creates_the_table():
    body = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ingestion_runs" in body


def test_020_insert_columns_all_exist_in_the_migration():
    body = MIGRATION.read_text(encoding="utf-8")
    table = body.split("CREATE TABLE IF NOT EXISTS ingestion_runs", 1)[1].split(");", 1)[0]
    declared = set(re.findall(r"^\s{4}([a-z_]+)\s+[A-Z]", table, flags=re.M))
    insert_cols = re.search(r"INSERT INTO ingestion_runs \((.*?)\) VALUES", _RUN_INSERT, re.S)
    inserted = {c.strip() for c in insert_cols.group(1).split(",")}
    assert inserted <= declared, inserted - declared
    # the health fields the task names
    for col in ("raw_items", "normalized", "skipped_total", "terms_failed",
                "deals_inserted", "deals_updated", "snapshots_inserted",
                "regular_price_coverage", "unit_null_rate", "dry_run", "status",
                "error", "trigger", "postal_code", "started_at", "finished_at"):
        assert col in declared, col


def test_020_constrains_trigger_and_status():
    body = MIGRATION.read_text(encoding="utf-8")
    assert "'cli', 'refresh_route', 'cold_start'" in body
    assert "'ok', 'unhealthy', 'failed'" in body


def test_020_has_no_delete_paths():
    body = re.sub(r"--.*", "", MIGRATION.read_text(encoding="utf-8").split("-- Rollback:")[0])
    assert not re.search(r"\b(DELETE|TRUNCATE)\b", body, re.I)


def test_nothing_in_app_deletes_from_ingestion_runs():
    for path in list((REPO_ROOT / "app").rglob("*.py")) + list((REPO_ROOT / "scripts").glob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(DELETE\s+FROM|TRUNCATE)\s+ingestion_runs", text, re.I), path


# --- freshness helpers --------------------------------------------------------

class _QueryDb:
    """Fake database whose cursor records the query and returns a canned row."""

    def __init__(self, row):
        self.row = row
        self.queries = []

    def get_cursor(self):
        outer = self

        class Cur:
            def execute(self, sql, params=None):
                outer.queries.append((sql, params))

            def fetchone(self):
                return outer.row

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Cur()


def test_latest_successful_run_only_considers_ok_non_dry_runs():
    db = _QueryDb({"run_id": 3, "status": "ok"})
    assert latest_successful_run("M5V3A8", database=db)["run_id"] == 3
    sql, params = db.queries[0]
    assert "status = 'ok'" in sql and "NOT dry_run" in sql
    assert "ORDER BY finished_at DESC" in sql
    assert params == {"postal_code": "M5V3A8"}


def test_latest_run_considers_any_status_and_none_is_none():
    db = _QueryDb(None)
    assert latest_run("M5V3A8", database=db) is None
    assert "status = 'ok'" not in db.queries[0][0]


def test_live_deal_count_counts_valid_deals_at_active_stores():
    db = _QueryDb({"n": 12})
    assert live_deal_count("M5V3A8", database=db) == 12
    sql = db.queries[0][0]
    for clause in ("valid_from <= CURRENT_DATE", "valid_until >= CURRENT_DATE",
                   "s.is_active = true", "s.postal_code"):
        assert clause in sql


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def _ok_run(days_ago):
    return {"finished_at": NOW - timedelta(days=days_ago)}


def test_fresh_run_with_live_deals_is_healthy():
    healthy, problems, age = evaluate_freshness(_ok_run(2), 40, now=NOW)
    assert healthy and problems == [] and age == timedelta(days=2)


def test_eight_days_is_fine_nine_is_stale():
    assert evaluate_freshness(_ok_run(8), 5, now=NOW)[0] is True
    healthy, problems, _ = evaluate_freshness(_ok_run(9), 5, now=NOW)
    assert not healthy and "9 day(s) old" in problems[0]


def test_zero_live_deals_is_unhealthy_even_when_the_run_is_fresh():
    healthy, problems, _ = evaluate_freshness(_ok_run(1), 0, now=NOW)
    assert not healthy and problems == ["0 live deals"]


def test_never_ingested_is_unhealthy():
    healthy, problems, age = evaluate_freshness(None, 3, now=NOW)
    assert not healthy and age is None
    assert "no successful ingestion run" in problems[0]


# --- NULLS LAST ---------------------------------------------------------------

def test_every_discount_ordering_in_app_code_sorts_nulls_last():
    """78% of ingested deals have a NULL discount; Postgres sorts NULL first in
    DESC order, so an unguarded ORDER BY ranks them above real discounts."""
    offenders = []
    checked = 0
    for path in (REPO_ROOT / "app").rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"discount_percentage\s+DESC\b(?!\s+NULLS\s+LAST)", text):
            offenders.append(f"{path.name}: {text[m.start() - 30:m.end() + 20]!r}")
        checked += len(re.findall(r"discount_percentage\s+DESC\s+NULLS\s+LAST", text))
    assert not offenders, offenders
    assert checked >= 5   # database.py + the four in store_service.py


def test_store_service_queries_sort_nulls_last():
    text = (REPO_ROOT / "app" / "services" / "store_service.py").read_text(encoding="utf-8")
    assert text.count("discount_percentage DESC NULLS LAST") == 4


# --- CLI ----------------------------------------------------------------------

@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location(
        "ingest_deals_cli", REPO_ROOT / "scripts" / "ingest_deals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli_enabled(monkeypatch, cli):
    monkeypatch.setattr(settings, "deal_ingestion_enabled", True)
    return cli


def _fake_run(status="ok", reasons=(), calls=None):
    def run(postal_code, *, terms=None, trigger):
        if calls is not None:
            calls.append({"postal_code": postal_code, "trigger": trigger})
        report = IngestionReport(postal_code=postal_code, source="flipp")
        report.normalized = 10
        report.status = status
        report.health_reasons = list(reasons)
        return report
    return run


def test_cli_exits_zero_on_a_healthy_run_and_records_as_cli(monkeypatch, cli_enabled):
    calls = []
    monkeypatch.setattr(cli_enabled, "run_ingestion", _fake_run(calls=calls))
    assert cli_enabled.main(["M5V 3A8", "--limit-terms", "1"]) == 0
    assert calls == [{"postal_code": "M5V3A8", "trigger": "cli"}]


def test_cli_exits_nonzero_on_an_unhealthy_run(monkeypatch, cli_enabled, capsys):
    monkeypatch.setattr(cli_enabled, "run_ingestion",
                        _fake_run("unhealthy", ["zero deals normalised"]))
    assert cli_enabled.main(["M5V3A8"]) == 1
    assert "zero deals normalised" in capsys.readouterr().err


def test_cli_exits_nonzero_on_a_failed_run_and_keeps_going(monkeypatch, cli_enabled, capsys):
    seen = []

    def run(postal_code, *, terms=None, trigger):
        seen.append(postal_code)
        if postal_code == "M5V3A8":
            raise RuntimeError("neon went away")
        return _fake_run()(postal_code, terms=terms, trigger=trigger)

    monkeypatch.setattr(cli_enabled, "run_ingestion", run)
    assert cli_enabled.main(["M5V3A8", "K1A0B1"]) == 1
    assert seen == ["M5V3A8", "K1A0B1"]
    assert "neon went away" in capsys.readouterr().err


def test_cli_a_few_failed_terms_below_threshold_do_not_fail_the_run(monkeypatch, cli_enabled):
    def run(postal_code, *, terms=None, trigger):
        report = _fake_run()(postal_code, terms=terms, trigger=trigger)
        report.terms_failed = ["milk"]
        return report

    monkeypatch.setattr(cli_enabled, "run_ingestion", run)
    assert cli_enabled.main(["M5V3A8"]) == 0


def test_cli_dry_run_records_nothing_and_never_calls_run_ingestion(monkeypatch, cli_enabled):
    def boom(*a, **k):
        raise AssertionError("dry run must not ingest or record")

    monkeypatch.setattr(cli_enabled, "run_ingestion", boom)
    monkeypatch.setattr(runs, "record_run", boom)
    monkeypatch.setattr(
        cli_enabled, "collect_deals",
        lambda provider, pc, terms=None: ([], IngestionReport(postal_code=pc, source="flipp")),
    )
    assert cli_enabled.main(["M5V3A8", "--dry-run", "--terms", "rice"]) == 0


def _patch_health(monkeypatch, cli, *, last_ok, live, last=None):
    def no_ingest(*a, **k):
        raise AssertionError("--health must never ingest")

    monkeypatch.setattr(cli, "run_ingestion", no_ingest)
    monkeypatch.setattr(cli, "collect_deals", no_ingest)
    monkeypatch.setattr(cli, "latest_run", lambda pc: last or last_ok)
    monkeypatch.setattr(cli, "latest_successful_run", lambda pc: last_ok)
    monkeypatch.setattr(cli, "live_deal_count", lambda pc: live)


def _run_dict(days_ago, status="ok"):
    return {
        "run_id": 5, "status": status, "trigger": "cli", "normalized": 80,
        "deals_inserted": 70, "deals_updated": 10, "terms_failed": 0,
        "finished_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "health_reasons": [], "error": None,
    }


def test_health_flag_exits_zero_when_fresh_with_live_deals(monkeypatch, cli, capsys):
    _patch_health(monkeypatch, cli, last_ok=_run_dict(1), live=30)
    assert cli.main(["M5V3A8", "--health"]) == 0
    out = capsys.readouterr().out
    assert "live deals:    30" in out and "last ok age:   1.0 day(s)" in out


def test_health_flag_exits_nonzero_when_last_ok_run_is_stale(monkeypatch, cli):
    _patch_health(monkeypatch, cli, last_ok=_run_dict(9), live=30)
    assert cli.main(["M5V3A8", "--health"]) == 1


def test_health_flag_exits_nonzero_with_zero_live_deals(monkeypatch, cli):
    _patch_health(monkeypatch, cli, last_ok=_run_dict(1), live=0)
    assert cli.main(["M5V3A8", "--health"]) == 1


def test_health_flag_exits_nonzero_when_nothing_was_ever_recorded(monkeypatch, cli, capsys):
    _patch_health(monkeypatch, cli, last_ok=None, live=5, last=None)
    assert cli.main(["M5V3A8", "--health"]) == 1
    assert "none recorded" in capsys.readouterr().out


def test_health_flag_works_while_ingestion_is_disabled(monkeypatch, cli):
    """It only reads, so it must not demand DEAL_INGESTION_ENABLED."""
    monkeypatch.setattr(settings, "deal_ingestion_enabled", False)
    _patch_health(monkeypatch, cli, last_ok=_run_dict(1), live=3)
    assert cli.main(["M5V3A8", "--health"]) == 0
