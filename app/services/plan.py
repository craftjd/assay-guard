"""Where each row of an import stands against main, and what saving would do.

The import page and the write path both call build_plan. A digest of each key
list is the confirmation. If main or the run moves, the digest no longer matches.

A preview plan classifies the raw rows again in memory with the loaded config.
It is compared with the latest stored run and cannot write.
"""

from __future__ import annotations

import json

from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.diff import count_kinds, kinds_phrase, run_changes, version_changes
from app.services.names import Names, import_names
from app.services.processing import ROWS_PAGE, lineage, main_snapshot, stored_rows
from app.services.rules import (
    RULES_ID,
    absent_keys,
    business_values,
    current_rules,
    failure_fields,
    failure_list,
    field_changes,
    key_digest,
    parse_decimal,
    payload_digest,
    same_business,
)
from app.services.when import friendly

# Where a row of this file stands against main right now. Order is display order.
STATES = (
    ("saved", "Saved", "ok"),
    ("in_main", "Already in main", "ok-soft"),
    ("newer", "Newer in main", "dim"),
    ("new", "New", "act"),
    ("update", "Different", "warn"),
    ("remove", "Fails config, in main", "err"),
    ("rejected", "Fails config", "err-soft"),
    ("dismissed", "Dismissed", "dim"),
    ("rerun", "Out of date", "warn-soft"),
)

# What a dismissed proposal leaves alone, by the kind of change it was.
DISMISS_WORDS = {
    "new": "Not added to main",
    "update": "Main value kept",
    "remove": "Kept in main",
    "absent": "Kept in main",
}
STATE_LABEL = {code: label for code, label, _tone in STATES}
STATE_TONE = {code: tone for code, _label, tone in STATES}

OUTCOME_WORDS = {
    "good": ("new", "new"),
    "already_same": ("same as main", "same as main"),
    "changed": ("different", "different"),
    "invalid": ("fails config", "fail config"),
}

_MANIFEST = (
    ("inserted", "Added"),
    ("replaced", "Updated"),
    ("replace", "Updated"),
    ("set_aside", "Removed"),
    ("absent", "Removed"),
    ("voided", "Removed (voided)"),
    ("rolled_back", "Removed (rolled back)"),
    ("restored", "Put back (rolled back)"),
)


def load_plan(import_id: int) -> dict:
    with get_conn() as conn:
        return build_plan(conn, import_id)


def load_preview(import_id: int) -> dict:
    """Process again in memory with the loaded config. Nothing is stored."""
    from app.services.processing import preview_import

    with get_conn() as conn:
        evaluation = preview_import(conn, import_id)
        conn.rollback()
        return build_plan(conn, import_id, preview=evaluation)


