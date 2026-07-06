# PPL compatibility (OpenSearch 1.x)

Validated patterns for clusters where newer PPL syntax fails. When in doubt, run queries through `log-analytics.py` — it applies these rules automatically.

## Time filtering

| Syntax | Status on OS 1.x | Alternative |
|---|---|---|
| `DATE_SUB(NOW(), INTERVAL 1 HOUR)` | Fails (`SyntaxCheckException`) | Literal UTC cutoff (see below) |
| `now()` / `now() - 1 hour` | Fails | Literal UTC cutoff |
| `` `time` > '2026-01-15 12:00:00' `` | Works | Compute cutoff in script/shell |
| Query DSL `"gte": "now-1h"` | Works | Use for aggregations PPL cannot express |

**Safe time filter (PPL):**

```
source=<PATTERN> | where `time` > '<UTC_CUTOFF>'
```

Compute `<UTC_CUTOFF>` as `datetime.utcnow() - timedelta(hours=N)` formatted `YYYY-MM-DD HH:MM:SS`. The helper script does this in `analyze`.

## Field quoting

| Context | `time` field | Dotted fields (`log.level`) |
|---|---|---|
| `where` | `` `time` `` (backticks) | `` `log.level` `` |
| `fields`, `sort`, `span`, `by` | `time` (no backticks) | `` `log.level` `` |

Using backticks in `fields`/`sort`/`span` for `time` causes `SemanticCheckException`.

## Schema discovery

| Command | Status | Alternative |
|---|---|---|
| `describe my-index-name` | Fails on hyphenated names | `source=<index> \| head 1` — schema is in the response |
| `source=<pattern> \| head N` | Works with wildcards | Preferred |

When both `timestamp` (string) and `time` (timestamp) exist, **filter on the timestamp-typed field**. The helper script picks by type automatically.

## Aggregations and conditionals

| Syntax | Status | Alternative |
|---|---|---|
| `sum(case(level = 'ERROR', 1 else 0))` | Fails on many 1.x clusters | Separate queries with `where level = '<exact>'` |
| `eval category = case(...)` | Fails | Filter with `where like(message, '%…%')` or post-process in script |
| `stats count() by span(time, 10m)` | Works when `time` is unquoted in `span` | Use bucket size `10m` for ≤6h windows, `1h` for longer |

## Log level filtering

Levels are **case-sensitive**. Always read values from:

```
source=<PATTERN> | stats count() as cnt by level | sort - cnt
```

Filter with the exact string (`ERROR`, `error`, `INFO`, …). Do not assume uppercase.

## Message / text filtering

| Syntax | Status on OS 1.x | Example |
|---|---|---|
| `where match(message, 'timeout')` | Works (preferred) | Full-text search on analyzed fields |
| `where like(message, '%timeout%')` | Often 0 rows on analyzed/text fields | Wildcard — validate before relying on |
| `where message like '%timeout%'` | **Fails silently (0 rows)** | SQL-style — do not use |

Prefer `top N message` on error-filtered rows (built into `analyze`) over multiple message-filter queries. Use `match()` when you need to filter by keyword.

## Passing queries to opensearch-cli

**Do:**

```bash
echo '{"query": "source=idx* | head 5"}' > /tmp/q.json
opensearch-cli curl post --path "_plugins/_ppl" --data @/tmp/q.json --pretty
```

Or: `log-analytics.py ppl --query '…'`

**Do not:** inline JSON with backticks through the shell — escaping fails silently or produces invalid JSON.

## Query DSL fallback (time + errors)

When PPL time functions are unavailable:

```bash
opensearch-cli curl post --path "<PATTERN>/_search" --data '{
  "size": 0,
  "query": {"bool": {"filter": [{"range": {"<TIMESTAMP_FIELD>": {"gte": "now-1h"}}}]}},
  "aggs": {"by_level": {"terms": {"field": "<LEVEL_FIELD>", "size": 20}}}
}' --pretty
```

Replace field names with values from `discover` output.
