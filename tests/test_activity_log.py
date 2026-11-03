"""The activity log, checked by running a real ADK agent with a scripted stand-in for Gemini."""
import asyncio
import json
import logging

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from opsfleet_agent.activity_log import ActivityLog, attach, show
from opsfleet_agent.recovery import on_model_error


class ScriptedModel(BaseLlm):
    """Plays back one reply per call; an exception in the script is raised instead."""

    script: list = []

    async def generate_content_async(self, llm_request, stream=False):
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield LlmResponse(
            content=types.Content(role="model", parts=[reply]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10),
        )


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(t):
    return types.Part(text=t)


ROW_VALUE = "value-only-in-the-query-result"


def run_sql(sql: str) -> dict:
    """Run a query."""
    if "broken" in sql:
        return {"error": "Unrecognized name: broken. Fix the query and try again."}
    if "nothing" in sql:
        return {"rows": [], "row_count": 0, "total_rows": 0, "note": "No rows."}
    return {"rows": [{"n": ROW_VALUE}], "row_count": 1, "total_rows": 1}


def explode() -> dict:
    """A tool that crashes."""
    raise RuntimeError("tool bug")


def ask(tmp_path, script, question="How many orders?", tools=(run_sql,)):
    log = ActivityLog(str(tmp_path / "logs" / "agent.jsonl"))
    agent = Agent(name="analyst", model=ScriptedModel(model="scripted", script=list(script)), tools=list(tools),
                  on_model_error_callback=on_model_error)
    runner = InMemoryRunner(app=App(name="test", root_agent=agent, plugins=[attach(log)]))

    async def go():
        session = await runner.session_service.create_session(app_name="test", user_id="ben")
        message = types.Content(role="user", parts=[text(question)])
        async for _ in runner.run_async(user_id="ben", session_id=session.id, new_message=message):
            pass

    try:
        asyncio.run(go())
    except Exception:
        pass  # a failing run is still logged; the test checks the log
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    return log, lines


def kinds(lines):
    return [line["kind"] for line in lines]


def test_a_question_is_logged_from_start_to_end(tmp_path):
    _, lines = ask(tmp_path, [
        call("run_sql", sql="SELECT broken"),
        call("run_sql", sql="SELECT nothing"),
        call("run_sql", sql="SELECT COUNT(*) AS n FROM orders"),
        text("There were 42 orders."),
    ])
    assert kinds(lines) == ["question", "model_call", "tool", "model_call", "tool", "model_call", "tool",
                            "model_call", "answer", "question_end"]
    assert lines[0]["text"] == "How many orders?"
    assert {line["user"] for line in lines} == {"ben"}
    assert len({line["question_id"] for line in lines}) == 1

    broken, empty, good = [line for line in lines if line["kind"] == "tool"]
    assert broken["args"]["sql"] == "SELECT broken" and "Unrecognized name" in broken["error"]
    assert empty["total_rows"] == 0
    assert good["total_rows"] == 1 and "error" not in good
    assert lines[-2]["text"] == "There were 42 orders."

    end = lines[-1]
    assert end["outcome"] == "finished"
    assert (end["model_calls"], end["queries"], end["failed_queries"]) == (4, 3, 2)
    assert (end["tokens_in"], end["tokens_out"]) == (400, 40)


def test_query_results_are_not_logged(tmp_path):
    _, lines = ask(tmp_path, [call("run_sql", sql="SELECT 1"), text("Done.")])
    tool = next(line for line in lines if line["kind"] == "tool")
    assert "rows" not in tool and ROW_VALUE not in json.dumps(tool)


def test_a_gemini_failure_is_logged(tmp_path):
    _, lines = ask(tmp_path, [ConnectionError("network down")])
    error = next(line for line in lines if line["kind"] == "model_error")
    assert error["error_type"] == "ConnectionError" and "network down" in error["error"]
    assert "answer" in kinds(lines)  # the plain-English message from recovery.py still reaches the user


def test_a_crashing_tool_is_logged(tmp_path):
    _, lines = ask(tmp_path, [call("explode"), text("unreachable")], tools=(explode,))
    crash = next(line for line in lines if line["kind"] == "tool_crash")
    assert crash["tool"] == "explode" and "tool bug" in crash["error"]
    assert lines[-1]["kind"] == "question_end" and lines[-1]["outcome"] == "failed"


def test_golden_lookups_and_gemini_retries_are_copied_in(tmp_path):
    log = ActivityLog(str(tmp_path / "agent.jsonl"))
    attach(log)
    try:
        logging.getLogger("opsfleet_agent.golden").info("[golden examples] churn (0.81)")
        logging.getLogger("google_genai._api_client").info("Retrying request in 2.0 seconds")
        logging.getLogger("google_genai._api_client").info("Request sent")  # not a retry: ignored
    finally:
        for name in ("opsfleet_agent.golden", "google_genai._api_client"):
            logging.getLogger(name).handlers.clear()
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert kinds(lines) == ["examples", "gemini_retry"]
    assert "churn" in lines[0]["message"]


def test_long_text_is_cut(tmp_path):
    _, lines = ask(tmp_path, [text("x" * 5000)])
    answer = next(line for line in lines if line["kind"] == "answer")
    assert len(answer["text"]) < 2100 and "3000 more characters" in answer["text"]


def test_the_log_reads_back_as_text(tmp_path, capsys):
    log, _ = ask(tmp_path, [call("run_sql", sql="SELECT broken"), text("Sorry, that failed.")])
    show(str(log.path), last=5)
    out = capsys.readouterr().out
    assert "QUESTION from ben: How many orders?" in out
    assert "SELECT broken" in out and "ERROR: Unrecognized name" in out
    assert "ANSWER: Sorry, that failed." in out
    assert "1 queries (1 failed or empty)" in out