def build_plan(conn, import_id: int, preview: dict | None = None) -> dict:
    from app.services.config_watch import state_for

    rules = current_rules()
    with conn.cursor(DictCursor) as cur:
        cur.execute(
            """
            SELECT b.import_id, b.group_key, b.status, b.replaces_import_id,
                   b.archived_at, b.archived_reason, f.file_name
            FROM import_batch b
            LEFT JOIN import_file f ON f.import_id = b.import_id
            WHERE b.import_id = %s
            """,
            (import_id,),
        )
        batch = cur.fetchone()
        if batch is None:
            raise ValueError(f"Import {import_id} was not found.")
        cur.execute(
            """
            SELECT run_id, run_no, config_version, finished_at, started_at, summary_json
            FROM processing_run
            WHERE import_id = %s
            ORDER BY run_id DESC
            """,
            (import_id,),
        )
        runs = cur.fetchall()
        chain = lineage(cur, import_id)
        names = Names(import_names(cur))
        file_names = {key: value["file"] for key, value in names.items()}
        base = _base(batch, names, cur, import_id)
        if preview is None and not runs:
            return _empty(base, "missing", "")

        if preview is not None:
            latest = {"run_id": None, "run_no": None, "config_version": cfg.CONFIG_VERSION,
                      "finished_at": None, "started_at": None}
            summary = {"status": preview["status"], "reason": preview["reason"],
                       "counts": preview["counts"], "rules": RULES_ID}
            latest_rows = {row["raw_id"]: dict(row) for row in preview["rows"]}
            config = {"state": "current", "affected": 0, "counts": {}, "run_config": cfg.CONFIG_VERSION}
        else:
            latest = runs[0]
            summary = _json_dict(latest["summary_json"])
            latest_rows = stored_rows(cur, int(latest["run_id"]))
            config = state_for(cur, int(latest["run_id"]), latest["config_version"])
        status = summary.get("status") or "failed"
        main_rows = main_snapshot(cur)
        dismissals = _dismissals(cur, import_id)
        group_main = _group_main(cur, batch["group_key"]) if status == "completed" else set()
        duplicate_of = _earlier_same_bytes(cur, import_id)
        before_rows = None
        if batch["replaces_import_id"] is not None:
            cur.execute(
                "SELECT run_id FROM processing_run WHERE import_id = %s ORDER BY run_id DESC LIMIT 1",
                (int(batch["replaces_import_id"]),),
            )
            earlier = cur.fetchone()
            if earlier is not None:
                before_rows = stored_rows(cur, int(earlier["run_id"]))
        preview_changes = None
        if preview is not None and runs:
            changes, settled = run_changes(
                stored_rows(cur, int(runs[0]["run_id"])), latest_rows, rules, main_rows, set(chain)
            )
            preview_changes = {"rows": changes, "counts": count_kinds(changes),
                               "phrase": kinds_phrase(count_kinds(changes)), "settled": settled}

    own = set(chain)

    def source(current: dict) -> dict:
        main_id = current["import_id"]
        return {"main_import_id": main_id, "main_file": file_names.get(main_id, ""),
                "main_label": names[main_id]["label"], "main_title": names[main_id]["title"]}

    rows: list[dict] = []
    adds, replacements, set_asides, conflicts, dismissed = [], [], [], [], []

    def is_dismissed(key: str, kind: str, sha: str) -> bool:
        return bool(key) and dismissals.get((key, kind), {}).get("sha") == sha
    file_keys: set[str] = set()
    if status == "completed":
        for row in latest_rows.values():
            key = row["row_key"]
            if key:
                file_keys.add(key)
            payload = row["payload"]
            values, _ = business_values(payload, rules)
            current = main_rows.get(key) if key else None
            state, note, fields = _state(row, values, current, own, import_id, rules, names)
            sha = ""
            if state in {"new", "update"}:
                sha = payload_digest(values)
            elif state == "remove":
                sha = payload_digest(current["payload"])
            if sha and is_dismissed(key, state, sha):
                dismissed.append(_dismissed_entry(key, state, dismissals[(key, state)], values))
                note = DISMISS_WORDS[state]
                state = "dismissed"
            entry = {
                "raw_id": row["raw_id"],
                "source_row_no": row.get("source_row_no"),
                "row_key": key,
                "outcome": row["outcome"],
                "reason": row["reason"],
                "values": values,
                "state": state,
                "note": note,
                "fields": fields,
                "tone": STATE_TONE[state],
                "label": STATE_LABEL[state],
                "main_import_id": current["import_id"] if current else None,
            }
            rows.append(entry)
            if state == "new":
                adds.append({"row_key": key, "values": values, "sha": sha})
            elif state == "update":
                archived, _ = business_values(current["payload"], rules)
                replacements.append(
                    {
                        "row_key": key,
                        "changes": [
                            {"field": name, "main": old, "file": new}
                            for name, old, new in field_changes(current["payload"], payload, rules)
                        ],
                        "before": _show(archived),
                        "after": _show(values),
                        "sha": sha,
                        **source(current),
                    }
                )
            elif state == "remove":
                archived, _ = business_values(current["payload"], rules)
                set_asides.append(
                    {
                        "row_key": key,
                        "reason": "; ".join(reason for _n, reason in failure_list(current["payload"], rules)),
                        "before": _show(archived),
                        "fields": failure_fields(current["payload"], rules),
                        "sha": sha,
                        **source(current),
                    }
                )
            elif state == "rerun":
                conflicts.append({"row_key": key, "note": note})
    absent = []
    if status == "completed":
        for key in absent_keys(group_main, file_keys):
            current = main_rows.get(key)
            if current is None or current["import_id"] > import_id and current["import_id"] not in own:
                continue
            archived, _ = business_values(current["payload"], rules)
            sha = payload_digest(current["payload"])
            if is_dismissed(key, "absent", sha):
                dismissed.append(_dismissed_entry(key, "absent", dismissals[(key, "absent")], archived))
                continue
            absent.append({"row_key": key, "before": _show(archived), "sha": sha, **source(current)})

    state_counts = {code: 0 for code, _l, _t in STATES}
    for row in rows:
        state_counts[row["state"]] += 1
    rules_match = summary.get("rules") == RULES_ID
    config_ok = config["state"] in {"current", "unaffected", "kept"}
    archived = base["archived"]
    pending = len(adds) + len(replacements) + len(set_asides) + len(absent)
    can_write = (
        preview is None
        and status == "completed"
        and config_ok
        and rules_match
        and duplicate_of is None
        and archived is None
    )
    plan = {
        **base,
        "is_preview": preview is not None,
        "status": status,
        "reason": summary.get("reason") or "",
        "run_id": int(latest["run_id"]) if latest["run_id"] is not None else None,
        "run_no": _run_no(latest),
        "run_count": len(runs),
        "run_when": friendly(latest["finished_at"] or latest["started_at"]) if latest["run_id"] else "",
        "config_version": latest["config_version"],
        "config": config,
        "config_match": config_ok,
        "rules_match": rules_match,
        "counts": _counts(summary.get("counts")),
        "rows": rows,
        "states": [
            {"code": code, "label": label, "tone": tone, "count": state_counts[code]}
            for code, label, tone in STATES
        ],
        "state_counts": state_counts,
        "row_total": len(rows),
        "adds": adds,
        "add_groups": _add_groups(adds, rules),
        "replacements": replacements,
        "set_asides": set_asides,
        "absent": absent,
        "conflicts": conflicts,
        "dismissed": dismissed,
        "pending": pending,
        "add_digest": key_digest([row["row_key"] for row in adds]),
        "replace_digest": key_digest([row["row_key"] for row in replacements]),
        "set_aside_digest": key_digest([row["row_key"] for row in set_asides]),
        "absent_digest": key_digest([row["row_key"] for row in absent]),
        "can_write": can_write,
        "duplicate_of": duplicate_of,
        "duplicate_note": (
            f"Same file as {names[duplicate_of]['label']}, which was stored first." if duplicate_of else ""
        ),
        "duplicate_label": names[duplicate_of]["label"] if duplicate_of else "",
        "history": _history(runs, names) if preview is None else [],
        "names": names,
        "preview_changes": preview_changes,
        "version": _version(before_rows, latest_rows, rules, base["replaces"]) if status == "completed" else None,
        "columns": _columns(rules),
    }
    plan["verdict"] = _verdict(plan)
    return plan


