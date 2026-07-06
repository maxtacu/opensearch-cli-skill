# OpenSearch Log Analytics Guide (opensearch-cli)

Discovery-first log analytics. **Run the helper script before ad-hoc PPL** to avoid failed requests and wasted tokens.

## Helper script

Location: [scripts/log-analytics.py](scripts/log-analytics.py)

```bash
export OPENSEARCH_PROFILE=<PROFILE>

# Schema, detected fields, level values, sample rows
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py discover --index '<INDEX_PATTERN>'

# Last N hours: total, by_level, volume_by_bucket, recent_errors, top_error_messages, error_breakdown
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py analyze --index '<INDEX_PATTERN>' --hours 1

# Single PPL query (safe JSON body, no shell escaping)
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py ppl --query 'source=<PATTERN> | head 5'
```

The script:
- Writes PPL bodies to temp JSON files (never shell-escapes queries)
- Auto-sets `AWS_PROFILE` / `AWS_REGION` from `~/.opensearch-cli/config.yaml`
- Normalizes index names (appends `*` for date-suffixed daily indices)
- Computes UTC cutoffs for time windows (OS 1.x lacks `DATE_SUB`/`now()`)
- Detects timestamp/level/message/service fields from schema
- Discovers exact level strings before filtering errors
- Returns `error_rate_pct` and `error_breakdown` by module/funcName/namespace when present
- Applies [ppl-compat.md](ppl-compat.md) quoting rules for the `time` field

## Setup

Config: `~/.opensearch-cli/config.yaml`

```yaml
profiles:
  - name: <PROFILE>
    endpoint: https://<id>.<region>.es.amazonaws.com
    aws_iam: { profile: "<AWS_SSO_PROFILE>", service: es }   # AOS IAM
    max_retry: 3
    timeout: 10
```

AOS credentials — **three different names**:

| Variable | Source |
|---|---|
| `OPENSEARCH_PROFILE` | `profiles[].name` in config.yaml |
| `AWS_PROFILE` | `profiles[].aws_iam.profile` in config.yaml |
| `AWS_REGION` | Infer from endpoint hostname (`*.eu-west-1.es.amazonaws.com`) or set explicitly |

```bash
# Recommended: print exports from config
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py --profile <PROFILE> setup

# Or set manually:
export OPENSEARCH_PROFILE=<PROFILE>
export AWS_PROFILE=<AWS_IAM_PROFILE_FROM_CONFIG>
export AWS_REGION=<REGION>
aws sts get-caller-identity
```

The helper script auto-sets `AWS_PROFILE` and `AWS_REGION` from config when they are unset. You still need valid SSO/session credentials.

Connectivity (once per session):

```bash
opensearch-cli curl get --path "_cluster/health" --pretty
```

## Phase 1 — Discover indices

```bash
opensearch-cli curl get --path "_cat/indices/<INDEX_PATTERN>" \
  --query-params "v=true&h=index,health,docs.count&s=index"
```

Then run `log-analytics.py discover`. Prefer `_cat/indices/<pattern>` over listing all indices on large clusters.

## Phase 2 — Understand schema

**Do not use `describe` on hyphenated index names** — it often fails. Use:

```bash
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py discover --index '<PATTERN>'
```

Common field roles (verify against `detected` output — do not assume):

| Role | Common names |
|---|---|
| Timestamp | `@timestamp`, `timestamp`, `time` |
| Level | `level`, `log.level`, `severityText` |
| Message | `message`, `msg`, `body` |
| Service/source | `service.name`, `podName`, `host.name` |
| Correlation | `trace_id`, `traceId`, `request_id` |

## Phase 3 — Analyze

**Default:** `log-analytics.py analyze --index '<PATTERN>' --hours <N>`

Custom PPL templates (substitute detected field names and exact level strings):

