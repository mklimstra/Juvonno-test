# scat6.py — SCAT6 (Sport Concussion Assessment Tool 6) form definition,
# scoring, and flat-record serialization.
#
# Source: Echemendia RJ, et al. Br J Sports Med 2023;57:622-631 (SCAT6,
# for adolescents 13+ and adults). This module encodes the form content;
# the SCAT6 must be administered by a Health Care Professional.
from __future__ import annotations
from typing import Dict, List, Optional

# ────────── Step: Red Flags (Box 1) ──────────
RED_FLAGS = [
    "Neck pain or tenderness",
    "Seizure or convulsion",
    "Double vision",
    "Loss of consciousness",
    "Weakness or tingling/burning in more than 1 arm or in the legs",
    "Deteriorating conscious state",
    "Vomiting",
    "Severe or increasing headache",
    "Increasingly restless, agitated or combative",
    "GCS < 15",
    "Visible deformity of the skull",
]

# ────────── Step 1: Observable Signs ──────────
OBSERVABLE_SIGNS = [
    "Lying motionless on playing surface",
    "Falling unprotected to the surface",
    "Balance/gait difficulties, motor incoordination, ataxia: stumbling, slow/laboured movements",
    "Disorientation or confusion, staring or limited responsiveness, or an inability to respond appropriately to questions",
    "Blank or vacant look",
    "Facial injury after head trauma",
    "Impact seizure",
    "High-risk mechanism of injury (sport-dependent)",
]

# ────────── Step 2: Glasgow Coma Scale ──────────
GCS_EYE = [
    ("No eye opening", 1), ("Eye opening to pain", 2),
    ("Eye opening to speech", 3), ("Eyes opening spontaneously", 4),
]
GCS_VERBAL = [
    ("No verbal response", 1), ("Incomprehensible sounds", 2),
    ("Inappropriate words", 3), ("Confused", 4), ("Oriented", 5),
]
GCS_MOTOR = [
    ("No motor response", 1), ("Extension to pain", 2), ("Abnormal flexion to pain", 3),
    ("Flexion/withdrawal to pain", 4), ("Localized to pain", 5), ("Obeys commands", 6),
]

# ────────── Step 3: Cervical Spine ──────────
CERVICAL_ITEMS = [
    "Does the athlete report neck pain at rest?",
    "Is there tenderness to palpation?",
    "If NO neck pain and NO tenderness, does the athlete have a full range of ACTIVE pain-free movement?",
    "Are limb strength and sensation normal?",
]

# ────────── Step 4 (immediate): Coordination & Ocular/Motor ──────────
COORD_OCULAR_ITEMS = [
    "Coordination: Is finger-to-nose normal for both hands with eyes open and closed?",
    "Ocular/Motor: Without moving their head or neck, can the patient look side-to-side and up-and-down without double vision?",
    "Are observed extraocular eye movements normal?",
]

# ────────── Step 5 (immediate): Maddocks Questions ──────────
MADDOCKS_QUESTIONS = [
    "What venue are we at today?",
    "Which half is it now?",
    "Who scored last in this match?",
    "What team did you play last week/game?",
    "Did your team win the last game?",
]

# ────────── Off-field Step 1: Athlete Background ──────────
BACKGROUND_ITEMS = [
    ("hosp_head_injury", "Hospitalised for head injury?"),
    ("headache_disorder", "Diagnosed/treated for headache disorder or migraine?"),
    ("learning_disability", "Diagnosed with a learning disability/dyslexia?"),
    ("adhd", "Diagnosed with attention deficit hyperactivity disorder (ADHD)?"),
    ("psych_disorder", "Diagnosed with depression, anxiety, or other psychological disorder?"),
]

# ────────── Off-field Step 2: Symptom Evaluation (22 items, 0–6) ──────────
SYMPTOMS = [
    "Headaches", "Pressure in head", "Neck pain", "Nausea or vomiting", "Dizziness",
    "Blurred vision", "Balance problems", "Sensitivity to light", "Sensitivity to noise",
    "Feeling slowed down", 'Feeling like "in a fog"', "\"Don't feel right\"",
    "Difficulty concentrating", "Difficulty remembering", "Fatigue or low energy",
    "Confusion", "Drowsiness", "More emotional", "Irritability", "Sadness",
    "Nervous or anxious", "Trouble falling asleep (if applicable)",
]
SYMPTOM_MAX_SEVERITY = 6
SYMPTOM_COUNT = len(SYMPTOMS)              # 22
SYMPTOM_SEVERITY_MAX = SYMPTOM_COUNT * SYMPTOM_MAX_SEVERITY  # 132