def label_for(plan: dict) -> tuple[str, str]:
    """Status on the Process list. Same plan the import page shows."""
    if plan["archived"]:
        return "archived", {"replaced": "Replaced", "rolled_back": "Rolled back"}.get(plan["archived"]["reason"], "Voided")
    if plan["status"] == "missing":
        return "not_run", "Not processed"
    if plan["status"] != "completed":
        return "failed", "Could not process"
    if plan["duplicate_of"] is not None:
        return "duplicate", "Duplicate file"
    if not plan["rules_match"] or plan["conflicts"]:
        return "rerun", "Process again"
    if not plan["config_match"]:
        return "config", "Config changed"
    if plan["pending"]:
        return "review", "Ready to save"
    return "matches", "Up to date"


def filter_rows(plan: dict, state: str | None, query: str, page: int) -> dict:
    rows = plan["rows"]
    if state in STATE_LABEL:
        rows = [row for row in rows if row["state"] == state]
    if query:
        rows = [row for row in rows if _matches(row, query, row["note"])]
    return _page(rows, page)


def _matches(row: dict, query: str, text: str | None) -> bool:
    """Sample, status, any value, the note or reason, or the exact file line."""
    needle = query.strip().lower()
    if not needle:
        return True
    if str(row.get("source_row_no")) == needle:
        return True
    haystack = " ".join(
        [
            row.get("row_key") or "",
            row.get("label") or STATE_LABEL.get(row.get("state") or "", ""),
            text or "",
            *(str(value) for value in (row.get("values") or {}).values()),
        ]
    ).lower()
    return needle in haystack


