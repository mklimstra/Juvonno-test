# scat6_encounter.py — detect SCAT6 intakes/charts filled out inside Juvonno
# and convert their form fields into the app's assessment + scores format.
#
# Tuned against the real CSI Pacific template ("SCAT6 - Baseline + KD",
# template id 78). Key properties of that template this parser handles:
#   • duplicate field labels — mBESS firm & foam blocks reuse the exact same
#     labels ("Double Leg Stance (of 10)" …), and "Trial 1/2/3" appears for
#     immediate memory (word strings), tandem gait (times), and delayed recall
#     (word strings) — so fields are processed as an ORDERED list, never a dict
#   • clinician-recorded scores ("Orientation Score (of 5)" …) take precedence
#     over recomputed values; computation fills whatever isn't recorded
#   • data-entry quirks: "Single Less Stance" typo, non-numeric entries like
#     "o" or "not tested", labels with embedded newlines/trailing spaces
#   • the King-Devick add-on (card times + total) is captured as extra data
from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple

import scat6 as S


# ────────── helpers ──────────
def _norm(s) -> str:
    s = str(s or "").lower().replace("\n", " ")
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("'", "")
    return re.sub(r"[^a-z0-9% ]+", " ", s).strip()


def _num(v) -> Optional[float]:
    """First number in a value ('3', '13', '0 hard 2 foam', '12.5 s' …).

    Guard: free-text notes sometimes land in score fields (e.g. "This was
    accidentally saved, it is not an official SCAT6" — whose 'SCAT6' must not
    score as 6). A number only counts if it starts within the first characters
    of the value, which holds for every legitimate entry style seen."""
    s = str(v or "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    if m.start() > 8:
        return None
    return float(m.group(0))


def _int(v) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _yn(v) -> str:
    t = str(v or "").strip().lower()
    if t.startswith(("y", "1", "true", "checked")):
        return "Y"
    if t.startswith(("n", "0", "false")):
        return "N"
    return ""


def _is_numeric(v) -> bool:
    return re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", str(v or "")) is not None


def _collect_fields(enc: Dict) -> Tuple[List[str], List[Tuple[str, str]]]:
    """(template_names, ordered [(normalized_label, value), …])."""
    names: List[str] = []
    pairs: List[Tuple[str, str]] = []
    for block in (enc.get("data") or []):
        if not isinstance(block, dict):
            continue
        tpl = block.get("template")
        if isinstance(tpl, dict):
            for key in ("tab_name", "name", "title"):
                val = tpl.get(key)
                if isinstance(val, str) and val.strip():
                    names.append(val.strip())
        for f in (block.get("fields") or []):
            if not isinstance(f, dict):
                continue
            label = f.get("name") or f.get("label") or f.get("title") or ""
            value = f.get("value")
            if value is None:
                value = f.get("answer") or f.get("text") or ""
            label_n = _norm(label)
            value_s = str(value).strip()
            # skip fields whose "name" is just the internal id echoed back
            if label_n and value_s and not value_s.startswith("---") \
                    and str(label).strip() != str(f.get("id") or ""):
                pairs.append((label_n, value_s))
    return names, pairs


def is_scat6_encounter(enc: Dict) -> bool:
    names, pairs = _collect_fields(enc)
    hay = " ".join(names).lower() + " " + " ".join(l for l, _ in pairs)
    return bool(re.search(r"scat[\s\-]?6|scat6", hay))


def _all(pairs, *needles, exclude=()) -> List[str]:
    """Values of every field (in order) whose label contains all needles."""
    out = []
    for label, value in pairs:
        if all(n in label for n in needles) and not any(x in label for x in exclude):
            out.append(value)
    return out


def _first(pairs, *needles, exclude=()) -> Optional[str]:
    vals = _all(pairs, *needles, exclude=exclude)
    return vals[0] if vals else None


def _exact_all(pairs, label_norm) -> List[str]:
    return [v for l, v in pairs if l == label_norm]


def _enc_date(enc: Dict) -> str:
    for key in ("chart_date", "date", "encounter_date", "creation_date", "created_at"):
        raw = enc.get(key)
        if raw:
            return str(raw).split("T")[0].split(" ")[0]
    return ""


def _date_like(v) -> str:
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _split_words(v) -> List[str]:
    return [w.strip().lower() for w in re.split(r"[|,;/]+", str(v or "")) if w.strip()]


BACKGROUND_NEEDLES = {
    "hosp_head_injury": ("hospitalised",),
    "headache_disorder": ("headache disorder",),
    "learning_disability": ("learning disability",),
    "adhd": ("adhd",),
    "psych_disorder": ("psychological",),
}

_ORIENT_NEEDLES = [("what month",), ("date today",), ("day of the week",),
                   ("what year",), ("time it right now",)]


def parse_scat6_encounter(enc: Dict, encounter_id: Optional[int] = None) -> Optional[Dict]:
    """Convert a Juvonno SCAT6 encounter into {'assessment':…, 'scores':…},
    or None if the encounter isn't a SCAT6."""
    if not isinstance(enc, dict) or not enc:
        return None
    names, pairs = _collect_fields(enc)
    hay = " ".join(names).lower() + " " + " ".join(l for l, _ in pairs)
    if not re.search(r"scat[\s\-]?6|scat6", hay):
        return None

    a: Dict = {"encounter_fields": [list(p) for p in pairs]}
    direct: Dict = {}   # clinician-recorded scores — take precedence

    # ── header / identity ──
    cust = enc.get("customer") or {}
    if isinstance(cust, dict) and cust:
        a["athlete_id"] = cust.get("id")
        a["athlete_name"] = f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip()
    exam_date = (_date_like(_first(pairs, "date of examination"))
                 or _date_like((_exact_all(pairs, "date") or [""])[0])
                 or _enc_date(enc))
    a["date_of_examination"] = exam_date
    a["time_of_examination"] = ""
    a["examiner"] = ((_exact_all(pairs, "name") or [None])[0]
                     or _first(pairs, "examiner") or _first(pairs, "clinician") or "")
    a["examiner_title"] = _first(pairs, "title") or ""
    a["notes"] = _first(pairs, "additional clinical notes") or ""

    v = _first(pairs, "neurological")
    a["neuro_exam"] = ("Abnormal" if v and "abnormal" in _norm(v)
                       else "Normal" if v else "")
    v = _first(pairs, "concussion diagnosed")
    if v:
        t = _norm(v)
        a["concussion_diagnosed"] = ("Deferred" if "defer" in t
                                     else "Yes" if t.startswith("y")
                                     else "No" if t.startswith("n") else v)
    else:
        a["concussion_diagnosed"] = ""
    v = _first(pairs, "different from", "usual self")
    if v:
        t = _norm(v)
        a["different_from_usual"] = ("Not applicable" if "applicable" in t
                                     else "Yes" if t.startswith("y")
                                     else "No" if t.startswith("n") else v)
    else:
        a["different_from_usual"] = ""

    # baseline vs post-injury (template name or a field valued "Baseline")
    field_vals = " ".join(v.lower() for _, v in pairs if len(v) < 40)
    if "baseline" in " ".join(names).lower() or "baseline" in field_vals:
        a["assessment_type"] = "baseline"
    else:
        a["assessment_type"] = "post_injury"

    # ── athlete background ──
    bg = {}
    for key, needles in BACKGROUND_NEEDLES.items():
        v = _first(pairs, *needles)
        if v is not None:
            bg[key] = _yn(v)
    a["background"] = bg
    a["medications"] = _first(pairs, "current medications") or ""

    # ── symptoms (22 items; labels match SCAT6 wording) ──
    sym_ratings, sym_found = [], 0
    for symptom in S.SYMPTOMS:
        needle = re.sub(r"\s*\(.*\)$", "", _norm(symptom)).strip()
        val = None
        for label, value in pairs:
            if needle and needle in label and "disorder" not in label:
                val = value
                break
        r = _int(val)
        if r is not None:
            sym_found += 1
            sym_ratings.append(max(0, min(6, r)))
        else:
            sym_ratings.append(0)
    if sym_found:
        a["symptom_ratings"] = sym_ratings
    direct["symptom_number"] = _int(_first(pairs, "total number of symptoms")
                                    or _first(pairs, "symptom number"))
    direct["symptom_severity"] = _int(_first(pairs, "symptom severity"))
    a["worse_physical"] = _yn(_first(pairs, "physical activity"))
    a["worse_mental"] = _yn(_first(pairs, "mental activity"))
    a["percent_normal"] = _int(_first(pairs, "percent of normal")
                               or _first(pairs, "% of normal"))
    a["percent_normal_why"] = _first(pairs, "if not 100") or ""

    # ── orientation ──
    orient, o_found = [], 0
    for needles in _ORIENT_NEEDLES:
        i = _int(_first(pairs, *needles))
        if i in (0, 1):
            o_found += 1
            orient.append(i)
        else:
            orient.append(None)
    if o_found:
        a["orientation_answers"] = orient
    direct["orientation"] = _int(_first(pairs, "orientation score")
                                 or _first(pairs, "orientation of 5")
                                 or _first(pairs, "orientation"))

    # ── immediate memory / delayed recall word trials ──
    a["word_list"] = ((_first(pairs, "word list") or "A").strip().upper() or "A")[:1]
    words = S.WORD_LISTS.get(a["word_list"], S.WORD_LISTS["A"])
    word_trials = {1: [], 2: [], 3: []}   # word-string values per exact "trial n"
    for n in (1, 2, 3):
        for v in _exact_all(pairs, f"trial {n}"):
            if not _is_numeric(v) and re.search(r"[a-zA-Z]", v):
                word_trials[n].append(v)
    im_trials = []
    for n in (1, 2, 3):
        recalled = set(_split_words(word_trials[n][0])) if word_trials[n] else set()
        im_trials.append([1 if w.lower() in recalled else 0 for w in words])
    if any(any(t) for t in im_trials):
        a["im_trials"] = im_trials
    # a second word-string "trial 1" is the delayed-recall trial
    if len(word_trials[1]) >= 2:
        recalled = set(_split_words(word_trials[1][-1]))
        a["dr_hits"] = [1 if w.lower() in recalled else 0 for w in words]
    direct["im_trial1"] = _int(_first(pairs, "trial 1 total"))
    direct["im_trial2"] = _int(_first(pairs, "trial 2 total"))
    direct["im_trial3"] = _int(_first(pairs, "trial 3 total"))
    direct["immediate_memory"] = _int(_first(pairs, "immediate memory score")
                                      or _first(pairs, "immediate memory of 30")
                                      or _first(pairs, "immediate memory"))
    direct["delayed_recall"] = _int(_first(pairs, "delayed recall score")
                                    or _first(pairs, "delayed recall of 10")
                                    or _first(pairs, "delayed recall"))

    # ── concentration ──
    a["digit_list"] = ((_first(pairs, "digit list") or "A").strip().upper() or "A")[:1]
    direct["digits_score"] = _int(_first(pairs, "digits score"))
    a["months_seconds"] = _num(_first(pairs, "time taken to complete"))
    a["months_errors"] = _int(_first(pairs, "number of errors"))
    direct["months_score"] = _int(_first(pairs, "months score"))
    direct["concentration"] = _int(_first(pairs, "concentration score")
                                   or _first(pairs, "concentration of 5")
                                   or _first(pairs, "concentration"))
    direct["cognitive_total"] = _int(_first(pairs, "cognitive total")
                                     or _first(pairs, "total of 50"))

    # ── GCS / Maddocks (present on post-injury templates) ──
    a["gcs_e"] = _int(_first(pairs, "eye response"))
    a["gcs_v"] = _int(_first(pairs, "verbal response"))
    a["gcs_m"] = _int(_first(pairs, "motor response"))
    direct["gcs_total"] = _int(_first(pairs, "gcs"))
    direct["maddocks"] = _int(_first(pairs, "maddocks"))

    # ── mBESS: firm block first, foam block second (identical labels) ──
    def _stance(*needles):
        vals = _all(pairs, *needles)
        firm = _int(vals[0]) if len(vals) >= 1 else None
        foam = _int(vals[1]) if len(vals) >= 2 else None
        return firm, foam
    a["mbess_double"], a["mbess_foam_double"] = _stance("double", "stance")
    a["mbess_tandem"], a["mbess_foam_tandem"] = _stance("tandem", "stance")
    # covers both "Single Leg Stance" and the template's "Single Less Stance" typo
    a["mbess_single"], a["mbess_foam_single"] = _stance("single", "stance")
    totals = _all(pairs, "total errors")
    direct["mbess_total"] = _int(totals[0]) if len(totals) >= 1 else \
        _int(_first(pairs, "mbess total"))
    direct["mbess_foam_total"] = _int(totals[1]) if len(totals) >= 2 else None
    a["foot_tested"] = _first(pairs, "foot tested") or ""
    a["test_surface"] = _first(pairs, "testing surface") or ""
    a["footwear"] = _first(pairs, "footwear") or ""

    # ── tandem gait: numeric-valued exact "trial n" fields ──
    tg = []
    for n in (1, 2, 3):
        numeric = [v for v in _exact_all(pairs, f"trial {n}") if _is_numeric(v)]
        tg.append(_num(numeric[0]) if numeric else None)
    a["tg_times"] = tg
    direct["tg_average"] = _num(_first(pairs, "average"))
    direct["tg_fastest"] = _num(_first(pairs, "fastest", exclude=("dual",)))
    direct["dual_task_fastest"] = _num(_first(pairs, "dual task fastest")
                                       or _first(pairs, "dual", "fastest"))

    # ── King-Devick add-on (kept as extra data) ──
    kd_total = _num(_first(pairs, "k d total time") or _first(pairs, "kd total"))
    if kd_total is not None:
        a["kd_total_time"] = kd_total
        a["kd_card_times"] = [_num(_first(pairs, f"card {n} time")) for n in (1, 2, 3)]

    # ── concussion history / demographics ──
    a["num_past_concussions"] = _int(_first(pairs, "how many", "concussion"))
    a["dob"] = _date_like(_first(pairs, "date of birth") or _first(pairs, "dob"))

    # ── scores: compute from items, then let recorded values take precedence ──
    scores = S.compute_all_scores(a)
    for key, val in direct.items():
        if val is not None:
            scores[key] = val
    # Domains with neither item data nor a recorded score are absent (None),
    # not zero — computed defaults must not fabricate results.
    has_im = bool(a.get("im_trials"))
    presence = {
        "symptom_number": sym_found > 0, "symptom_severity": sym_found > 0,
        "orientation": o_found > 0,
        "im_trial1": has_im, "im_trial2": has_im, "im_trial3": has_im,
        "immediate_memory": has_im,
        "digits_score": False,
        "months_score": a.get("months_seconds") is not None,
        "concentration": False,
        "delayed_recall": "dr_hits" in a,
        "cognitive_total": False,
        "maddocks": False,
        "gcs_total": any(a.get(k) is not None for k in ("gcs_e", "gcs_v", "gcs_m")),
        "mbess_total": any(a.get(k) is not None
                           for k in ("mbess_double", "mbess_tandem", "mbess_single")),
        "mbess_foam_total": any(a.get(k) is not None
                                for k in ("mbess_foam_double", "mbess_foam_tandem",
                                          "mbess_foam_single")),
        "tg_average": any(t is not None for t in a.get("tg_times", [])),
        "tg_fastest": any(t is not None for t in a.get("tg_times", [])),
        "dual_task_fastest": False,
    }
    for key, has_items in presence.items():
        if not has_items and direct.get(key) is None:
            scores[key] = None
    # rebuild aggregates from best available parts when not recorded
    if direct.get("concentration") is None and (
            scores.get("digits_score") or scores.get("months_score")):
        scores["concentration"] = int(scores.get("digits_score") or 0) \
            + int(scores.get("months_score") or 0)
    if direct.get("cognitive_total") is None:
        comp = [scores.get(k) for k in ("orientation", "immediate_memory",
                                        "concentration", "delayed_recall")]
        if any(c not in (None, "", 0) for c in comp):
            scores["cognitive_total"] = sum(int(c or 0) for c in comp)

    a["source"] = "juvonno_intake"
    if encounter_id is not None:
        a["encounter_id"] = int(encounter_id)
    a["template_name"] = names[0] if names else "SCAT6 intake"
    a.setdefault("sport_team", "")
    return {"assessment": a, "scores": scores}
