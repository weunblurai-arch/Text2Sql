"""Create factual natural-language summaries directly from SQL results."""

def _display_value(column, value):
    if value is None:
        return "not available"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if any(token in column.lower() for token in ("revenue", "price", "spent", "order_value")):
            return "${:,.2f}".format(value)
        return "{:,.2f}".format(value) if isinstance(value, float) else "{:,}".format(value)
    return str(value)


def summarize(plan, result):
    """Summarize returned values only; works with curated and arbitrary AI plans."""
    rows = result.get("rows", [])
    if not rows:
        return "No records matched this query."
    columns = result.get("columns", list(rows[0]))
    chart = plan.get("chart") or {}
    x, y = chart.get("x"), chart.get("y")
    if len(rows) == 1:
        summary = "; ".join("{}: {}".format(column.replace("_", " ").capitalize(),
                                              _display_value(column, rows[0].get(column)))
                            for column in columns[:6]) + "."
    elif x in columns and y in columns and all(isinstance(row.get(y), (int, float)) for row in rows):
        highest = max(rows, key=lambda row: row[y])
        summary = "{} has the highest {} in these results at {}. The query returned {} rows.".format(
            highest[x], y.replace("_", " "), _display_value(y, highest[y]), len(rows))
    elif plan.get("title") == "Low-stock inventory" and "stock_remaining" in columns:
        empty_count = sum(row.get("stock_remaining") == 0 for row in rows)
        summary = "{} product/store combinations have fewer than 10 units; {} are out of stock in these results.".format(
            len(rows), empty_count)
    else:
        first = "; ".join("{}: {}".format(column.replace("_", " "), _display_value(column, rows[0].get(column)))
                          for column in columns[:4])
        summary = "The query returned {} rows. First result — {}.".format(len(rows), first)
    if result.get("truncated"):
        summary += " Results are limited to the first {} rows; additional rows exist.".format(len(rows))
    return summary