def run_detail(import_id: int, run_no: int, outcome: str | None, query: str, page: int) -> dict:
    """A stored run as it was: what it found, how it differs from the run before, what it saved."""
    rules = current_rules()
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """
                SELECT run_id, run_no, config_version, finished_at, started_at, summary_json
                FROM processing_run
                WHERE import_id = %s
                ORDER BY run_id
                """,
                (import_id,),
            )
            runs = cur.fetchall()
            run = next((item for item in runs if int(item["run_no"] or 0) == run_no), None)
            if run is None:
                raise ValueError(f"Run {run_no} of import {import_id} was not found.")
            index = runs.index(run)
            previous = runs[index - 1] if index else None
            summary = _json_dict(run["summary_json"])
            this_rows = stored_rows(cur, int(run["run_id"]))
            changes: list[dict] = []
            settled = 0
            if previous is not None:
                changes, settled = run_changes(
                    stored_rows(cur, int(previous["run_id"])),
                    this_rows,
                    rules,
                    main_snapshot(cur),
                    set(lineage(cur, import_id)),
                )
            names = Names(import_names(cur))
    rows = list(this_rows.values())
    if outcome in OUTCOME_WORDS:
        rows = [row for row in rows if row["outcome"] == outcome]
    for row in rows:
        row["values"], _ = business_values(row["payload"], rules)
        row["fields"] = failure_fields(row["payload"], rules) if row["outcome"] == "invalid" else []
    if query:
        rows = [row for row in rows if _matches(row, query, row["reason"])]
    entry = _history_entry(run, names)
    return {
        "run_no": run_no,
        "run_count": len(runs),
        "is_latest": index == len(runs) - 1,
        "config_version": run["config_version"],
        "when": friendly(run["finished_at"] or run["started_at"]),
        "status": summary.get("status") or "failed",
        "reason": summary.get("reason") or "",
        "counts": _counts(summary.get("counts")),
        "found": entry["found"],
        "change_note": entry["changes"],
        "written": _written(summary),
        "previous_run_no": int(previous["run_no"] or 0) if previous else None,
        "previous_config": previous["config_version"] if previous else None,
        "changes": changes,
        "change_phrase": kinds_phrase(count_kinds(changes)),
        "settled": settled,
        "rows": _page(rows, page),
        "outcome": outcome or "",
        "query": query,
        "columns": _columns(rules),
    }