# ────────── Off-field Step 3: Cognitive screening ──────────
ORIENTATION_QUESTIONS = [
    "What month is it?",
    "What is the date today?",
    "What is the day of the week?",
    "What year is it?",
    "What time is it right now? (within 1 hour)",
]

WORD_LISTS = {
    "A": ["Jacket", "Arrow", "Pepper", "Cotton", "Movie",
          "Dollar", "Honey", "Mirror", "Saddle", "Anchor"],
    "B": ["Finger", "Penny", "Blanket", "Lemon", "Insect",
          "Candle", "Paper", "Sugar", "Sandwich", "Wagon"],
    "C": ["Baby", "Monkey", "Perfume", "Sunset", "Iron",
          "Elbow", "Apple", "Carpet", "Saddle", "Bubble"],
}

DIGIT_LISTS = {
    "A": [("4-9-3", "6-2-9"), ("3-8-1-4", "3-2-7-9"),
          ("6-2-9-7-1", "1-5-2-8-6"), ("7-1-8-4-6-2", "5-3-9-1-4-8")],
    "B": [("5-2-6", "4-1-5"), ("1-7-9-5", "4-9-6-8"),
          ("4-8-5-2-7", "6-1-8-4-3"), ("8-3-1-9-6-4", "7-2-4-8-5-6")],
    "C": [("1-4-2", "6-5-8"), ("6-8-3-1", "3-4-8-1"),
          ("4-9-1-5-3", "6-8-2-5-1"), ("3-7-6-5-1-9", "9-2-6-5-1-4")],
}

MONTHS_REVERSED = ["December", "November", "October", "September", "August", "July",
                   "June", "May", "April", "March", "February", "January"]

# ────────── mBESS ──────────
MBESS_STANCES = [("double", "Double Leg Stance"), ("tandem", "Tandem Stance"),
                 ("single", "Single Leg Stance")]
MBESS_MAX_PER_STANCE = 10

# ────────── Symptom-scale sub-questions ──────────
PERCENT_NORMAL_QUESTION = "If 100% is feeling perfectly normal, what percent of normal do you feel?"


