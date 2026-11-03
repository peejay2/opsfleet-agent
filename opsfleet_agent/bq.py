"""Thin wrapper around the BigQuery client: run a query, read table schemas."""

from functools import cached_property

from google.cloud import bigquery

from .safety import UnsafeQuery


def jsonable(value):
    """Make a BigQuery value safe to hand to the model (dates, decimals -> str)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class BigQueryRunner:
    def __init__(self, project: str, dataset: str, max_bytes_billed: int) -> None:
        self.project = project
        self.dataset = dataset
        self.max_bytes_billed = max_bytes_billed

    @cached_property
    def client(self) -> bigquery.Client:
        # Created on first use so the CLI starts even before credentials are checked.
        return bigquery.Client(project=self.project)

    def query(self, sql: str, max_rows: int, params: list = ()) -> dict:
        """Run a query; return at most max_rows rows plus the total row count.

        A dry run first lets BigQuery itself confirm the query is a read-only SELECT
        over our dataset and within the cost limit, without running it.
        """
        dry = self.client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params))
        if dry.statement_type != "SELECT":
            raise UnsafeQuery(f"only SELECT is allowed, got {dry.statement_type}")
        outside = [t for t in dry.referenced_tables if f"{t.project}.{t.dataset_id}" != self.dataset]
        if outside:
            raise UnsafeQuery(f"query reads tables outside {self.dataset}")
        if dry.total_bytes_processed > self.max_bytes_billed:
            raise UnsafeQuery(f"query would scan {dry.total_bytes_processed / 1e9:.1f} GB, over the limit")

        config = bigquery.QueryJobConfig(maximum_bytes_billed=self.max_bytes_billed, query_parameters=params)
        result = self.client.query(sql, job_config=config).result(max_results=max_rows)
        rows = [{k: jsonable(v) for k, v in row.items()} for row in result]
        return {"rows": rows, "row_count": len(rows), "total_rows": result.total_rows}

    def schema(self, table: str) -> list[dict]:
        """Column names, types and descriptions of one table in the dataset."""
        t = self.client.get_table(f"{self.dataset}.{table}")
        return [
            {"name": f.name, "type": f.field_type, "description": f.description or ""}
            for f in t.schema
        ]