def _state(row: dict, values: dict, current: dict | None, own: set[int], import_id: int, rules, names):
    """(state, note, failing fields) for one row of this file."""
    outcome = row["outcome"]
    source = names[current["import_id"]] if current is not None else None
    if outcome == "invalid":
        fields = failure_fields(row["payload"], rules)
        if current is not None and _newer(current, own, import_id):
            return "rejected", f"{row['reason']} · {source['label']} has a newer value in main", fields
        if current is not None and failure_list(current["payload"], rules):
            return "remove", row["reason"], fields
        if current is not None:
            return "rejected", f"{row['reason']} · main keeps the value from {source['label']}", fields
        return "rejected", row["reason"], fields
    if current is None:
        if outcome == "good":
            return "new", "", []
        return "rerun", "Removed from main after this run", []
    if same_business(current["payload"], row["payload"], rules):
        if current["import_id"] == import_id:
            return "saved", "", []
        if current["import_id"] in own:
            return "saved", f"Saved by version {source['version']}", []
        return "in_main", f"Same value already in main, from {source['label']}", []
    if _newer(current, own, import_id):
        return "newer", f"{source['label']} has a newer value", []
    if outcome == "changed":
        diff = field_changes(current["payload"], row["payload"], rules)
        return "update", ", ".join(f"{name} {old or 'blank'} → {new or 'blank'}" for name, old, new in diff), [
            name for name, _o, _n in diff
        ]
    return "rerun", "Main changed after this run", []


def _newer(current: dict, own: set[int], import_id: int) -> bool:
    return current["import_id"] not in own and current["import_id"] > import_id


def _verdict(plan: dict) -> dict:
    """The one-line answer at the top of the import page."""
    counts = plan["state_counts"]
    if plan["archived"]:
        if plan["archived"]["reason"] == "replaced":
            by = plan["archived"]["by"]
            return {
                "tone": "idle",
                "title": f"Replaced by version {plan['archived']['by_version']}" if by else "Replaced",
                "detail": "This is an earlier version of the file. It is kept for reference and can't be changed.",
            }
        if plan["archived"]["reason"] == "rolled_back":
            return {
                "tone": "idle",
                "title": "Rolled back",
                "detail": "This version was undone. The version it replaced is current again.",
            }
        return {
            "tone": "idle",
            "title": "Voided",
            "detail": "Its rows were taken out of main and kept in the archive.",
        }
    if plan["status"] == "missing":
        return {
            "tone": "idle",
            "title": "Not processed yet",
            "detail": "Processing checks every row against the config and against main. Nothing changes in main until you save.",
        }
    if plan["status"] != "completed":
        return {"tone": "err", "title": "This file could not be processed", "detail": plan["reason"]}
    if plan["duplicate_of"] is not None:
        return {
            "tone": "dim",
            "title": f"Same file as {plan['duplicate_label']}",
            "detail": f"Nothing to do here. Work from {plan['duplicate_label']}.",
        }
    if not plan["rules_match"]:
        return {"tone": "warn", "title": "Process again", "detail": "The config was updated after this run."}
    if plan["config"]["state"] in {"affected", "unchecked"}:
        affected = plan["config"]["affected"]
        phrase = kinds_phrase(plan["config"]["counts"])
        old = plan["config_version"]
        detail = (
            f"{phrase[0].upper()}{phrase[1:]}. Process again to use {cfg.CONFIG_VERSION}, or keep the {old} results."
            if phrase
            else f"Not yet tested with {cfg.CONFIG_VERSION}. Preview to see what would change."
        )
        title = (
            f"{cfg.CONFIG_VERSION} would change {affected:,} row{'s' if affected != 1 else ''}"
            if affected
            else "Config changed"
        )
        return {"tone": "warn", "title": title, "detail": detail}
    if counts["rerun"]:
        return {
            "tone": "warn",
            "title": "Process again",
            "detail": f"Main changed after this run. {counts['rerun']:,} row{'s' if counts['rerun'] != 1 else ''} need a fresh check.",
        }
    if plan["pending"]:
        parts = []
        for key, single, many in (
            ("adds", "new row to add", "new rows to add"),
            ("replacements", "row to update", "rows to update"),
            ("set_asides", "row to remove because it fails the config", "rows to remove because they fail the config"),
            ("absent", "row to remove because it's no longer in the file", "rows to remove because they're no longer in the file"),
        ):
            count = len(plan[key])
            if count:
                parts.append(f"{count:,} {single if count == 1 else many}")
        title = parts[0][0].upper() + parts[0][1:] if len(parts) == 1 else f"{plan['pending']:,} changes to save"
        detail = "Check below, then save. Main doesn't change until you do."
        if len(parts) > 1:
            detail = " · ".join(parts) + ". " + detail
        return {"tone": "act", "title": title, "detail": detail}
    left_out = counts["rejected"]
    detail = "Every row that passes the config is in main."
    if plan["dismissed"]:
        count = len(plan["dismissed"])
        detail = f"Nothing left to save. {count:,} proposed change{'s were' if count != 1 else ' was'} dismissed."
    if left_out:
        detail += f" {left_out:,} row{'s' if left_out != 1 else ''} fail{'s' if left_out == 1 else ''} the config and {'was' if left_out == 1 else 'were'} left out."
    return {"tone": "ok", "title": "Up to date", "detail": detail}


