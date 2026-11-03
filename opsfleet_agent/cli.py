"""Terminal chat with the analyst agent. Run: uv run python -m opsfleet_agent"""
import argparse
import asyncio
import logging

from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from .access import AccessError, load_scope
from .activity_log import ActivityLog, attach
from .agent import build_agent
from .config import ConfigError, load_settings

APP_NAME = "opsfleet_agent"
CONFIRMATION_REQUEST = "adk_request_confirmation"  # the call ADK makes when a tool asks the user to confirm
GREY_TEXT = "\033[2m"     # terminal code: switch to faint text
NORMAL_TEXT = "\033[0m"   # terminal code: back to normal


class RetryNotice(logging.Handler):
    """Shows one short line whenever the Gemini client waits before a retry."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("Retrying"):
            print(f"{GREY_TEXT}(Gemini is busy, retrying...){NORMAL_TEXT}")
        elif record.levelno >= logging.WARNING:
            print(record.getMessage())


class GreyLine(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        print(f"{GREY_TEXT}{record.getMessage()}{NORMAL_TEXT}")


def show_golden_examples() -> None:
    golden_log = logging.getLogger("opsfleet_agent.golden")  # which trios each question was given
    golden_log.setLevel(logging.INFO)
    golden_log.addHandler(GreyLine())
    golden_log.propagate = False


def show_gemini_retries() -> None:
    gemini_log = logging.getLogger("google_genai._api_client")  # where the client logs each retry
    gemini_log.setLevel(logging.INFO)
    gemini_log.addHandler(RetryNotice())
    gemini_log.propagate = False


async def chat(user_id: str, show_tools: bool) -> None:
    settings = load_settings()
    scope = load_scope(user_id)
    log = ActivityLog(settings.log_file)
    app = App(name=APP_NAME, root_agent=build_agent(settings, user_id, scope), plugins=[attach(log)])
    runner = InMemoryRunner(app=app)
    run_config = RunConfig(max_llm_calls=settings.max_model_calls)
    show_gemini_retries()
    if show_tools:
        show_golden_examples()
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=user_id)

    print(f"Retail analyst ({settings.model}). Signed in as {user_id}: products where {scope.describe()}.")
    print("Type 'quit' or press Ctrl+D to leave.")
    while True:
        try:
            text = (await asyncio.to_thread(input, "\nyou> ")).strip()
        except EOFError:
            return
        if not text:
            continue
        if text.lower() in ("quit", "exit"):
            return

        message = types.Content(role="user", parts=[types.Part(text=text)])
        try:
            # A turn pauses when a tool asks for confirmation; ask the user and resume with their answer.
            while message:
                confirmations = await run_turn(runner, run_config, user_id, session.id, message, show_tools)
                message = await ask_user_to_confirm(confirmations) if confirmations else None
        except LlmCallsLimitExceededError:
            print("\nagent> Sorry, I couldn't finish working that out. Try asking a simpler or more specific question.")
        except Exception as e:  # one failed turn must not end the session
            print(f"\nagent> Sorry, something went wrong: {e}")


async def run_turn(runner, run_config, user_id, session_id, message, show_tools) -> list[types.FunctionCall]:
    """Print the agent's reply as it streams in. Returns any confirmation requests it paused on."""
    confirmations = []
    replying = False  # a reply can arrive in several pieces; label only the first
    events = runner.run_async(user_id=user_id, session_id=session_id, new_message=message, run_config=run_config)
    async for event in events:
        for part in (event.content.parts if event.content else None) or []:
            call = part.function_call
            if part.text and not part.thought:
                print(part.text if replying else f"\nagent> {part.text}")
                replying = True
            elif call and call.name == CONFIRMATION_REQUEST and call.id in (event.long_running_tool_ids or ()):
                confirmations.append(call)
            elif call:
                replying = False
                if show_tools:
                    detail = (call.args or {}).get("sql", "")
                    print(f"{GREY_TEXT}[{call.name}] {detail}{NORMAL_TEXT}")
    return confirmations


async def ask_user_to_confirm(confirmations: list[types.FunctionCall]) -> types.Content:
    """Ask the user directly, outside the model, and pass their answer back to the waiting tool.

    The prompt text comes from the tool (program code), and the answer goes straight back
    to the tool, which decides whether it counts as a yes. The model never sees either.
    """
    parts = []
    for call in confirmations:
        print(f"\n{call.args['toolConfirmation']['hint']}")
        try:
            answer = (await asyncio.to_thread(input, "confirm> ")).strip()
        except EOFError:
            answer = ""
        parts.append(types.Part(function_response=types.FunctionResponse(
            id=call.id, name=CONFIRMATION_REQUEST, response={"payload": {"answer": answer}},
        )))
    return types.Content(role="user", parts=parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the retail data analyst.")
    parser.add_argument("--user", required=True, help="who is chatting, one of the users in access.json")
    parser.add_argument("--quiet", action="store_true", help="hide the tool calls the agent makes")
    args = parser.parse_args()
    try:
        asyncio.run(chat(args.user, show_tools=not args.quiet))
    except (ConfigError, AccessError) as e:
        raise SystemExit(f"Setup problem: {e}")
    except KeyboardInterrupt:
        pass
    print("\nBye.")
