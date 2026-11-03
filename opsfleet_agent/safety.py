"""Guards enforced in code on every query and every answer, whatever the model says.

The model writes SQL against short table names (orders, products, ...). Before the
query runs, those names are bound to filtered versions of the real tables: only the
user's own products, the sales and customers linked to them, and no personal columns.
"""
import re

import sqlglot
from google.cloud import bigquery
from sqlglot import exp

from .access import ProductScope

# Columns of `users` the model may see. Names, emails, addresses and coordinates are left out.
USER_COLUMNS = ("id", "age", "gender", "city", "state", "country", "traffic_source", "created_at")

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class UnsafeQuery(Exception):
    """The query was refused before it ran."""


def scoped_tables(dataset: str, scope: ProductScope) -> dict[str, str]:
    """SQL for each table the model can query, filtered to the user's products.

    Order matters: each table is defined in terms of the ones before it.
    """
    conditions = []
    for field, _ in scope.filters:
        if field == "distribution_center":
            conditions.append(
                f"distribution_center_id IN "
                f"(SELECT id FROM `{dataset}.distribution_centers` WHERE name IN UNNEST(@allowed_{field}))"
            )
        else:
            conditions.append(f"{field} IN UNNEST(@allowed_{field})")
    product_filter = " AND ".join(conditions)
    return {
        "products": f"SELECT * FROM `{dataset}.products` WHERE {product_filter}",
        "order_items": f"SELECT * FROM `{dataset}.order_items` WHERE product_id IN (SELECT id FROM products)",
        "orders": f"SELECT * FROM `{dataset}.orders` WHERE order_id IN (SELECT order_id FROM order_items)",
        "users": (
            f"SELECT {', '.join(USER_COLUMNS)} FROM `{dataset}.users` "
            f"WHERE id IN (SELECT user_id FROM order_items)"
        ),
        "distribution_centers": (
            f"SELECT * FROM `{dataset}.distribution_centers` "
            f"WHERE id IN (SELECT distribution_center_id FROM products)"
        ),
    }


def scope_query(sql: str, dataset: str, scope: ProductScope) -> tuple[str, list]:
    """Check the model's SQL and rewrite it so it can only read the user's own data.

    Returns the SQL to run and its query parameters. Raises UnsafeQuery unless the
    input is a single SELECT that reads only the short table names (or its own CTEs).
    """
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.ParseError as e:
        raise UnsafeQuery(f"could not parse the SQL: {e}")
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise UnsafeQuery("send exactly one SELECT statement")
    query = statements[0]

    tables = scoped_tables(dataset, scope)
    own_ctes = {cte.alias.lower() for cte in query.find_all(exp.CTE)}
    if clash := own_ctes & tables.keys():
        raise UnsafeQuery(f"{', '.join(sorted(clash))} is a table name; give your CTE another name")
    for table in query.find_all(exp.Table):
        if table.db or table.catalog or table.name.lower() not in tables.keys() | own_ctes:
            raise UnsafeQuery(f"{table.sql('bigquery')} is not available; use only {', '.join(tables)}")

    # Put the filtered tables first, ahead of any CTEs the model wrote itself.
    ours = [
        exp.CTE(this=sqlglot.parse_one(body, read="bigquery"), alias=exp.TableAlias(this=exp.to_identifier(name)))
        for name, body in tables.items()
    ]
    theirs = query.args["with_"].expressions if query.args.get("with_") else []
    query.set("with_", exp.With(expressions=ours + theirs))

    params = [
        bigquery.ArrayQueryParameter(f"allowed_{field}", "STRING", list(values)) for field, values in scope.filters
    ]
    return query.sql(dialect="bigquery"), params


def redact_output(callback_context, llm_response):
    """Last line of defence: blank out email addresses in anything the model says."""
    if not llm_response.content:
        return None
    for part in llm_response.content.parts or []:
        if part.text:
            part.text = EMAIL.sub("[redacted]", part.text)
    return llm_response