def _base(batch: dict, names: dict, cur, import_id: int) -> dict:
    archived = None
    if batch["archived_at"] is not None:
        cur.execute(
            "SELECT MIN(import_id) AS by_id FROM import_batch WHERE replaces_import_id = %s",
            (import_id,),
        )
        found = cur.fetchone()
        archived = {
            "reason": batch["archived_reason"] or "voided",
            "by": int(found["by_id"]) if found and found["by_id"] is not None else None,
            "by_version": names[int(found["by_id"])]["version"] if found and found["by_id"] is not None else None,
            "when": friendly(batch["archived_at"]),
        }
    replaces = None
    if batch["replaces_import_id"] is not None:
        earlier = int(batch["replaces_import_id"])
        cur.execute("SELECT archived_reason FROM import_batch WHERE import_id = %s", (earlier,))
        found = cur.fetchone()
        replaces = {"import_id": earlier, "file_name": names[earlier]["file"], "version": names[earlier]["version"],
                    "label": names[earlier]["label"],
                    "can_roll_back": archived is None and bool(found) and found["archived_reason"] == "replaced"}
    return {
        "import_id": import_id,
        "name": names[import_id],
        "file_name": batch["file_name"] or "",
        "archived": archived,
        "replaces": replaces,
    }


def _version(before_rows: dict | None, latest_rows: dict, rules, replaces: dict | None) -> dict | None:
    if before_rows is None or replaces is None:
        return None
    found = version_changes(list(before_rows.values()), list(latest_rows.values()), rules)
    counts = count_kinds(found)
    return {
        **replaces,
        "rows": found,
        "changed": counts["values"],
        "added": counts["added"],
        "removed": counts["removed"],
        "same": len({row["row_key"] for row in latest_rows.values() if row["row_key"]}) - counts["values"] - counts["added"],
    }


def _history(runs: list[dict], names: dict) -> list[dict]:
    """One line per stored run, newest first."""
    return [dict(_history_entry(run, names), latest=index == 0) for index, run in enumerate(runs)]


def _history_entry(run: dict, names: dict) -> dict:
    summary = _json_dict(run["summary_json"])
    counts = _counts(summary.get("counts"))
    if summary.get("status") == "completed":
        found = " · ".join(
            f"{counts[key]:,} {OUTCOME_WORDS[key][0] if counts[key] == 1 else OUTCOME_WORDS[key][1]}"
            for key in OUTCOME_WORDS
            if counts[key]
        )
    else:
        found = "Could not process"
    changes = summary.get("changes") or {}
    if "replaces" in changes:
        phrase = kinds_phrase(changes.get("counts") or {})
        note = f"Compared with version {names[int(changes['replaces'])]['version']}: {phrase or 'same values'}"
    elif changes.get("rules_changed"):
        note = "Config updated"
    elif changes.get("status_changed"):
        note = "Result changed"
    elif "compared_with" in changes:
        note = kinds_phrase(changes.get("counts") or {}) or "—"
    elif int(run["run_no"] or 0) == 1:
        note = "First run"
    else:
        note = "—"
    return {
        "run_no": int(run["run_no"] or 0),
        "run_id": int(run["run_id"]),
        "when": friendly(run["finished_at"] or run["started_at"]),
        "config_version": run["config_version"],
        "found": found,
        "changes": note,
        "written": _written(summary),
    }


