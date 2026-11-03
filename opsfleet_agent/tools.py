"""Tools the agent can call. ADK reads each function's name, signature and docstring."""
from google.adk.tools import ToolContext
from google.api_core import exceptions as gcp
from google.auth import exceptions as auth

from .access import ProductScope
from .bq import BigQueryRunner
from .config import Settings
from .safety import USER_COLUMNS, UnsafeQuery, scope_query, scoped_tables


def make_tools(settings: Settings, bq: BigQueryRunner, scope: ProductScope) -> list:
    table_names = list(scoped_tables(settings.dataset, scope))
    schemas: dict[str, list[dict]] = {}  # table schemas never change mid-session
    failures = {"question": None, "count": 0}  # failed or empty queries in a row, per question

    def describe_tables() -> dict:
        """Return the columns (name, type, description) of every table you can query.

        Call this before writing SQL if you are unsure which columns exist, and to
        answer questions about what data is available.
        """
        try:
            for table in table_names:
                if table not in schemas:
                    columns = bq.schema(table)
                    if table == "users":  # personal columns are not queryable, so don't list them
                        columns = [c for c in columns if c["name"] in USER_COLUMNS]
                    schemas[table] = columns
        except Exception as e:
            return {"error": bigquery_failure(e)}
        return {"tables": schemas, "visible_data": f"only products where {scope.describe()}, and their sales"}

    def run_sql(sql: str, tool_context: ToolContext) -> dict:
        """Run one read-only BigQuery SQL query and return the first rows of the result.

        Refer to tables by their short names only: orders, order_items, products, users,
        distribution_centers. Aggregate in SQL rather than fetching raw rows: only the
        first rows are returned, and total_rows tells you how many the query produced.

        Args:
            sql: A single SELECT (or WITH ... SELECT) statement in BigQuery Standard SQL.
        """
        if failures["question"] != tool_context.invocation_id:  # a new question starts a new count
            failures.update(question=tool_context.invocation_id, count=0)
        if failures["count"] >= settings.max_failed_queries:
            return {"error": (
                f"Stopped: {failures['count']} queries in a row failed or came back empty for this question. "
                "Do not call run_sql again. Tell the user what you tried, what went wrong, and suggest "
                "how they could rephrase the question."
            )}

        try:
            scoped_sql, params = scope_query(sql, settings.dataset, scope)
            result = bq.query(scoped_sql, max_rows=settings.max_rows, params=params)
        except UnsafeQuery as e:
            failures["count"] += 1
            return {"error": f"Query refused: {e}. Rewrite the query."}
        except (gcp.BadRequest, gcp.NotFound) as e:  # a mistake in the SQL: the model can fix it
            failures["count"] += 1
            return {"error": f"{e.message}. Fix the query and try again."}
        except Exception as e:  # BigQuery itself is unavailable: retrying won't help
            return {"error": bigquery_failure(e)}

        if result["total_rows"] == 0:
            failures["count"] += 1
            result["note"] = (
                "No rows. Before concluding there is no data, check the filters: exact spelling and "
                "case of values (look them up with SELECT DISTINCT), and the date range."
            )
        else:
            failures["count"] = 0
        return result

    return [describe_tables, run_sql]


def bigquery_failure(error: Exception) -> str:
    """Error text for problems the model can't fix by rewriting the query."""
    if isinstance(error, (auth.DefaultCredentialsError, auth.RefreshError)):
        problem = "the Google Cloud login is missing or expired (run: gcloud auth application-default login)"
    elif isinstance(error, gcp.Forbidden):
        problem = "access was denied, or the BigQuery quota is used up"
    else:
        problem = f"BigQuery is unavailable ({type(error).__name__}: {error})"
    return f"Data service problem: {problem}. Do not retry. Tell the user the data can't be reached right now."
