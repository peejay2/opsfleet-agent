"""Saved Reports and the delete flow, checked with an in-memory database."""
from types import SimpleNamespace

import pytest

from opsfleet_agent import report_tools
from opsfleet_agent.report_tools import make_report_tools
from opsfleet_agent.reports import ReportStore


class FakeContext:
    """Stands in for ADK's ToolContext: a conversation id and the user's confirmation answer."""

    def __init__(self, conversation="conv-1", answer=None, question="q-1"):
        self.session = SimpleNamespace(id=conversation)
        self.invocation_id = question
        self.tool_confirmation = None if answer is None else SimpleNamespace(payload={"answer": answer})
        self.actions = SimpleNamespace(skip_summarization=False)
        self.hint = None

    def request_confirmation(self, hint):
        self.hint = hint


@pytest.fixture
def store():
    store = ReportStore(":memory:")
    store.save("anna", "conv-0", "Calvin Klein Q1 review", "Sales rose 12%.")
    store.save("anna", "conv-1", "Levi's vs Calvin Klein", "Levi's led in jeans.")
    store.save("anna", "conv-1", "Denim trends", "Jeans are flat.")
    store.save("ben", "conv-9", "Calvin Klein sweaters", "Ben's own report.")
    return store


def tools_for(store, owner="anna"):
    save, find, read, delete, undo = make_report_tools(store, owner)
    return SimpleNamespace(save=save, find=find, read=read, delete=delete, undo=undo)


def titles(result):
    return [r["title"] for r in result["reports"]]


def test_find_by_text_is_case_insensitive_and_searches_the_body():
    t = tools_for(store := ReportStore(":memory:"))
    store.save("anna", "c", "Quarterly", "calvin klein did well")
    assert titles(t.find(FakeContext(), text="Calvin Klein")) == ["Quarterly"]


def test_find_only_sees_the_users_own_reports(store):
    found = titles(tools_for(store).find(FakeContext(), text="Calvin Klein"))
    assert found == ["Calvin Klein Q1 review", "Levi's vs Calvin Klein"]  # not ben's


def test_find_this_conversation_only(store):
    found = titles(tools_for(store).find(FakeContext("conv-1"), this_conversation_only=True))
    assert found == ["Levi's vs Calvin Klein", "Denim trends"]


def test_no_matches_gives_no_ticket(store):
    result = tools_for(store).find(FakeContext(), text="Nike")
    assert result["reports"] == [] and "ticket" not in result


def test_delete_first_asks_the_program_to_confirm(store):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]
    ctx = FakeContext()
    result = t.delete(ticket, ctx)
    assert "Waiting" in result["status"] and ctx.actions.skip_summarization
    assert "Delete these 2 reports?" in ctx.hint and "Levi's vs Calvin Klein" in ctx.hint
    assert len(store.find("anna")) == 3  # nothing deleted yet


def test_yes_moves_exactly_the_listed_reports_to_trash(store):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]
    t.delete(ticket, FakeContext())
    result = t.delete(ticket, FakeContext(answer="Yes"))
    assert result["status"] == "deleted" and result["moved_to_trash"] == 2
    assert titles({"reports": store.find("anna")}) == ["Denim trends"]
    assert len(store.find("ben")) == 1  # ben's Calvin Klein report is untouched


def test_anything_but_yes_cancels(store):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]
    assert t.delete(ticket, FakeContext(answer="sure, go on"))["status"] == "cancelled"
    assert len(store.find("anna")) == 3


def test_a_ticket_gets_one_answer(store):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]
    t.delete(ticket, FakeContext(answer="no"))
    assert "error" in t.delete(ticket, FakeContext(answer="yes"))
    assert len(store.find("anna")) == 3


def test_after_a_no_the_agent_cannot_ask_again_in_the_same_question(store):
    t = tools_for(store)
    t.delete(t.find(FakeContext(), text="Calvin Klein")["ticket"], FakeContext(answer="no"))
    ctx = FakeContext()
    assert "declined" in t.delete(t.find(ctx, text="Calvin Klein")["ticket"], ctx)["error"]
    assert ctx.hint is None  # the user was not asked a second time
    ctx = FakeContext(question="q-2")  # a new question may ask again
    assert "Waiting" in t.delete(t.find(ctx, text="Calvin Klein")["ticket"], ctx)["status"]


def test_single_report_prompt_reads_naturally(store):
    t = tools_for(store)
    ctx = FakeContext()
    t.delete(t.find(ctx, text="Denim")["ticket"], ctx)
    assert ctx.hint.startswith("Delete this report?")


def test_made_up_ticket_is_refused(store):
    assert "error" in tools_for(store).delete("abcd1234", FakeContext(answer="yes"))


def test_another_users_ticket_is_refused(store):
    ticket = tools_for(store, "ben").find(FakeContext(), text="Calvin Klein")["ticket"]
    assert "error" in tools_for(store, "anna").delete(ticket, FakeContext(answer="yes"))


def test_expired_ticket_is_refused(store, monkeypatch):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]
    later = report_tools.time.monotonic() + 121
    monkeypatch.setattr(report_tools.time, "monotonic", lambda: later)
    assert "expired" in t.delete(ticket, FakeContext(answer="yes"))["error"]


def test_big_batch_needs_the_number_typed(store):
    for i in range(10):
        store.save("anna", "conv-2", f"Weekly {i}", "Calvin Klein numbers")
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]  # 12 reports
    ctx = FakeContext()
    t.delete(ticket, ctx)
    assert "Type 12" in ctx.hint
    assert t.delete(ticket, FakeContext(answer="yes"))["status"] == "cancelled"
    ticket = t.find(FakeContext(), text="Calvin Klein")["ticket"]  # the user asks again
    assert t.delete(ticket, FakeContext(answer="12", question="q-2"))["moved_to_trash"] == 12


def test_undo_restores_the_last_delete(store):
    t = tools_for(store)
    ticket = t.find(FakeContext(), text="Denim")["ticket"]
    t.delete(ticket, FakeContext(answer="yes"))
    assert t.undo()["restored"] == ["Denim trends"]
    assert len(store.find("anna")) == 3
    assert "no recent delete" in t.undo()["message"]


def test_undo_cannot_restore_another_users_delete(store):
    ben = tools_for(store, "ben")
    ben.delete(ben.find(FakeContext(), text="sweaters")["ticket"], FakeContext(answer="yes"))
    assert "no recent delete" in tools_for(store, "anna").undo()["message"]


def test_reports_trashed_over_30_days_ago_are_purged(store):
    store.db.execute("UPDATE reports SET trashed_at = '2020-01-01T00:00:00+00:00' WHERE title = 'Denim trends'")
    store.purge_old_trash()
    assert store.db.execute("SELECT COUNT(*) FROM reports WHERE owner = 'anna'").fetchone()[0] == 2


def test_save_records_the_conversation(store):
    t = tools_for(store)
    saved = t.save("Nike check", "None of my brands.", FakeContext("conv-7"))["saved"]
    assert store.find("anna", conversation_id="conv-7")[0]["id"] == saved["id"]
    assert t.read(saved["id"])["body"] == "None of my brands."
    assert "error" in tools_for(store, "ben").read(saved["id"])
