---
name: opensearch-cli-skill
description: >
  Analyze logs in OpenSearch using opensearch-cli, PPL, and Query DSL. Use this
  skill when the user wants to query logs from the terminal, investigate error
  patterns, discover log indices, check error rates, or debug application issues
  through log data without using the MCP server. Activate when the user mentions
  opensearch-cli, log analysis, PPL, error rate, log patterns, or log analytics
  and prefers CLI over MCP.
compatibility: Requires opensearch-cli installed and configured (~/.opensearch-cli/config.yaml). PPL requires the SQL plugin.
metadata:
  author: opensearch-project
  version: "2.0"
---

# OpenSearch Log Analytics (CLI)

Analyze logs via `opensearch-cli`. **Minimize round-trips:** use the helper script before writing ad-hoc PPL.

## Prerequisites

- `opensearch-cli` + profile in `~/.opensearch-cli/config.yaml`
- AOS: `AWS_PROFILE`, `AWS_REGION`, valid credentials (`aws sts get-caller-identity`)
- Shell with `required_permissions: ["all"]` when SSO/credential_process is used

**Important:** `OPENSEARCH_PROFILE` (opensearch-cli config name) ≠ `AWS_PROFILE` (IAM profile in `aws_iam.profile`). Both must be set for AOS.

## Fast path (always start here)

```bash
# 0) Print required exports from ~/.opensearch-cli/config.yaml (run once per session)
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py --profile <PROFILE> setup
# eval the export_commands, or rely on the script auto-setting AWS env from config

export OPENSEARCH_PROFILE=<PROFILE>   # optional if only one profile

# 1) Discover indices, schema, detected fields, level values
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py discover --index '<INDEX_PATTERN>'

# 2) Standard analysis for last N hours (total, levels, volume, recent errors, error_breakdown)
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py analyze --index '<INDEX_PATTERN>' --hours 1

# 3) Custom PPL only when needed (writes JSON body safely — no shell escaping)
python3 .agents/skills/log-analytics-cli/scripts/log-analytics.py ppl --query 'source=<PATTERN> | head 5'
```

Replace `<INDEX_PATTERN>` with the user-provided index or wildcard. Daily log indices often use date suffixes — pass the base name (e.g. `production-alert-routing-automation`); the script appends `*` when `_cat/indices` finds date-suffixed matches. **Never hardcode field names** — take them from `discover` output.

## Key rules

1. **Script first** — `setup` (if creds fail), then `discover` or `analyze`. Do not guess field names or PPL syntax.
2. **Discovery first** — if the user gives only a service name, resolve the index via `_cat/indices/<pattern>`.
3. **No shell-escaped PPL** — use `log-analytics.py ppl` or `opensearch-cli curl post --data @file.json`. Inline `--data '{"query":"..."}'` breaks on backticks/quotes.
4. **PPL 1.x compatibility** — see [ppl-compat.md](ppl-compat.md). Do not use `DATE_SUB(NOW(), …)`, `now()`, or `case()` until validated.
5. **Level values are case-sensitive** — use exact strings from `discover` (`ERROR` vs `error`).
6. **Reserved `time` field** — backticks in `where` only: `` where `time` > '…' ``; use bare `time` in `fields`, `sort`, `span`.
7. **Verify connectivity once** — `_cluster/health` (skip `authinfo` if it 403s; proceed if data queries work).
8. **Run commands yourself** — do not return unverified queries to the user.
9. **Message search** — prefer `where match(message, 'term')`; SQL-style `message like '%term%'` returns 0 rows; `like(field, '%term%')` may also return 0 on analyzed fields — validate first.

## Workflow

| Phase | Action |
|---|---|
| Connect | `log-analytics.py setup`; verify AWS creds for AOS |
| Discover | `log-analytics.py discover --index '<pattern>'` |
| Analyze | `log-analytics.py analyze --index '<pattern>' --hours <N>` (includes `error_breakdown`) |
| Deep dive | `log-analytics.py ppl --query '…'` using detected field names |
| Fallback | Query DSL with `now-1h` range when PPL time functions fail — see [log-analytics-cli.md](log-analytics-cli.md) |

## opensearch-cli essentials

```bash
opensearch-cli profile list
opensearch-cli curl get --path "_cat/indices/<PATTERN>" --query-params "v=true&h=index,docs.count&s=index"
opensearch-cli curl post --path "_plugins/_ppl" --data @query.json --pretty
```

## Reference files

| File | Content |
|---|---|
| [scripts/log-analytics.py](scripts/log-analytics.py) | Discovery + analysis helper (run this first) |
| [ppl-compat.md](ppl-compat.md) | PPL syntax that fails on OpenSearch 1.x and safe alternatives |
| [log-analytics-cli.md](log-analytics-cli.md) | Full workflow, Query DSL fallback, troubleshooting |
