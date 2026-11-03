"""The activity log: one JSON line per thing that happened, to see afterwards what went wrong.

Every line carries the time, the user, the session and the question it belongs to, so a
single question can be followed from start to finish. It records:

  question       what the user asked
  examples       which golden trios were added to the instructions
  model_call     each Gemini call: tokens in and out, and which tools it asked for
  gemini_retry   the Gemini client waiting before trying again
  model_error    a Gemini failure the retries didn't fix
  tool           each tool call: arguments (the SQL), how long it took, rows or the error
  tool_crash     a tool that raised instead of returning an error
  answer         the reply shown to the user (after personal details are removed)
  question_end   totals for the question, and how it ended
  run_error      the question failed with an exception

Query results are not logged, only row counts, so the log holds no customer data.

Read it with: uv run python -m opsfleet_agent.activity_log [--last N]
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from google.adk.plugins.base_plugin import BasePlugin

MAX_TEXT = 2000  # longer texts (answers, SQL) are cut to this many characters


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"... [{len(text) - MAX_TEXT} more characters]"


class ActivityLog:
    """Appends lines to the log file. The CLI runs one question at a time, so the
    question being answered is kept here and stamped onto every line."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.current = {}  # user, session and question id of the question being answered

    def write(self, kind: str, **fields) -> None:
        line = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind}
        line |= self.current | fields
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass  # a full disk or a locked file must not break the chat


class LogPlugin(BasePlugin):
    """Hooks into every step ADK takes and writes it to the activity log."""

    def __init__(self, log: ActivityLog) -> None:
        super().__init__(name="activity_log")
        self.log = log
        self.question = {}  # running totals for the question being answered
        self.tool_started = {}  # tool call id -> start time

    async def before_run_callback(self, *, invocation_context):
        content = invocation_context.user_content
        parts = content.parts if content else None
        self.log.current = {
            "user": invocation_context.user_id,
            "session": invocation_context.session.id,
            "question_id": invocation_context.invocation_id,
        }
        self.question = {"started": time.monotonic(), "model_calls": 0, "queries": 0, "failed_queries": 0,
                         "tokens_in": 0, "tokens_out": 0}
        texts = [p.text for p in parts or [] if p.text]
        if texts:
            self.log.write("question", text=clip(" ".join(texts)))
        else:  # the user answering a confirmation prompt, which resumes the question
            answers = [p.function_response.response for p in parts or [] if p.function_response]
            self.log.write("confirmation_answer", answers=answers)

    async def after_model_callback(self, *, callback_context, llm_response):
        usage = llm_response.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        tokens_out = (usage.candidates_token_count or 0) if usage else 0
        calls = [p.function_call.name for p in (llm_response.content.parts if llm_response.content else None) or []
                 if p.function_call]
        self.question["model_calls"] += 1
        self.question["tokens_in"] += tokens_in
        self.question["tokens_out"] += tokens_out
        self.log.write("model_call", tokens_in=tokens_in, tokens_out=tokens_out, tool_calls=calls,
                       finish_reason=llm_response.finish_reason)

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        self.log.write("model_error", error_type=type(error).__name__, error=clip(str(error)))

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        self.tool_started[tool_context.function_call_id] = time.monotonic()

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        started = self.tool_started.pop(tool_context.function_call_id, time.monotonic())
        outcome = {"ms": round((time.monotonic() - started) * 1000)}
        if isinstance(result, dict):
            for key in ("row_count", "total_rows"):
                if key in result:
                    outcome[key] = result[key]
            for key in ("error", "note", "status", "message"):
                if key in result:
                    outcome[key] = clip(str(result[key]))
        if tool.name == "run_sql":
            self.question["queries"] += 1
            if "error" in outcome or outcome.get("total_rows") == 0:
                self.question["failed_queries"] += 1
        args = {k: clip(str(v)) for k, v in tool_args.items()}
        self.log.write("tool", tool=tool.name, args=args, **outcome)

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self.tool_started.pop(tool_context.function_call_id, None)
        self.log.write("tool_crash", tool=tool.name, error_type=type(error).__name__, error=clip(str(error)))

    async def on_event_callback(self, *, invocation_context, event):
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
            if text:
                self.log.write("answer", text=clip(text))

    async def after_run_callback(self, *, invocation_context):
        self.end_question("finished")

    async def on_run_error_callback(self, *, invocation_context, error):
        self.log.write("run_error", error_type=type(error).__name__, error=clip(str(error)))
        self.end_question("failed")

    def end_question(self, outcome: str) -> None:
        if not self.question:
            return
        totals = dict(self.question)
        seconds = round(time.monotonic() - totals.pop("started"), 1)
        self.log.write("question_end", outcome=outcome, seconds=seconds, **totals)
        self.question = {}