```
source=<PATTERN> | where `time` > '<UTC_CUTOFF>' | stats count() as cnt by level | sort - cnt
source=<PATTERN> | where `time` > '<UTC_CUTOFF>' | stats count() as volume by span(time, 10m) | sort time
source=<PATTERN> | where `time` > '<UTC_CUTOFF>' and level = '<EXACT_LEVEL>' | fields time, <MESSAGE_FIELD> | sort - time | head 20
source=<PATTERN> | where `time` > '<UTC_CUTOFF>' and level = '<EXACT_LEVEL>' | top 10 <MESSAGE_FIELD>
source=<PATTERN> | where like(<MESSAGE_FIELD>, '%search term%') | sort - time | head 20
source=<PATTERN> | where match(<MESSAGE_FIELD>, 'search term') | sort - time | head 20
```

> **Message filters:** Use `like(field, '%term%')` or `match(field, 'term')`. Do **not** use SQL-style `field like '%term%'` — it silently returns 0 rows on OS 1.x.

> **Time filters:** See [ppl-compat.md](ppl-compat.md). Do not use `DATE_SUB(NOW(), INTERVAL …)` or `case()` on OS 1.x without validating first.

### Running PPL manually

Always use a JSON file:

```bash
echo '{"query": "source=<PATTERN> | head 5"}' > /tmp/ppl.json
opensearch-cli curl post --path "_plugins/_ppl" --data @/tmp/ppl.json --pretty
```

## Query DSL fallback

Use when PPL time functions or conditionals fail:

```bash
opensearch-cli curl post --path "<PATTERN>/_search" --data '{
  "size": 0,
  "query": {"bool": {"filter": [{"range": {"<TIMESTAMP_FIELD>": {"gte": "now-1h"}}}]}},
  "aggs": {"by_level": {"terms": {"field": "<LEVEL_FIELD>", "size": 20}}}
}' --pretty
```

Recent errors with sort:

```bash
opensearch-cli curl post --path "<PATTERN>/_search" --data '{
  "size": 20,
  "query": {"bool": {
    "must": [{"term": {"<LEVEL_FIELD>": "<EXACT_LEVEL>"}}],
    "filter": [{"range": {"<TIMESTAMP_FIELD>": {"gte": "now-1h"}}}]
  }},
  "sort": [{"<TIMESTAMP_FIELD>": "desc"}]
}' --pretty
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `aws region is not found` | Run `log-analytics.py setup`; export `AWS_REGION` (inferred from endpoint) |
| `giving up after N attempt(s)` on PPL | Check AWS creds/region; use concrete index not wildcard; retry (transient) |
| `SyntaxCheckException` on `DATE_SUB` / `now()` / `case()` | Use [ppl-compat.md](ppl-compat.md) alternatives or `log-analytics.py analyze` |
| `SemanticCheckException` on `` `time` `` in fields/sort | Bare `time` in fields/sort/span; backticks only in `where` |
| `describe` fails on index name | `source=<index> \| head 1` |
| 0 errors but errors expected | Wrong level casing — check `level_values` from `discover` |
| Message filter returns 0 rows | Prefer `match(field, 'term')`; avoid SQL-style `field like '%x%'`; `like()` may fail on analyzed fields — see [ppl-compat.md](ppl-compat.md) |
| `invalid data` from opensearch-cli | Broken JSON from shell escaping — use `@file.json` or helper script |
| 0 rows, no error | Pattern matches nothing — `_cat/indices/<pattern>`; try adding `*` suffix |
| SSO/credential errors in sandbox | Re-run with `required_permissions: ["all"]` |
| 403 on authinfo | Often normal without admin perms; proceed if data queries succeed |

## Advanced

- Cross-index correlation: find ID in one index, filter another on same field
- PPL `ad` / `patterns`: version-dependent — validate with `ppl` subcommand first
- AOS operational logs (slow/audit): usually in CloudWatch, not the search cluster

Shared PPL patterns: [log-analytics.md](../log-analytics/log-analytics.md)
