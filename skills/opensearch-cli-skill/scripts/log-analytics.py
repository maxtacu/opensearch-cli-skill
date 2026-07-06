#!/usr/bin/env python3
"""
OpenSearch log analytics helper for the log-analytics-cli skill.

Wraps opensearch-cli with safe PPL execution (JSON file bodies, no shell escaping)
and batches discovery + common analytics to minimize failed requests.

Requires: opensearch-cli, python3.9+
Env: OPENSEARCH_PROFILE (or --profile), AWS_PROFILE/AWS_REGION for AOS IAM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TIMESTAMP_CANDIDATES = ("@timestamp", "timestamp", "time", "event.created")
LEVEL_CANDIDATES = ("level", "log.level", "severity", "severityText")
MESSAGE_CANDIDATES = ("message", "msg", "body", "log", "event.original")
SERVICE_CANDIDATES = ("service.name", "podName", "kubernetes.pod_name", "host.name", "namespace")
ERROR_BREAKDOWN_FIELDS = ("module", "funcName", "namespace", "podName", "templateName")

ERROR_LEVEL_RE = re.compile(r"^(error|err|fatal|critical|severe)$", re.I)
WARN_LEVEL_RE = re.compile(r"^(warn|warning)$", re.I)
CONFIG_PATH = Path.home() / ".opensearch-cli" / "config.yaml"
AWS_REGION_FROM_HOST = re.compile(r"\.([a-z]{2}-[a-z]+-\d)\.es\.amazonaws\.com")


def _resolve_profile(profile: str | None) -> str | None:
    return profile or os.environ.get("OPENSEARCH_PROFILE")


def _profile_arg(profile: str | None) -> list[str]:
    p = _resolve_profile(profile)
    return ["--profile", p] if p else []


def _read_profile_config(profile: str | None) -> dict[str, str]:
    """Load endpoint and aws_iam.profile from ~/.opensearch-cli/config.yaml."""
    name = _resolve_profile(profile)
    if not name or not CONFIG_PATH.is_file():
        return {}
    text = CONFIG_PATH.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*-\s+name:\s*", text)
    for block in blocks[1:]:
        if not block.startswith(name):
            continue
        cfg: dict[str, str] = {"name": name}
        endpoint = re.search(r"^\s*endpoint:\s*(\S+)", block, re.M)
        if endpoint:
            cfg["endpoint"] = endpoint.group(1)
        aws_profile = re.search(r"^\s*profile:\s*(\S+)", block, re.M)
        if aws_profile:
            cfg["aws_profile"] = aws_profile.group(1)
        return cfg
    return {}


def _region_from_endpoint(endpoint: str) -> str | None:
    host = urlparse(endpoint).hostname or endpoint
    match = AWS_REGION_FROM_HOST.search(host)
    return match.group(1) if match else None


def _ensure_aws_env(profile: str | None) -> dict[str, str]:
    """Set AWS_PROFILE and AWS_REGION from opensearch-cli config when missing."""
    cfg = _read_profile_config(profile)
    applied: dict[str, str] = {}
    if cfg.get("aws_profile") and not os.environ.get("AWS_PROFILE"):
        os.environ["AWS_PROFILE"] = cfg["aws_profile"]
        applied["AWS_PROFILE"] = cfg["aws_profile"]
    if not os.environ.get("AWS_REGION") and cfg.get("endpoint"):
        region = _region_from_endpoint(cfg["endpoint"])
        if region:
            os.environ["AWS_REGION"] = region
            applied["AWS_REGION"] = region
    return applied


def _normalize_index_pattern(pattern: str, profile: str | None) -> str:
    """Append wildcard when the base name matches date-suffixed daily indices."""
    if "*" in pattern or "?" in pattern:
        return pattern
    try:
        indices = _cat_indices_text(f"{pattern}*", profile)
    except RuntimeError:
        return pattern
    if indices and all(row["index"].startswith(f"{pattern}-") for row in indices):
        return f"{pattern}*"
    return pattern


def _oscmd(args: list[str], profile: str | None = None) -> Any:
    _ensure_aws_env(profile)
    cmd = ["opensearch-cli", *args, *_profile_arg(profile), "--pretty"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if proc.returncode != 0:
        raise RuntimeError(err or out or "opensearch-cli failed")
    if not out:
        raise RuntimeError(err or "opensearch-cli returned empty response")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response: {out[:200]}") from exc


def _ppl(query: str, profile: str | None = None) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"query": query}, f)
        path = f.name
    try:
        return _oscmd(["curl", "post", "--path", "_plugins/_ppl", "--data", f"@{path}"], profile)
    finally:
        os.unlink(path)


def _ppl_rows(resp: dict[str, Any]) -> list[list[Any]]:
    if "error" in resp:
        raise RuntimeError(resp["error"].get("details") or resp["error"].get("reason") or str(resp["error"]))
    return resp.get("datarows") or []


def _ppl_schema(resp: dict[str, Any]) -> list[dict[str, str]]:
    if "error" in resp:
        raise RuntimeError(resp["error"].get("details") or resp["error"].get("reason") or str(resp["error"]))
    return resp.get("schema") or []


def _field_names(schema: list[dict[str, str]]) -> set[str]:
    return {col["name"] for col in schema}


def _pick(candidates: tuple[str, ...], available: set[str]) -> str | None:
    for name in candidates:
        if name in available:
            return name
    return None


def _pick_timestamp(schema: list[dict[str, str]]) -> str | None:
    """Prefer timestamp-typed fields; avoid string fields named 'timestamp'."""
    by_name = {col["name"]: col.get("type", "") for col in schema}
    available = set(by_name)
    for name in TIMESTAMP_CANDIDATES:
        if name in available and by_name[name] == "timestamp":
            return name
    return _pick(TIMESTAMP_CANDIDATES, available)


def _ppl_field(name: str, *, context: str) -> str:
    """Quote field names for PPL. Reserved words need backticks only in WHERE."""
    if "." in name or name.startswith("@"):
        return f"`{name}`"
    if name == "time" and context == "where":
        return "`time`"
    return name


def _utc_cutoff(hours: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _cat_indices_text(pattern: str, profile: str | None) -> list[dict[str, str]]:
    _ensure_aws_env(profile)
    cmd = [
        "opensearch-cli",
        "curl",
        "get",
        "--path",
        f"_cat/indices/{pattern}",
        "--query-params",
        "v=true&h=index,health,docs.count&s=index",
        *_profile_arg(profile),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    rows: list[dict[str, str]] = []
    for i, line in enumerate(proc.stdout.splitlines()):
        if i == 0 or not line.strip():
            continue
        parts = line.split()
        if parts:
            rows.append(
                {
                    "index": parts[0],
                    "health": parts[1] if len(parts) > 1 else "",
                    "docs.count": parts[2] if len(parts) > 2 else "",
                }
            )
    return rows


def _narrow_source(pattern: str, indices: list[dict[str, str]], hours: float) -> str:
    """Use latest concrete index for short windows on date-suffixed indices."""
    if hours > 48 or not indices:
        return pattern
    latest = indices[-1]["index"]
    if latest == pattern or "*" in pattern:
        # Date-suffixed indices end with YYYY-MM-DD on many log clusters
        if re.search(r"-\d{4}-\d{2}-\d{2}$", latest):
            return latest
    return pattern


def _concrete_index(pattern: str, profile: str | None) -> str:
    indices = _cat_indices_text(pattern, profile)
    if not indices:
        raise RuntimeError(f"No indices match pattern: {pattern}")
    return indices[-1]["index"]


def discover(pattern: str, profile: str | None, sample: int = 3) -> dict[str, Any]:
    pattern = _normalize_index_pattern(pattern, profile)
    indices = _cat_indices_text(pattern, profile)
    if not indices:
        return {"pattern": pattern, "error": "no_matching_indices", "indices": []}

    concrete = indices[-1]["index"]
    # Prefer concrete index for sampling — wildcards can timeout on large clusters.
    sample_resp = _ppl(f"source={concrete} | head {sample}", profile)
    schema = _ppl_schema(sample_resp)
    fields = _field_names(schema)

    ts_field = _pick_timestamp(schema)
    level_field = _pick(LEVEL_CANDIDATES, fields)
    message_field = _pick(MESSAGE_CANDIDATES, fields)
    service_field = _pick(SERVICE_CANDIDATES, fields)

    level_values: list[dict[str, Any]] = []
    if level_field:
        q = f"source={concrete} | stats count() as cnt by {_ppl_field(level_field, context='group')} | sort - cnt | head 20"
        try:
            resp = _ppl(q, profile)
            cols = [c["name"] for c in _ppl_schema(resp)]
            for row in _ppl_rows(resp):
                level_values.append({cols[i]: row[i] for i in range(len(cols))})
        except RuntimeError:
            pass

    ppl_hints: dict[str, str] = {
        "level_filter_note": "Use exact level strings from level_values (case-sensitive).",
        "describe_unreliable": "Prefer `source=<index> | head 1` over `describe` for hyphenated index names.",
    }
    if ts_field:
        ppl_hints["timestamp_filter"] = f"where {_ppl_field(ts_field, context='where')} > '<UTC_CUTOFF>'"
        ppl_hints["timestamp_in_fields_sort_span"] = ts_field
    else:
        ppl_hints["timestamp_filter"] = "no_timestamp_field_detected_use_query_dsl_range"
    if ts_field == "time":
        ppl_hints["timestamp_where"] = "Use backticks in where: `time` > '<UTC_CUTOFF>'"
        ppl_hints["timestamp_elsewhere"] = "Use bare time in fields, sort, span (no backticks)"

    return {
        "pattern": pattern,
        "concrete_index_sampled": concrete,
        "indices": indices,
        "fields": sorted(fields),
        "detected": {
            "timestamp": ts_field,
            "level": level_field,
            "message": message_field,
            "service": service_field,
        },
        "level_values": level_values,
        "ppl_hints": ppl_hints,
        "sample_rows": _ppl_rows(sample_resp),
        "schema": schema,
    }


def _error_levels(level_values: list[dict[str, Any]], level_field: str) -> list[str]:
    found: list[str] = []
    for item in level_values:
        val = item.get(level_field)
        if val is None:
            continue
        if ERROR_LEVEL_RE.match(str(val)):
            found.append(str(val))
    return found or ["ERROR", "error"]


def _stats_by_field(
    source: str,
    time_clause: str,
    err_filter: str,
    field: str,
    profile: str | None,
    *,
    top_n: int = 10,
) -> list[dict[str, Any]] | None:
    q = (
        f"source={source}{time_clause} | where {err_filter} "
        f"| stats count() as cnt by {_ppl_field(field, context='group')} | sort - cnt | head {top_n}"
    )
    try:
        resp = _ppl(q, profile)
        cols = [c["name"] for c in _ppl_schema(resp)]
        return [{cols[i]: row[i] for i in range(len(cols))} for row in _ppl_rows(resp)]
    except RuntimeError:
        return None


def setup(profile: str | None) -> dict[str, Any]:
    cfg = _read_profile_config(profile)
    name = _resolve_profile(profile)
    region = _region_from_endpoint(cfg["endpoint"]) if cfg.get("endpoint") else None
    exports: list[str] = []
    if name:
        exports.append(f"export OPENSEARCH_PROFILE={name}")
    if cfg.get("aws_profile"):
        exports.append(f"export AWS_PROFILE='{cfg['aws_profile']}'")
    if region:
        exports.append(f"export AWS_REGION={region}")
    return {
        "opensearch_profile": name,
        "endpoint": cfg.get("endpoint"),
        "aws_profile": cfg.get("aws_profile"),
        "aws_region_inferred": region,
        "export_commands": exports,
        "note": "OPENSEARCH_PROFILE and AWS_PROFILE are different — set both for AOS IAM.",
    }


def analyze(pattern: str, hours: float, profile: str | None, top_n: int = 10) -> dict[str, Any]:
    pattern = _normalize_index_pattern(pattern, profile)
    meta = discover(pattern, profile, sample=1)
    if meta.get("error"):
        return meta

    source = _narrow_source(pattern, meta.get("indices") or [], hours)

    ts = meta["detected"]["timestamp"]
    level = meta["detected"]["level"]
    message = meta["detected"]["message"]
    service = meta["detected"]["service"]
    cutoff = _utc_cutoff(hours)
    result: dict[str, Any] = {
        "pattern": pattern,
        "query_source": source,
        "window_hours": hours,
        "cutoff_utc": cutoff,
        "detected_fields": meta["detected"],
        "indices": meta["indices"],
    }

    if not ts:
        result["warning"] = "No timestamp field detected; running unbounded stats (may be slow)."
        time_clause = ""
    else:
        time_clause = f" | where {_ppl_field(ts, context='where')} > '{cutoff}'"

    # Total count
    total_q = f"source={source}{time_clause} | stats count() as total"
    try:
        rows = _ppl_rows(_ppl(total_q, profile))
        result["total"] = rows[0][0] if rows else 0
    except RuntimeError as e:
        result["total_error"] = str(e)

    total = result.get("total") or 0

    # Levels
    if level:
        lvl_q = (
            f"source={source}{time_clause} | stats count() as cnt "
            f"by {_ppl_field(level, context='group')} | sort - cnt"
        )
        try:
            resp = _ppl(lvl_q, profile)
            cols = [c["name"] for c in _ppl_schema(resp)]
            result["by_level"] = [{cols[i]: row[i] for i in range(len(cols))} for row in _ppl_rows(resp)]
        except RuntimeError as e:
            result["by_level_error"] = str(e)

        error_levels = _error_levels(meta.get("level_values") or [], level)
        err_filter = " or ".join(f"{_ppl_field(level, context='where')} = '{v}'" for v in error_levels)
        err_q = f"source={source}{time_clause} | where {err_filter}"
        if message:
            err_q += (
                f" | fields {_ppl_field(ts, context='fields') if ts else 'time'}, "
                f"{_ppl_field(service, context='fields') if service else 'service'}, "
                f"{_ppl_field(message, context='fields')} "
                f"| sort - {_ppl_field(ts, context='sort') if ts else 'time'} | head {top_n}"
            )
        else:
            err_q += f" | head {top_n}"
        try:
            resp = _ppl(err_q, profile)
            cols = [c["name"] for c in _ppl_schema(resp)]
            result["recent_errors"] = [{cols[i]: row[i] for i in range(len(cols))} for row in _ppl_rows(resp)]
        except RuntimeError as e:
            result["recent_errors_error"] = str(e)

        if message:
            top_q = f"source={source}{time_clause} | where {err_filter} | top {top_n} {_ppl_field(message, context='fields')}"
            try:
                resp = _ppl(top_q, profile)
                cols = [c["name"] for c in _ppl_schema(resp)]
                result["top_error_messages"] = [{cols[i]: row[i] for i in range(len(cols))} for row in _ppl_rows(resp)]
            except RuntimeError as e:
                result["top_error_messages_error"] = str(e)

        error_count = sum(
            row.get("cnt", 0)
            for row in result.get("by_level") or []
            if row.get(level) is not None and ERROR_LEVEL_RE.match(str(row[level]))
        )
        if total:
            result["error_count"] = error_count
            result["error_rate_pct"] = round(100 * error_count / total, 3)

        breakdown: dict[str, list[dict[str, Any]]] = {}
        available = set(meta.get("fields") or [])
        for field in ERROR_BREAKDOWN_FIELDS:
            if field not in available:
                continue
            rows = _stats_by_field(source, time_clause, err_filter, field, profile, top_n=top_n)
            if rows:
                breakdown[field] = rows
        if breakdown:
            result["error_breakdown"] = breakdown

    # Volume over time (optional — may timeout on large wildcard patterns)
    if ts:
        span_field = _ppl_field(ts, context="span")
        bucket = "10m" if hours <= 6 else "1h"
        vol_q = f"source={source}{time_clause} | stats count() as volume by span({span_field}, {bucket}) | sort {span_field}"
        try:
            resp = _ppl(vol_q, profile)
            cols = [c["name"] for c in _ppl_schema(resp)]
            result["volume_by_bucket"] = [{cols[i]: row[i] for i in range(len(cols))} for row in _ppl_rows(resp)]
        except Exception as e:  # noqa: BLE001 — report partial results
            result["volume_error"] = str(e)

    return result


def run_ppl(query: str, profile: str | None) -> dict[str, Any]:
    return _ppl(query, profile)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenSearch log analytics helper")
    parser.add_argument("--profile", help="opensearch-cli profile (default: OPENSEARCH_PROFILE)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_disc = sub.add_parser("discover", help="List indices, schema, detected fields, level values")
    p_disc.add_argument("--index", required=True, help="Index name or pattern (wildcards ok)")
    p_disc.add_argument("--sample", type=int, default=3, help="Sample row count")

    p_an = sub.add_parser("analyze", help="Run standard last-N-hours analytics")
    p_an.add_argument("--index", required=True, help="Index name or pattern")
    p_an.add_argument("--hours", type=float, default=1.0, help="Lookback window in hours")
    p_an.add_argument("--top", type=int, default=10, help="Top N for errors/messages")

    p_ppl = sub.add_parser("ppl", help="Run a single PPL query (safe JSON body)")
    p_ppl.add_argument("--query", required=True, help="PPL query string")

    sub.add_parser("setup", help="Print AWS/OpenSearch env exports from config.yaml")

    args = parser.parse_args()
    try:
        if args.cmd == "discover":
            out = discover(args.index, args.profile, sample=args.sample)
        elif args.cmd == "analyze":
            out = analyze(args.index, args.hours, args.profile, top_n=args.top)
        elif args.cmd == "setup":
            out = setup(args.profile)
        else:
            out = run_ppl(args.query, args.profile)
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
