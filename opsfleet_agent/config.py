"""Settings, read once from the environment (and from .env if there is one)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    project: str            # GCP project that runs (and pays for) the BigQuery jobs
    dataset: str            # dataset the agent is allowed to analyse
    model: str
    max_bytes_billed: int   # BigQuery refuses any query that would scan more than this
    max_rows: int           # rows handed back to the model per query
    max_failed_queries: int  # failed or empty queries in a row before the agent must stop and explain
    max_model_calls: int    # Gemini calls per question, a backstop against the agent looping
    reports_db: str         # SQLite file holding the Saved Reports library
    embedding_model: str    # turns questions into vectors to find similar golden trios
    golden_examples: int    # golden trios added to the instructions per question
    log_file: str           # the activity log: every question, query, retry and failure


def load_settings() -> Settings:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise ConfigError("GOOGLE_CLOUD_PROJECT is not set. Add it to .env (see README).")
    if not os.environ.get("GOOGLE_API_KEY"):
        raise ConfigError("GOOGLE_API_KEY is not set. Get one at https://aistudio.google.com/apikey and put it in .env.")
    return Settings(
        project=project,
        dataset=os.environ.get("BQ_DATASET", "bigquery-public-data.thelook_ecommerce"),
        model=os.environ.get("MODEL", "gemini-3.5-flash-lite"),
        max_bytes_billed=int(os.environ.get("MAX_BYTES_BILLED", 10**9)),
        max_rows=int(os.environ.get("MAX_ROWS", 50)),
        max_failed_queries=int(os.environ.get("MAX_FAILED_QUERIES", 3)),
        max_model_calls=int(os.environ.get("MAX_MODEL_CALLS", 15)),
        reports_db=os.environ.get("REPORTS_DB", "reports.db"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001"),
        golden_examples=int(os.environ.get("GOLDEN_EXAMPLES", 3)),
        log_file=log_file(),
    )


def log_file() -> str:
    return os.environ.get("LOG_FILE", "logs/agent.jsonl")
