"""Recovery from failed queries and failed Gemini calls, checked without the network."""
from types import SimpleNamespace

import httpx
from google.api_core import exceptions as gcp
from google.auth import exceptions as auth
from google.genai import errors

from opsfleet_agent.access import ProductScope
from opsfleet_agent.config import Settings
from opsfleet_agent.recovery import gemini_failure_message, on_model_error
from opsfleet_agent.tools import make_tools

SETTINGS = Settings(
    project="test", dataset="bigquery-public-data.thelook_ecommerce", model="test",
    max_bytes_billed=10**9, max_rows=50, max_failed_queries=3, max_model_calls=15, reports_db=":memory:",
    embedding_model="test", golden_examples=3, log_file="unused.jsonl",
)
SCOPE = ProductScope((("brand", ("Calvin Klein",)),))
OK = {"rows": [{"n": 1}], "row_count": 1, "total_rows": 1}
EMPTY = {"rows": [], "row_count": 0, "total_rows": 0}


class FakeBigQuery:
    """Plays back a list of results; an exception in the list is raised instead."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def query(self, sql, max_rows, params):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


def run_sql_with(bq):
    _, run_sql = make_tools(SETTINGS, bq, SCOPE)
    return run_sql


def question(n):
    return SimpleNamespace(invocation_id=f"question-{n}")


def test_sql_error_goes_back_to_the_model():
    run_sql = run_sql_with(FakeBigQuery(gcp.BadRequest("Unrecognized name: revenue"), OK))
    result = run_sql("SELECT revenue FROM orders", question(1))
    assert "Unrecognized name: revenue" in result["error"] and "Fix the query" in result["error"]
    assert run_sql("SELECT 1 FROM orders", question(1)) == OK


def test_empty_result_gets_a_hint():
    result = run_sql_with(FakeBigQuery(EMPTY))("SELECT * FROM products WHERE category = 'jeans'", question(1))
    assert result["total_rows"] == 0 and "spelling" in result["note"]


def test_agent_must_stop_after_three_failures_in_one_question():
    bq = FakeBigQuery(gcp.BadRequest("bad"), EMPTY, gcp.NotFound("no such column"))
    run_sql = run_sql_with(bq)
    for _ in range(3):
        run_sql("SELECT 1 FROM orders", question(1))
    result = run_sql("SELECT 1 FROM orders", question(1))
    assert result["error"].startswith("Stopped") and bq.calls == 3  # the fourth never reached BigQuery


def test_a_success_resets_the_count():
    bq = FakeBigQuery(gcp.BadRequest("bad"), gcp.BadRequest("bad"), OK, gcp.BadRequest("bad"), OK)
    run_sql = run_sql_with(bq)
    results = [run_sql("SELECT 1 FROM orders", question(1)) for _ in range(5)]
    assert results[-1] == OK


def test_a_new_question_starts_a_new_count():
    run_sql = run_sql_with(FakeBigQuery(*[gcp.BadRequest("bad")] * 3, OK))
    for _ in range(3):
        run_sql("SELECT 1 FROM orders", question(1))
    assert run_sql("SELECT 1 FROM orders", question(2)) == OK


def test_refused_query_counts_as_a_failure():
    run_sql = run_sql_with(FakeBigQuery())
    for _ in range(3):
        assert "refused" in run_sql("DELETE FROM orders WHERE TRUE", question(1))["error"]
    assert run_sql("DELETE FROM orders WHERE TRUE", question(1))["error"].startswith("Stopped")


def test_bigquery_outage_says_do_not_retry():
    run_sql = run_sql_with(FakeBigQuery(gcp.ServiceUnavailable("backend error")))
    assert "Do not retry" in run_sql("SELECT 1 FROM orders", question(1))["error"]


def test_expired_login_is_explained():
    run_sql = run_sql_with(FakeBigQuery(auth.RefreshError("token expired")))
    assert "gcloud auth application-default login" in run_sql("SELECT 1 FROM orders", question(1))["error"]


def api_error(code):
    return errors.APIError(code, {"error": {"message": "details", "status": "X"}})


def test_gemini_failures_get_plain_messages():
    assert "usage limit" in gemini_failure_message(api_error(429))
    assert "overloaded" in gemini_failure_message(api_error(503))
    assert "setup problem" in gemini_failure_message(api_error(404))
    assert "isn't responding" in gemini_failure_message(httpx.ConnectTimeout("timed out"))


def test_gemini_failure_becomes_a_normal_reply():
    response = on_model_error(None, None, api_error(503))
    assert "overloaded" in response.content.parts[0].text
