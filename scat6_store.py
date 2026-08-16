# scat6_store.py — SQLite persistence for SCAT6 assessments (same pattern as
# the original comments.db: a small local DB next to the app).
from __future__ import annotations
import os, json, sqlite3
from typing import Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "scat6.db")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scat6_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_id INTEGER NOT NULL,
            athlete_name TEXT,
            date_of_examination TEXT,
            assessment_type TEXT,
            examiner TEXT,
            symptom_number INTEGER,
            symptom_severity INTEGER,
            cognitive_total INTEGER,
            mbess_total INTEGER,
            concussion_diagnosed TEXT,
            payload_json TEXT NOT NULL,
            scores_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def save_assessment(assessment: Dict, scores: Dict) -> int:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO scat6_assessments
           (athlete_id, athlete_name, date_of_examination, assessment_type, examiner,
            symptom_number, symptom_severity, cognitive_total, mbess_total,
            concussion_diagnosed, payload_json, scores_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(assessment.get("athlete_id") or 0),
            assessment.get("athlete_name") or "",
            assessment.get("date_of_examination") or "",
            assessment.get("assessment_type") or "",
            assessment.get("examiner") or "",
            scores.get("symptom_number"),
            scores.get("symptom_severity"),
            scores.get("cognitive_total"),
            scores.get("mbess_total"),
            assessment.get("concussion_diagnosed") or "",
            json.dumps(assessment, default=str),
            json.dumps(scores, default=str),
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(new_id)


def list_assessments(athlete_id: Optional[int] = None) -> List[Dict]:
    conn = _db()
    cur = conn.cursor()
    q = """SELECT id, athlete_id, athlete_name, date_of_examination, assessment_type,
                  examiner, symptom_number, symptom_severity, cognitive_total,
                  mbess_total, concussion_diagnosed, created_at
           FROM scat6_assessments"""
    args: tuple = ()
    if athlete_id is not None:
        q += " WHERE athlete_id = ?"
        args = (int(athlete_id),)
    q += " ORDER BY date_of_examination ASC, id ASC"
    cur.execute(q, args)
    rows = cur.fetchall()
    conn.close()
    cols = ["id", "athlete_id", "athlete_name", "date_of_examination", "assessment_type",
            "examiner", "symptom_number", "symptom_severity", "cognitive_total",
            "mbess_total", "concussion_diagnosed", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def get_assessment(assessment_id: int) -> Optional[Dict]:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT payload_json, scores_json FROM scat6_assessments WHERE id = ?",
                (int(assessment_id),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    payload = json.loads(row[0])
    scores = json.loads(row[1])
    payload["assessment_id"] = int(assessment_id)
    return {"assessment": payload, "scores": scores}


def delete_assessment(assessment_id: int) -> None:
    conn = _db()
    conn.execute("DELETE FROM scat6_assessments WHERE id = ?", (int(assessment_id),))
    conn.commit()
    conn.close()
