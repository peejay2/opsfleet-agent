"""The golden bucket: analyst-written trios (question -> SQL -> report) in trios.json.

Before the model answers a question, the closest trios are added to its instructions
by code, so the agent follows the company's definitions without having to decide to
look them up. Saved reports never come in here: only reviewed trios are retrieved.
"""
import json
import logging
import math
from collections.abc import Callable
from pathlib import Path

from google import genai
from google.genai import types

from .recovery import GEMINI_RETRIES

log = logging.getLogger("opsfleet_agent.golden")

# Below this, a trio isn't really about the question (off-topic questions score about 0.55,
# related ones 0.7 and up with gemini-embedding-001). Retune if the embedding model changes.
MIN_SIMILARITY = 0.6

# (texts, task type) -> one vector per text
Embedder = Callable[[list[str], str], list[list[float]]]


def gemini_embedder(model: str) -> Embedder:
    client = genai.Client(http_options=types.HttpOptions(timeout=30_000, retry_options=GEMINI_RETRIES))

    def embed(texts: list[str], task_type: str) -> list[list[float]]:
        result = client.models.embed_content(
            model=model, contents=texts, config=types.EmbedContentConfig(task_type=task_type),
        )
        return [e.values for e in result.embeddings]

    return embed


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class GoldenBucket:
    def __init__(self, trios: list[dict], embed: Embedder) -> None:
        self.trios = trios
        self.embed = embed
        self.vectors: list[list[float]] | None = None  # embedded on first use, then kept

    @classmethod
    def load(cls, embed: Embedder) -> "GoldenBucket":
        return cls(json.loads(Path(__file__).with_name("trios.json").read_text()), embed)

    def similar(self, question: str, k: int) -> list[tuple[float, dict]]:
        """Up to k trios close to the question, best first, with their similarity scores."""
        if self.vectors is None:
            self.vectors = self.embed([t["question"] for t in self.trios], "RETRIEVAL_DOCUMENT")
        [query] = self.embed([question], "RETRIEVAL_QUERY")
        scored = sorted(zip((cosine(query, v) for v in self.vectors), self.trios), key=lambda p: -p[0])
        return [(score, trio) for score, trio in scored[:k] if score >= MIN_SIMILARITY]


def examples_instruction(trios: list[dict]) -> str:
    parts = [
        "Past analyses by the company's analysts, the closest ones to this question. Use their "
        "definitions and approach when they fit; ignore any that don't. Their SQL uses the same "
        "short table names, but write your own query for this question. The reports describe how "
        "to present the result, not real figures."
    ]
    for i, t in enumerate(trios, 1):
        parts.append(
            f"Example {i}: {t['question']}\nDefinitions: {t['definitions']}\n"
            f"SQL:\n{t['sql']}\nHow to report it: {t['report']}"
        )
    return "\n\n".join(parts)


def make_example_callback(bucket: GoldenBucket, k: int):
    """A before-model callback adding the closest trios to the instructions, looked up once per question."""
    looked_up: dict[str, str] = {}  # invocation id -> instruction text, for the current question only

    def add_examples(callback_context, llm_request):
        question_id = callback_context.invocation_id
        if question_id not in looked_up:
            looked_up.clear()
            content = callback_context.user_content
            question = " ".join(p.text for p in (content.parts if content else []) if p.text).strip()
            looked_up[question_id] = ""
            if question:
                try:
                    matches = bucket.similar(question, k)
                    found = ", ".join(f"{t['id']} ({score:.2f})" for score, t in matches)
                    log.info(f"[golden examples] {found or 'none close enough'}")
                    if matches:
                        looked_up[question_id] = examples_instruction([t for _, t in matches])
                except Exception as e:  # answer without examples rather than fail the question
                    log.warning(f"[golden examples] unavailable, answering without them: {e}")
        if looked_up[question_id]:
            llm_request.append_instructions([looked_up[question_id]])
        return None

    return add_examples