def _written(summary: dict) -> list[dict]:
    """What a run saved to main, one part per kind of write, each row with its detail."""
    manifest = summary.get("manifest")
    if not isinstance(manifest, dict):
        return []
    parts: dict[str, dict] = {}
    for key, label in _MANIFEST:
        entries = manifest.get(key)
        if not isinstance(entries, list) or not entries:
            continue
        part = parts.setdefault(label, {"label": label, "items": []})
        for entry in entries:
            part["items"].append({"row_key": entry.get("row_key") or "", "detail": _entry_detail(key, entry)})
    found = []
    for part in parts.values():
        items = sorted(part["items"], key=lambda item: item["row_key"])
        count = len(items)
        keys = [item["row_key"] for item in items]
        if part["label"] == "Added" or not any(item["detail"] for item in items):
            brief = keys[0] if count == 1 else f"{keys[0]} to {keys[-1]}"
        else:
            shown = [f"{item['row_key']} ({item['detail']})" if item["detail"] else item["row_key"] for item in items[:2]]
            brief = ", ".join(shown) + (f" and {count - 2:,} more" if count > 2 else "")
        found.append(
            {
                "label": part["label"],
                "count": count,
                "rows": f"{count:,} row{'s' if count != 1 else ''}",
                "brief": brief,
                "items": items,
            }
        )
    return found


def _entry_detail(kind: str, entry: dict) -> str:
    if kind in {"replaced", "replace"}:
        return ", ".join(
            f"{field} {before or 'blank'} → {after or 'blank'}" for field, before, after in entry.get("changes") or []
        )
    if kind == "set_aside":
        return entry.get("reason") or "fails the config"
    if kind == "absent":
        return "not in this file"
    return ""


def _columns(rules) -> list[dict]:
    """Business columns shown on row tables, in a geologist's reading order."""
    columns = [
        (rules.hole, "Hole", False),
        (rules.interval_from, "From", True),
        (rules.interval_to, "To", True),
        (rules.sample_type, "Type", False),
    ]
    columns += [(name, name.replace("_", " "), True) for name in rules.analytes]
    return [{"name": name, "label": label, "num": num} for name, label, num in columns]


