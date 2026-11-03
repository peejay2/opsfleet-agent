"""The safety guards, checked without calling BigQuery or Gemini."""
from types import SimpleNamespace

import pytest

from opsfleet_agent.access import AccessError, ProductScope, load_scope
from opsfleet_agent.safety import UnsafeQuery, redact_output, scope_query

DATASET = "bigquery-public-data.thelook_ecommerce"
BRANDS = ProductScope((("brand", ("Calvin Klein",)),))


def scoped(sql, scope=BRANDS):
    return scope_query(sql, DATASET, scope)[0]


def test_short_table_names_are_bound_to_filtered_tables():
    sql = scoped("SELECT category, COUNT(*) FROM products GROUP BY category")
    assert sql.startswith("WITH products AS (SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`")
    assert "brand IN UNNEST(@allowed_brand)" in sql
    assert "FROM products GROUP BY category" in sql


def test_allowed_values_are_passed_as_a_parameter():
    _, params = scope_query("SELECT 1", DATASET, BRANDS)
    assert params[0].name == "allowed_brand" and params[0].values == ["Calvin Klein"]


def test_distribution_center_rule_filters_by_center_name():
    sql = scoped("SELECT * FROM products", ProductScope((("distribution_center", ("Memphis TN",)),)))
    assert "distribution_centers` WHERE name IN UNNEST(@allowed_distribution_center)" in sql


def test_several_filters_must_all_match():
    scope = ProductScope((("category", ("Jeans",)), ("distribution_center", ("Houston TX",))))
    sql, params = scope_query("SELECT * FROM products", DATASET, scope)
    assert "category IN UNNEST(@allowed_category) AND distribution_center_id IN" in sql
    assert {p.name: p.values for p in params} == {
        "allowed_category": ["Jeans"], "allowed_distribution_center": ["Houston TX"],
    }


def test_users_table_has_no_personal_columns():
    sql = scoped("SELECT * FROM users")
    users_cte = sql.split("users AS (")[1].split(")")[0]
    for column in ("first_name", "last_name", "email", "street_address", "postal_code", "latitude"):
        assert column not in users_cte


def test_models_own_ctes_come_after_ours():
    sql = scoped("WITH totals AS (SELECT SUM(sale_price) s FROM order_items) SELECT s FROM totals")
    assert sql.index("order_items AS (") < sql.index("totals AS (")


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders WHERE TRUE",
    "SELECT 1; SELECT 2",
    "DROP TABLE orders",
    "SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`",  # bypasses the filter
    "SELECT * FROM thelook_ecommerce.users",
    "SELECT * FROM `region-us`.INFORMATION_SCHEMA.TABLES",
    "SELECT * FROM events",  # exists in the dataset but isn't offered
    "WITH products AS (SELECT * FROM orders) SELECT * FROM products",  # shadowing a filtered table
    "SELECT * FROM (SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`)",
    "not sql at all (",
])
def test_refused(sql):
    with pytest.raises(UnsafeQuery):
        scope_query(sql, DATASET, BRANDS)


def test_emails_are_redacted_from_answers():
    part = SimpleNamespace(text="Top buyer is jane.doe@example.com, id 42.")
    response = SimpleNamespace(content=SimpleNamespace(parts=[part]))
    redact_output(None, response)
    assert part.text == "Top buyer is [redacted], id 42."


def test_demo_users_load():
    assert load_scope("anna") == ProductScope((("brand", ("Calvin Klein", "Levi's")),))
    assert load_scope("dave").describe() == "category in Jeans and distribution center in Houston TX, New Orleans LA"


def test_unknown_user_is_refused():
    with pytest.raises(AccessError):
        load_scope("mallory")
