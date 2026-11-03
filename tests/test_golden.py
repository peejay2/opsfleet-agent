"""The golden bucket: the trios themselves, and how they are found and handed to the model."""
from types import SimpleNamespace

import pytest

from opsfleet_agent.access import ProductScope
from opsfleet_agent.golden import GoldenBucket, examples_instruction, make_example_callback
from opsfleet_agent.safety import scope_query

TRIOS = GoldenBucket.load(embed=None).trios
WORDS = ["revenue", "churn", "state", "brand", "weather"]


def keyword_embed(texts, task_type):
    """A stand-in for Gemini embeddings: one dimension per keyword, plus a constant."""
    return [[1.0] + [float(w in text.lower()) for w in WORDS] for text in texts]


def bucket():
    return GoldenBucket([
        {"id": "rev", "question": "monthly revenue", "definitions": "d", "sql": "SELECT 1", "report": "r"},
        {"id": "churn", "question": "why did churn rise", "definitions": "d", "sql": "SELECT 2", "report": "r"},
        {"id": "state", "question": "spend by state", "definitions": "d", "sql": "SELECT 3", "report": "r"},
    ], keyword_embed)


@pytest.mark.parametrize("trio", TRIOS, ids=[t["id"] for t in TRIOS])
def test_each_trio_is_complete_and_its_sql_passes_the_safety_checks(trio):
    assert {"id", "question", "definitions", "sql", "report"} <= trio.keys() and trio["source"] == "analyst"
    scope_query(trio["sql"], "bigquery-public-data.thelook_ecommerce", ProductScope((("brand", ("Levi's",)),)))


def test_trios_have_unique_ids_and_there_are_10_to_15():
    ids = [t["id"] for t in TRIOS]
    assert len(ids) == len(set(ids)) and 10 <= len(ids) <= 15


def test_revenue_trios_leave_out_cancelled_and_returned_items():
    for trio in TRIOS:
        if "sale_price" in trio["sql"]:
            assert "'Cancelled'" in trio["sql"] and "'Returned'" in trio["sql"], trio["id"]


def test_closest_trio_comes_first():
    assert [t["id"] for _, t in bucket().similar("Why is churn up?", 3)][0] == "churn"


def test_unrelated_questions_get_no_trios():
    assert bucket().similar("what's the weather?", 3) == []


def test_trio_vectors_are_computed_once():
    calls = []
    b = bucket()
    b.embed = lambda texts, task: calls.append(task) or keyword_embed(texts, task)
    b.similar("churn", 1)
    b.similar("revenue", 1)
    assert calls == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "RETRIEVAL_QUERY"]


def test_instruction_includes_definitions_and_sql():
    text = examples_instruction(TRIOS[:2])
    assert TRIOS[0]["definitions"] in text and TRIOS[1]["sql"] in text


def context(question, question_id="q-1"):
    return SimpleNamespace(
        invocation_id=question_id,
        user_content=SimpleNamespace(parts=[SimpleNamespace(text=question)]),
    )


class FakeRequest:
    def __init__(self):
        self.added = []

    def append_instructions(self, texts):
        self.added += texts


def test_callback_adds_examples_and_looks_up_once_per_question():
    b = bucket()
    lookups = []
    original = b.similar
    b.similar = lambda q, k: lookups.append(q) or original(q, k)
    add = make_example_callback(b, 2)
    first, second = FakeRequest(), FakeRequest()
    add(context("why did churn rise?"), first)
    add(context("why did churn rise?"), second)  # same question, a later model call
    assert lookups == ["why did churn rise?"]
    assert "why did churn rise" in first.added[0] and first.added == second.added


def test_callback_survives_an_embedding_failure():
    b = bucket()
    b.embed = lambda texts, task: (_ for _ in ()).throw(RuntimeError("quota"))
    request = FakeRequest()
    assert make_example_callback(b, 2)(context("churn?"), request) is None
    assert request.added == []