def _page(rows: list[dict], page: int) -> dict:
    total = len(rows)
    pages = max((total + ROWS_PAGE - 1) // ROWS_PAGE, 1)
    page = min(max(page, 1), pages)
    shown = rows[(page - 1) * ROWS_PAGE : page * ROWS_PAGE]
    return {
        "rows": shown,
        "total": total,
        "page": page,
        "pages": pages,
        "start": 0 if total == 0 else (page - 1) * ROWS_PAGE + 1,
        "end": (page - 1) * ROWS_PAGE + len(shown),
    }


def _dismissals(cur, import_id: int) -> dict[tuple[str, str], dict]:
    cur.execute(
        "SELECT row_key, kind, payload_sha, dismissed_at FROM dismissed_change WHERE import_id = %s",
        (import_id,),
    )
    return {
        (row["row_key"], row["kind"]): {"sha": row["payload_sha"], "when": friendly(row["dismissed_at"])}
        for row in cur.fetchall()
    }


def _dismissed_entry(key: str, kind: str, found: dict, values: dict) -> dict:
    return {"row_key": key, "kind": kind, "what": DISMISS_WORDS[kind], "when": found["when"], "values": values}


def _group_main(cur, group_key: str | None) -> set[str]:
    if not group_key:
        return set()
    cur.execute(
        """
        SELECT m.row_key
        FROM main_assay m
        INNER JOIN import_batch b ON b.import_id = m.import_id
        WHERE b.group_key = %s
        """,
        (group_key,),
    )
    return {row["row_key"] for row in cur.fetchall() if row["row_key"]}


def _file_names(cur) -> dict[int, str]:
    cur.execute(
        """
        SELECT b.import_id, f.file_name
        FROM import_batch b
        LEFT JOIN import_file f ON f.import_id = b.import_id
        """
    )
    return {int(row["import_id"]): row["file_name"] or "" for row in cur.fetchall()}


def _earlier_same_bytes(cur, import_id: int) -> int | None:
    cur.execute(
        """
        SELECT MIN(b.import_id) AS first_id
        FROM import_file mine
        INNER JOIN import_file f ON f.fingerprint = mine.fingerprint
        INNER JOIN import_batch b ON b.import_id = f.import_id
        WHERE mine.import_id = %s
          AND b.import_id < %s
          AND b.status = 'succeed'
          AND EXISTS (SELECT 1 FROM raw_row r WHERE r.import_id = b.import_id)
        """,
        (import_id, import_id),
    )
    row = cur.fetchone()
    return int(row["first_id"]) if row and row["first_id"] is not None else None


def _add_groups(adds: list[dict], rules) -> list[dict]:
    """New rows summarised by hole. Standards and blanks group by sample type."""
    groups: dict[str, dict] = {}
    for row in adds:
        values = row["values"]
        sample_type = values.get(rules.sample_type, "").upper()
        hole = values.get(rules.hole, "")
        label = hole or f"{sample_type or 'No type'} (no hole)"
        group = groups.setdefault(
            label,
            {"label": label, "has_hole": bool(hole), "keys": [], "types": set(), "start": None, "end": None},
        )
        group["keys"].append(row["row_key"])
        if sample_type:
            group["types"].add(sample_type)
        start = parse_decimal(values.get(rules.interval_from, ""))
        end = parse_decimal(values.get(rules.interval_to, ""))
        if start is not None and (group["start"] is None or start < group["start"]):
            group["start"] = start
        if end is not None and (group["end"] is None or end > group["end"]):
            group["end"] = end
    ordered = sorted(groups.values(), key=lambda item: (not item["has_hole"], item["label"]))
    return [
        {
            "label": group["label"],
            "has_hole": group["has_hole"],
            "count": len(group["keys"]),
            "first": min(group["keys"]),
            "last": max(group["keys"]),
            "types": ", ".join(sorted(group["types"])),
            "start": "" if group["start"] is None else str(group["start"]),
            "end": "" if group["end"] is None else str(group["end"]),
        }
        for group in ordered
    ]


def _run_no(run: dict | None) -> int | None:
    if run is None or run.get("run_id") is None:
        return None
    return int(run["run_no"]) if run.get("run_no") is not None else None


def _empty(base: dict, status: str, reason: str) -> dict:
    empty = key_digest([])
    plan = {
        **base,
        "is_preview": False,
        "status": status,
        "reason": reason,
        "run_id": None,
        "run_no": None,
        "run_count": 0,
        "run_when": "",
        "config_version": "",
        "config": {"state": "current", "affected": 0, "counts": {}, "run_config": ""},
        "config_match": False,
        "rules_match": False,
        "counts": _counts(None),
        "rows": [],
        "states": [],
        "state_counts": {code: 0 for code, _l, _t in STATES},
        "row_total": 0,
        "adds": [],
        "add_groups": [],
        "replacements": [],
        "set_asides": [],
        "absent": [],
        "conflicts": [],
        "dismissed": [],
        "pending": 0,
        "add_digest": empty,
        "replace_digest": empty,
        "set_aside_digest": empty,
        "absent_digest": empty,
        "can_write": False,
        "duplicate_of": None,
        "duplicate_note": "",
        "history": [],
        "preview_changes": None,
        "version": None,
        "columns": [],
    }
    plan["verdict"] = _verdict(plan)
    return plan


def _counts(value: object) -> dict[str, int]:
    counts = {"good": 0, "already_same": 0, "changed": 0, "invalid": 0}
    if isinstance(value, dict):
        for name in counts:
            counts[name] = int(value.get(name) or 0)
    return counts


def _show(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}