# ══════════ Scoring helpers ══════════
def _int0(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def score_symptoms(ratings: List[Optional[int]]) -> Dict[str, int]:
    vals = [_int0(v) for v in (ratings or [])]
    return {
        "symptom_number": sum(1 for v in vals if v > 0),
        "symptom_severity": sum(vals),
    }


def score_gcs(e, v, m) -> Optional[int]:
    if e is None or v is None or m is None:
        return None
    return _int0(e) + _int0(v) + _int0(m)


def score_maddocks(answers: List[Optional[int]]) -> int:
    return sum(_int0(a) for a in (answers or []))


def score_orientation(answers: List[Optional[int]]) -> int:
    return sum(_int0(a) for a in (answers or []))


def score_immediate_memory(trial_hits: List[List]) -> Dict[str, int]:
    """trial_hits: 3 lists of 0/1 per word (10 words)."""
    trials = [sum(_int0(x) for x in (t or [])) for t in (trial_hits or [[], [], []])]
    while len(trials) < 3:
        trials.append(0)
    return {"im_trial1": trials[0], "im_trial2": trials[1], "im_trial3": trials[2],
            "immediate_memory": sum(trials[:3])}


def score_digits(row_scores: List[Optional[int]]) -> int:
    """4 rows, 1 point each."""
    return sum(_int0(x) for x in (row_scores or []))


def score_months(errors: Optional[int], seconds: Optional[float]) -> int:
    """1 point if 0 errors and completed in under 30 s."""
    if errors is None or seconds is None:
        return 0
    try:
        return 1 if (int(errors) == 0 and float(seconds) < 30.0) else 0
    except (TypeError, ValueError):
        return 0


def score_concentration(digit_rows: List[Optional[int]], months_errors, months_seconds) -> Dict[str, int]:
    digits = score_digits(digit_rows)
    months = score_months(months_errors, months_seconds)
    return {"digits_score": digits, "months_score": months, "concentration": digits + months}


def score_mbess(double_e, tandem_e, single_e) -> Dict[str, Optional[int]]:
    vals = [double_e, tandem_e, single_e]
    if all(v is None for v in vals):
        return {"mbess_double": None, "mbess_tandem": None, "mbess_single": None, "mbess_total": None}
    clamp = lambda v: max(0, min(MBESS_MAX_PER_STANCE, _int0(v)))
    d, t, s = clamp(double_e), clamp(tandem_e), clamp(single_e)
    return {"mbess_double": d, "mbess_tandem": t, "mbess_single": s, "mbess_total": d + t + s}


def score_delayed_recall(hits: List) -> int:
    return sum(_int0(x) for x in (hits or []))


def tandem_gait_summary(times: List[Optional[float]]) -> Dict[str, Optional[float]]:
    vals = []
    for t in (times or []):
        try:
            if t is not None and float(t) > 0:
                vals.append(float(t))
        except (TypeError, ValueError):
            continue
    if not vals:
        return {"tg_average": None, "tg_fastest": None}
    return {"tg_average": round(sum(vals) / len(vals), 2), "tg_fastest": min(vals)}


def compute_all_scores(a: Dict) -> Dict:
    """Compute every derived score from a raw assessment dict (see app.py for
    the collection format). Returns a flat dict of scores."""
    out: Dict = {}
    out.update(score_symptoms(a.get("symptom_ratings", [])))
    out["gcs_total"] = score_gcs(a.get("gcs_e"), a.get("gcs_v"), a.get("gcs_m"))
    out["maddocks"] = score_maddocks(a.get("maddocks_answers", []))
    out["orientation"] = score_orientation(a.get("orientation_answers", []))
    out.update(score_immediate_memory(a.get("im_trials", [[], [], []])))
    out.update(score_concentration(a.get("digit_rows", []),
                                   a.get("months_errors"), a.get("months_seconds")))
    out.update(score_mbess(a.get("mbess_double"), a.get("mbess_tandem"), a.get("mbess_single")))
    if any(a.get(k) is not None for k in ("mbess_foam_double", "mbess_foam_tandem", "mbess_foam_single")):
        foam = score_mbess(a.get("mbess_foam_double"), a.get("mbess_foam_tandem"), a.get("mbess_foam_single"))
        out["mbess_foam_total"] = foam["mbess_total"]
    else:
        out["mbess_foam_total"] = None
    out["delayed_recall"] = score_delayed_recall(a.get("dr_hits", []))
    out["cognitive_total"] = (out["orientation"] + out["immediate_memory"]
                              + out["concentration"] + out["delayed_recall"])
    out.update(tandem_gait_summary(a.get("tg_times", [])))
    dual = a.get("dual_task_times", [])
    try:
        dual_vals = [float(t) for t in dual if t is not None and float(t) > 0]
    except (TypeError, ValueError):
        dual_vals = []
    out["dual_task_fastest"] = min(dual_vals) if dual_vals else None
    return out


# ══════════ Flat record for CSV / history ══════════
CSV_COLUMNS = [
    "assessment_id", "date_of_examination", "examiner", "athlete_id", "athlete_name",
    "dob", "sex", "dominant_hand", "sport_team", "date_of_injury", "time_of_injury",
    "assessment_type",  # baseline | post_injury
    "num_past_concussions", "most_recent_concussion", "recovery_days",
    "red_flags", "observable_signs_count", "gcs_total", "cervical_normal",
    "coord_ocular_normal", "maddocks",
    "symptom_number", "symptom_severity", "worse_physical", "worse_mental",
    "percent_normal",
    "orientation", "im_trial1", "im_trial2", "im_trial3", "immediate_memory",
    "word_list", "digit_list", "digits_score", "months_seconds", "months_errors",
    "months_score", "concentration",
    "mbess_double", "mbess_tandem", "mbess_single", "mbess_total", "mbess_foam_total",
    "tg_trial1", "tg_trial2", "tg_trial3", "tg_average", "tg_fastest",
    "dual_task_fastest", "delayed_recall", "cognitive_total",
    "neuro_exam", "different_from_usual", "concussion_diagnosed", "notes",
]


def to_flat_record(assessment: Dict, scores: Dict) -> Dict:
    """Flatten the raw assessment + computed scores into one CSV-ready row."""
    a, s = assessment, scores
    tg = a.get("tg_times", []) or [None, None, None]
    while len(tg) < 3:
        tg.append(None)
    red = [RED_FLAGS[i] for i, v in enumerate(a.get("red_flags", [])) if v] \
        if isinstance(a.get("red_flags"), list) else (a.get("red_flags") or [])
    obs = a.get("observable_signs", [])
    obs_count = sum(1 for v in obs if v == "Y") if isinstance(obs, list) else ""
    return {
        "assessment_id": a.get("assessment_id", ""),
        "date_of_examination": a.get("date_of_examination", ""),
        "examiner": a.get("examiner", ""),
        "athlete_id": a.get("athlete_id", ""),
        "athlete_name": a.get("athlete_name", ""),
        "dob": a.get("dob", ""),
        "sex": a.get("sex", ""),
        "dominant_hand": a.get("dominant_hand", ""),
        "sport_team": a.get("sport_team", ""),
        "date_of_injury": a.get("date_of_injury", ""),
        "time_of_injury": a.get("time_of_injury", ""),
        "assessment_type": a.get("assessment_type", ""),
        "num_past_concussions": a.get("num_past_concussions", ""),
        "most_recent_concussion": a.get("most_recent_concussion", ""),
        "recovery_days": a.get("recovery_days", ""),
        "red_flags": "; ".join(red) if isinstance(red, list) else str(red),
        "observable_signs_count": obs_count,
        "gcs_total": s.get("gcs_total", ""),
        "cervical_normal": a.get("cervical_normal", ""),
        "coord_ocular_normal": a.get("coord_ocular_normal", ""),
        "maddocks": s.get("maddocks", ""),
        "symptom_number": s.get("symptom_number", ""),
        "symptom_severity": s.get("symptom_severity", ""),
        "worse_physical": a.get("worse_physical", ""),
        "worse_mental": a.get("worse_mental", ""),
        "percent_normal": a.get("percent_normal", ""),
        "orientation": s.get("orientation", ""),
        "im_trial1": s.get("im_trial1", ""),
        "im_trial2": s.get("im_trial2", ""),
        "im_trial3": s.get("im_trial3", ""),
        "immediate_memory": s.get("immediate_memory", ""),
        "word_list": a.get("word_list", ""),
        "digit_list": a.get("digit_list", ""),
        "digits_score": s.get("digits_score", ""),
        "months_seconds": a.get("months_seconds", ""),
        "months_errors": a.get("months_errors", ""),
        "months_score": s.get("months_score", ""),
        "concentration": s.get("concentration", ""),
        "mbess_double": s.get("mbess_double", ""),
        "mbess_tandem": s.get("mbess_tandem", ""),
        "mbess_single": s.get("mbess_single", ""),
        "mbess_total": s.get("mbess_total", ""),
        "mbess_foam_total": s.get("mbess_foam_total", ""),
        "tg_trial1": tg[0] if tg[0] is not None else "",
        "tg_trial2": tg[1] if tg[1] is not None else "",
        "tg_trial3": tg[2] if tg[2] is not None else "",
        "tg_average": s.get("tg_average", ""),
        "tg_fastest": s.get("tg_fastest", ""),
        "dual_task_fastest": s.get("dual_task_fastest", ""),
        "delayed_recall": s.get("delayed_recall", ""),
        "cognitive_total": s.get("cognitive_total", ""),
        "neuro_exam": a.get("neuro_exam", ""),
        "different_from_usual": a.get("different_from_usual", ""),
        "concussion_diagnosed": a.get("concussion_diagnosed", ""),
        "notes": (a.get("notes", "") or "").replace("\n", " ").strip(),
    }


# Domain rows for the serial-comparison table (Step 6: Decision)
DECISION_DOMAINS = [
    ("Neurological exam", "neuro_exam", None),
    ("Symptom number", "symptom_number", 22),
    ("Symptom severity", "symptom_severity", 132),
    ("Orientation", "orientation", 5),
    ("Immediate memory", "immediate_memory", 30),
    ("Concentration", "concentration", 5),
    ("Delayed recall", "delayed_recall", 10),
    ("Cognitive total", "cognitive_total", 50),
    ("mBESS total errors", "mbess_total", 30),
    ("Tandem gait fastest (s)", "tg_fastest", None),
    ("Dual task fastest (s)", "dual_task_fastest", None),
]
