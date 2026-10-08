"""SQLite 存储:查询/条件版本、搜索任务、报告版本、沟通笔记(规格 6/9)。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "configs" / ".." / "data" / "resumetrace.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches(
  search_id TEXT PRIMARY KEY, dataset_id TEXT, query_text TEXT, query_sha TEXT,
  input_type TEXT, detected_type TEXT, source_url TEXT, created_at REAL,
  criteria_version INT, parent_search_id TEXT, status TEXT, scope_constraints INT);
CREATE TABLE IF NOT EXISTS criteria_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, search_id TEXT, version INT,
  criteria_json TEXT, warnings TEXT);
CREATE TABLE IF NOT EXISTS reports(
  report_id TEXT PRIMARY KEY, search_id TEXT, candidate_id TEXT, version INT,
  report_json TEXT, created_at REAL, source_deleted INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS notes(
  note_id TEXT PRIMARY KEY, candidate_id TEXT, criterion_id TEXT, source_type TEXT,
  author TEXT, recorded_at REAL, text TEXT, report_version INT);
CREATE TABLE IF NOT EXISTS resume_versions(
  candidate_id TEXT PRIMARY KEY, dataset_id TEXT, document_id TEXT,
  source_sha256 TEXT, filename TEXT, ingested_at TEXT, parser_version TEXT, status TEXT);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = getattr(_local, "conn", None)
    if c is not None:
        return c
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    _local.conn = c
    return c


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def create_search(dataset_id, query_text, query_sha, detected_type, source_url=None,
                  scope_constraints=True, parent_search_id=None) -> str:
    sid = new_id("srch")
    with conn() as c:
        c.execute("INSERT INTO searches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, dataset_id, query_text, query_sha, "auto", detected_type,
                   source_url, time.time(), 1, parent_search_id, "running",
                   int(scope_constraints)))
    return sid


def save_criteria(sid, version, criteria, warnings):
    with conn() as c:
        c.execute("INSERT INTO criteria_versions(search_id, version, criteria_json, warnings)"
                  " VALUES (?,?,?,?)",
                  (sid, version, json.dumps(criteria, ensure_ascii=False),
                   json.dumps(warnings, ensure_ascii=False)))
        c.execute("UPDATE searches SET criteria_version=? WHERE search_id=?", (version, sid))


def save_report(sid, candidate_id, report: dict) -> str:
    rid = new_id("rpt")
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(version),0)+1 v FROM reports WHERE search_id=? AND candidate_id=?",
                        (sid, candidate_id)).fetchone()
        c.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,0)",
                  (rid, sid, candidate_id, row["v"], json.dumps(report, ensure_ascii=False),
                   time.time()))
    return rid


def get_report(rid) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM reports WHERE report_id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_reports(sid) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM reports WHERE search_id=? ORDER BY candidate_id, version", (sid,))]


def add_note(candidate_id, text, source_type, author="local", criterion_id=None) -> tuple[str, int]:
    nid = new_id("note")
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(report_version),0)+1 v FROM notes WHERE candidate_id=?",
                        (candidate_id,)).fetchone()
        c.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)",
                  (nid, candidate_id, criterion_id, source_type, author, time.time(),
                   text, row["v"]))
    return nid, row["v"]


def list_notes(candidate_id) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM notes WHERE candidate_id=? ORDER BY recorded_at", (candidate_id,))]


def finish_search(sid, status="done"):
    with conn() as c:
        c.execute("UPDATE searches SET status=? WHERE search_id=?", (status, sid))


def get_search(sid) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM searches WHERE search_id=?", (sid,)).fetchone()
    return dict(row) if row else None
