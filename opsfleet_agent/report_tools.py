"""Tools for the Saved Reports library, bound to the signed-in user.

Deleting takes two tools. find_reports lists matches and issues a ticket naming
exactly those reports. delete_reports takes only a ticket, and asks the user to
confirm through ADK's confirmation request, which the CLI answers by asking the
user directly. The model never sees or relays the answer, and it cannot add
reports to a ticket or reuse one.
"""
import time
import uuid

from google.adk.tools import ToolContext

from .reports import TRASH_DAYS, ReportStore

TICKET_SECONDS = 120
BIG_BATCH = 10  # above this, the user types the number of reports instead of "yes"


def confirmation_prompt(reports: list[dict], expected: str) -> str:
    lines = ["Delete this report?" if len(reports) == 1 else f"Delete these {len(reports)} reports?"]
    lines += [f"  {i}. {r['title']}  ({r['created_at'][:10]})" for i, r in enumerate(reports, 1)]
    if expected == "yes":
        lines.append("Type yes to delete, anything else to cancel.")
    else:
        lines.append(f"That's a lot of reports. Type {expected} (the number of reports) to delete, anything else to cancel.")
    return "\n".join(lines)


def make_report_tools(store: ReportStore, owner: str) -> list:
    tickets: dict[str, dict] = {}  # ticket id -> the reports it covers and when it expires
    declined_in: set[str] = set()  # questions where the user said no: don't ask again

    def save_report(title: str, body: str, tool_context: ToolContext) -> dict:
        """Save a report to the user's Saved Reports library. Only do this when the user asks.

        Args:
            title: A short title, naming what the report covers (e.g. brand, period).
            body: The full report text, as shown to the user.
        """
        report = store.save(owner, tool_context.session.id, title, body)
        return {"saved": report}

    def find_reports(tool_context: ToolContext, text: str = "", this_conversation_only: bool = False) -> dict:
        """List the user's saved reports, optionally only those mentioning some text or made in this conversation.

        Call this to show the user their reports, and always before deleting: it returns a
        ticket that delete_reports needs.

        Args:
            text: Only reports whose title or body contains this text (case-insensitive), e.g. a brand name.
            this_conversation_only: Only reports saved during the current conversation.
        """
        conversation = tool_context.session.id if this_conversation_only else None
        reports = store.find(owner, text=text, conversation_id=conversation)
        if not reports:
            return {"reports": [], "message": "No matching reports. Nothing to delete."}
        ticket = uuid.uuid4().hex[:8]
        tickets[ticket] = {"reports": reports, "expires": time.monotonic() + TICKET_SECONDS}
        listed = [{k: r[k] for k in ("id", "title", "created_at")} for r in reports]
        return {"reports": listed, "ticket": ticket, "ticket_expires_in_seconds": TICKET_SECONDS}

    def read_report(report_id: str) -> dict:
        """Return the full text of one of the user's saved reports.

        Args:
            report_id: The id from find_reports.
        """
        return store.read(owner, report_id) or {"error": f"No saved report with id {report_id}."}

    def delete_reports(ticket: str, tool_context: ToolContext) -> dict:
        """Delete (move to trash) exactly the reports listed by find_reports.

        The program asks the user to confirm; do not ask them yourself, and do not
        claim anything was deleted unless this tool says so.

        Args:
            ticket: The ticket returned by find_reports.
        """
        if tool_context.invocation_id in declined_in:
            return {"error": "The user declined to delete in this question. Do not retry; tell them nothing was deleted."}
        issued = tickets.get(ticket)
        if not issued or time.monotonic() > issued["expires"]:
            tickets.pop(ticket, None)
            return {"error": "This ticket is unknown or expired. Call find_reports again so the user sees a fresh list."}
        reports = issued["reports"]
        expected = str(len(reports)) if len(reports) > BIG_BATCH else "yes"

        confirmation = tool_context.tool_confirmation
        if confirmation is None:  # first call: pause and let the program ask the user
            tool_context.request_confirmation(hint=confirmation_prompt(reports, expected))
            tool_context.actions.skip_summarization = True
            return {"status": "Waiting for the user to confirm."}

        del tickets[ticket]  # a ticket gets one answer, whatever it was
        answer = str((confirmation.payload or {}).get("answer", "")).strip().lower()
        if answer != expected:
            declined_in.add(tool_context.invocation_id)
            return {"status": "cancelled", "message": "Declined. Tell the user, speaking to them directly, that nothing was deleted. Do not ask again."}
        moved = store.trash(owner, tool_context.session.id, [r["id"] for r in reports])
        return {
            "status": "deleted",
            "moved_to_trash": moved,
            "message": f"Tell the user they can say 'undo' within {TRASH_DAYS} days to restore them.",
        }

    def undo_last_delete() -> dict:
        """Restore the reports from the user's most recent delete."""
        titles = store.undo_last_delete(owner)
        if not titles:
            return {"message": "There is no recent delete to undo."}
        return {"restored": titles}

    return [save_report, find_reports, read_report, delete_reports, undo_last_delete]
