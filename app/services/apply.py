"""Confirmed writes. The digest must match the plan rebuilt under row locks."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pymysql
from pymysql.cursors import DictCursor

from app.db import get_conn
from app.services.plan import build_plan
from app.services.rules import RULES_ID, business_values, current_rules, field_changes, invalid_reason, payload_digest

_DIGESTS = {
    "adds": "add_digest",
    "replace": "replace_digest",
    "set_aside": "set_aside_digest",
    "absent": "absent_digest",
}
_REASONS = {
    "replace": "replace changed row",
    "set_aside": "set aside invalid live row",
    "absent": "absent from replacement file",
}


@dataclass(frozen=True)
class ApplyResult:
    import_id: int
    run_id: int
    action: str
    count: int


def apply_confirmed(import_id: int, action: str, run_id: int, digest: str) -> ApplyResult:
    if action not in _DIGESTS:
        raise ValueError("Unknown write.")
    with get_conn() as conn:
        try:
            result = _apply(conn, import_id, action, run_id, digest)
            conn.commit()
            return result
        except pymysql.err.IntegrityError as exc:
            conn.rollback()
            raise ValueError(
                "The write stopped because a row key conflicted. Main was not changed."
            ) from exc
        except Exception:
            conn.rollback()
            raise


def _apply(conn, import_id: int, action: str, run_id: int, digest: str) -> ApplyResult:
    rules = current_rules()
    with conn.cursor(DictCursor) as cur:
        cur.execute(
            "SELECT import_id FROM import_batch WHERE import_id = %s FOR UPDATE",
            (import_id,),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Import {import_id} was not found.")
        cur.execute("SELECT row_key FROM main_assay FOR UPDATE")
        cur.fetchall()
        plan = build_plan(conn, import_id)
        _require(plan, run_id, action, digest)
        manifest_key = {
            "adds": "inserted",
            "replace": "replaced",
            "set_aside": "set_aside",
            "absent": "absent",
        }[action]
        if action == "adds":
            entries = _insert_adds(cur, import_id, plan, rules)
        else:
            entries = _archive(cur, import_id, plan, action, rules)
        _merge_manifest(cur, plan["run_id"], manifest_key, entries)
    return ApplyResult(import_id, plan["run_id"], action, len(entries))


def _require(plan: dict, run_id: int, action: str, digest: str) -> None:
    if plan["run_id"] != run_id:
        raise ValueError(
            "A newer run exists. Review it before writing. Main was not changed."
        )
    if plan["archived"]:
        raise ValueError("This import was replaced or voided. Main was not changed.")
    if plan["status"] != "completed":
        raise ValueError("Processing did not complete. Main was not changed.")
    if not plan["rules_match"]:
        raise ValueError(
            "Reprocess this import before writing. Main was not changed."
        )
    if not plan["config_match"]:
        raise ValueError(
            "The loaded config changes this import's results. Process it again, "
            "or keep its current results first. Main was not changed."
        )
    if plan["duplicate_of"] is not None:
        raise ValueError(f"{plan['duplicate_note']} Main was not changed.")
    if digest != plan[_DIGESTS[action]]:
        raise ValueError(
            "These keys changed since the list was shown. "
            "Main was not changed. Review the import again."
        )
    keys = {
        "adds": plan["adds"],
        "replace": plan["replacements"],
        "set_aside": plan["set_asides"],
        "absent": plan["absent"],
    }[action]
    if not keys:
        raise ValueError("There is nothing to write. Main was not changed.")


def _insert_adds(cur, import_id: int, plan: dict, rules) -> list[dict]:
    payloads = _payloads(cur, plan["run_id"])
    entries = []
    rows = []
    for item in plan["adds"]:
        key = item["row_key"]
        stored = _ready_payload(payloads, key, rules)
        rows.append((key, import_id, json.dumps(stored, ensure_ascii=False)))
        entries.append({"row_key": key, "payload_sha": _sha(stored)})
    cur.executemany(
        """
        INSERT INTO main_assay (row_key, import_id, payload_json)
        VALUES (%s, %s, %s)
        """,
        rows,
    )
    return entries


def _archive(cur, import_id: int, plan: dict, action: str, rules) -> list[dict]:
    selected = {
        "replace": plan["replacements"],
        "set_aside": plan["set_asides"],
        "absent": plan["absent"],
    }[action]
    keys = [item["row_key"] for item in selected]
    placeholders = ", ".join(["%s"] * len(keys))
    cur.execute(
        f"""
        SELECT row_key, import_id, payload_json
        FROM main_assay
        WHERE row_key IN ({placeholders})
        FOR UPDATE
        """,
        keys,
    )
    main_rows = {row["row_key"]: row for row in cur.fetchall()}
    if set(main_rows) != set(keys):
        raise ValueError(
            "These keys changed since the list was shown. "
            "Main was not changed. Review the import again."
        )
    payloads = _payloads(cur, plan["run_id"]) if action == "replace" else {}
    reason = _REASONS[action]
    entries = []
    for item in selected:
        key = item["row_key"]
        current = main_rows[key]
        current_payload = _json_dict(current["payload_json"])
        stored_current = current["payload_json"]
        if not isinstance(stored_current, str):
            stored_current = json.dumps(current_payload, ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO archive_assay (
              row_key, from_import_id, payload_json, reason,
              run_id, replaced_by_import_id, payload_sha
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                key,
                int(current["import_id"]),
                stored_current,
                reason,
                plan["run_id"],
                import_id,
                _sha(current_payload),
            ),
        )
        cur.execute("DELETE FROM main_assay WHERE row_key = %s", (key,))
        written = current_payload
        if action == "replace":
            written = _ready_payload(payloads, key, rules)
            cur.execute(
                """
                INSERT INTO main_assay (row_key, import_id, payload_json)
                VALUES (%s, %s, %s)
                """,
                (key, import_id, json.dumps(written, ensure_ascii=False)),
            )
        entry = {"row_key": key, "payload_sha": _sha(written)}
        if action == "replace":
            entry["changes"] = [list(change) for change in field_changes(current_payload, written, rules)]
        elif action == "set_aside":
            entry["reason"] = item.get("reason") or ""
        entries.append(entry)
    return entries


def _ready_payload(payloads: dict[str, dict], key: str, rules) -> dict:
    if key not in payloads:
        raise ValueError(
            f"{key} is not on this run. Main was not changed."
        )
    stored, _ = business_values(payloads[key], rules)
    reason = invalid_reason(stored, rules)
    if reason:
        raise ValueError(
            f"{key} no longer passes the loaded rules ({reason}). Main was not changed."
        )
    return stored


def _payloads(cur, run_id: int) -> dict[str, dict]:
    cur.execute(
        """
        SELECT row_key, payload_json
        FROM processing_row
        WHERE run_id = %s AND row_key IS NOT NULL AND row_key <> ''
        """,
        (run_id,),
    )
    found: dict[str, dict] = {}
    for row in cur.fetchall():
        found[row["row_key"]] = _json_dict(row["payload_json"])
    return found


def record_manifest(cur, run_id: int, action: str, entries: list[dict]) -> None:
    _merge_manifest(cur, run_id, action, entries)


def payload_sha(payload: dict) -> str:
    return _sha(payload)


def _merge_manifest(cur, run_id: int, action: str, entries: list[dict]) -> None:
    cur.execute(
        "SELECT summary_json FROM processing_run WHERE run_id = %s FOR UPDATE",
        (run_id,),
    )
    row = cur.fetchone()
    summary = _json_dict(row["summary_json"] if row else None)
    manifest = summary.get("manifest")
    if not isinstance(manifest, dict):
        manifest = {}
    manifest[action] = entries
    summary["manifest"] = manifest
    summary["rules"] = summary.get("rules") or RULES_ID
    cur.execute(
        "UPDATE processing_run SET summary_json = %s WHERE run_id = %s",
        (json.dumps(summary, ensure_ascii=False), run_id),
    )


def _sha(payload: dict) -> str:
    return payload_digest(payload)


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}
