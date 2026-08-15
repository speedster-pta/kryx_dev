"""Shared unit-scoping SQL fragment builder.

Every unit-scoped list query needs the same "IN (?, ?, ...)" idiom:
unit_ids=None means unrestricted (superadmin sees everything), unit_ids=[]
means the caller has no unit access at all (the query must return zero
rows without executing), and a real list becomes an IN clause. Previously
hand-rolled independently in units.py, campaigns.py, and serving.py.
send_log.py already had its own correctly-factored version
(_scope_where), which additionally combines an optional
whatsapp_number_id filter, so it's left as-is rather than forced through
this more general-purpose helper.
"""


def unit_scope_clause(column: str, unit_ids: list[int] | None, joiner: str = "WHERE") -> tuple[str, list] | None:
    """Returns (sql_fragment, params) to append to a base query's SQL
    string and params list, or None meaning the caller has no accessible
    units and must return an empty result without querying.

    `joiner` is "WHERE" when the base query has no WHERE clause yet, or
    "AND" when extending one that already exists.
    """
    if unit_ids is None:
        return "", []
    if not unit_ids:
        return None
    placeholders = ",".join("?" for _ in unit_ids)
    return f" {joiner} {column} IN ({placeholders})", list(unit_ids)