class LogLines(logging.Handler):
    """Copies messages from a Python logger (golden lookups, Gemini retries) into the activity log."""

    def __init__(self, log: ActivityLog, kind: str, only_prefix: str = "") -> None:
        super().__init__()
        self.log, self.kind, self.only_prefix = log, kind, only_prefix

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.only_prefix):
            self.log.write(self.kind, level=record.levelname, message=clip(message))


def attach(log: ActivityLog) -> LogPlugin:
    """Route the loggers we care about into the activity log and return the ADK plugin."""
    for name, kind, prefix in (("opsfleet_agent.golden", "examples", ""),
                               ("google_genai._api_client", "gemini_retry", "Retrying")):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(LogLines(log, kind, prefix))
    return LogPlugin(log)


# ---- reading the log back ----

def describe(line: dict) -> str:
    kind = line["kind"]
    if kind == "question":
        return f"QUESTION from {line.get('user')}: {line['text']}"
    if kind == "confirmation_answer":
        return f"  user answered the confirmation prompt: {line['answers']}"
    if kind == "examples":
        return f"  examples: {line['message'].removeprefix('[golden examples] ')}"
    if kind == "model_call":
        tools = ", ".join(line["tool_calls"]) or "reply"
        return f"  gemini call -> {tools}  ({line['tokens_in']} tokens in, {line['tokens_out']} out)"
    if kind == "tool":
        head = f"  {line['tool']} ({line['ms']} ms)"
        sql = line["args"].get("sql")
        body = f"\n      " + sql.replace("\n", "\n      ") if sql else (f" {line['args']}" if line["args"] else "")
        if "error" in line:
            result = f"\n      ERROR: {line['error']}"
        elif "total_rows" in line:
            result = f"\n      -> {line['total_rows']} rows" + (" (EMPTY)" if line["total_rows"] == 0 else "")
        else:
            result = f"\n      -> {line.get('status') or line.get('message') or 'ok'}"
        return head + body + result
    if kind in ("model_error", "tool_crash", "run_error"):
        return f"  {kind.upper().replace('_', ' ')}: {line['error_type']}: {line['error']}"
    if kind == "gemini_retry":
        return f"  gemini retry: {line['message']}"
    if kind == "answer":
        return "  ANSWER: " + line["text"].replace("\n", "\n    ")
    if kind == "question_end":
        return (f"  -- {line['outcome']} in {line['seconds']}s: {line['model_calls']} gemini calls, "
                f"{line['queries']} queries ({line['failed_queries']} failed or empty), "
                f"{line['tokens_in'] + line['tokens_out']} tokens\n")
    return f"  {kind}: {line}"


def show(path: str, last: int) -> None:
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    question_starts = [i for i, line in enumerate(lines) if line["kind"] == "question"]
    start = question_starts[-last] if len(question_starts) >= last else 0
    session = None
    for line in lines[start:]:
        if line.get("session") != session:
            session = line.get("session")
            print(f"===== session {session}, {line['time'][:19].replace('T', ' ')} UTC =====")
        print(describe(line))


def main() -> None:
    from .config import log_file  # here, so reading the log needs no API keys

    parser = argparse.ArgumentParser(description="Show the agent's activity log in readable form.")
    parser.add_argument("--last", type=int, default=5, help="how many recent questions to show (default 5)")
    parser.add_argument("--file", default=log_file(), help="log file to read")
    args = parser.parse_args()
    if not Path(args.file).exists():
        raise SystemExit(f"No log yet at {args.file}. Chat with the agent first.")
    show(args.file, args.last)


if __name__ == "__main__":
    main()
