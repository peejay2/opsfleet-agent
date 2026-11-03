"""Which products each user may analyse. Filters live in access.json, one set per user.

A user's filters are combined with AND: a product is visible only if it passes every
one, e.g. category Jeans AND distribution center Houston TX. Within one filter the
listed values are alternatives (brand Calvin Klein OR Levi's).

"Products related to him" is undefined in the brief, so this format is our assumption
and a question for OpsFleet. Not supported yet: department (Men/Women), an "all" for
the CEO, and OR across filters ("Nike, plus all of Swim").
"""
import json
from dataclasses import dataclass
from pathlib import Path

FILTER_FIELDS = ("brand", "category", "distribution_center")


class AccessError(Exception):
    """The user is unknown or their filters are malformed."""


@dataclass(frozen=True)
class ProductScope:
    filters: tuple[tuple[str, tuple[str, ...]], ...]   # (field, allowed values); all must match

    def describe(self) -> str:
        return " and ".join(f"{field.replace('_', ' ')} in {', '.join(values)}" for field, values in self.filters)


def load_scope(user: str) -> ProductScope:
    users = json.loads(Path(__file__).with_name("access.json").read_text())
    if user not in users:
        raise AccessError(f"Unknown user '{user}'. Known users: {', '.join(users)}.")
    rule = users[user]
    if not rule or any(field not in FILTER_FIELDS or not values for field, values in rule.items()):
        raise AccessError(
            f"User '{user}' needs at least one filter, each one of {', '.join(FILTER_FIELDS)} with a list of values."
        )
    return ProductScope(tuple((field, tuple(values)) for field, values in rule.items()))
