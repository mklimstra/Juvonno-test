# scat6_encounter.py — detect SCAT6 intakes/charts filled out inside Juvonno
# and convert their form fields into the app's assessment + scores format.
#
# Juvonno encounter payloads look like:
#   enc["data"][i]["template"]["tab_name"]  → form/tab name
#   enc["data"][i]["fields"]                → [{"name"/"label": …, "value": …}, …]
#
# Template field labels vary between installs, so matching is fuzzy: labels are
# normalized and matched on distinctive keywords. Wherever per-item data is
# found (e.g. all 22 symptom ratings) the scores are recomputed by scat6.py;
# where the template only records totals, those totals are used directly. The
# raw field map is kept on the assessment so nothing is discarded.
from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple

import scat6 as S


# ────────── helpers ──────────
def _norm(s) -> str:
    s = str(s or "").lower()
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9%' ]+", " ", s).strip()


def _num(v) -> Optional[float]:
    """First number in a value ('3', '3 - moderate', '12.5 s' …)."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    if not m:
        return None
    f = float(m.group(0))
    return f


def _int(v) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _yn(v) -> str:
    t = _norm(v)
    if t.startswith(("y", "1", "true", "checked")):
        return "Y"
    if t.startswith(("n", "0", "false")):
        return "N"
    return ""


def _collect_fields(enc: Dict) -> Tuple[List[str], Dict[str, str]]:
    """Return (template_names, {normalized_label: value}) for an encounter."""
    names: List[str] = []
    fields: Dict[str, str] = {}
    data = enc.get("data")
    blocks = data if isinstance(data, list) else []
    for block in blocks:
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
            if label_n and value_s and not value_s.startswith("---"):
                fields[label_n] = value_s
    return names, fields


def is_scat6_encounter(enc: Dict) -> bool:
    names, fields = _collect_fields(enc)
    hay = " ".join(names).lower() + " " + " ".join(fields.keys())
    return bool(re.search(r"scat[\s\-]?6|scat6", hay))


def _find(fields: Dict[str, str], *needles, exclude=()) -> Optional[str]:
    """Value of the first field whose normalized label contains every needle
    (and none of the excludes)."""
    for label, value in fields.items():
        if all(n in label for n in needles) and not any(x in label for x in exclude):
            return value
    return None


def _enc_date(enc: Dict) -> str:
    for key in ("chart_date", "date", "encounter_date", "creation_date", "created_at"):
        raw = enc.get(key)
        if raw:
            return str(raw).split("T")[0].split(" ")[0]
    return ""


# distinctive keyword sets for the orientation questions
_ORIENT_NEEDLES = [("month",), ("date",), ("day", "week"), ("year",), ("time",)]


def parse_scat6_encounter(enc: Dict, encounter_id: Optional[int] = None) -> Optional[Dict]:
    """Convert a Juvonno SCAT6 encounter into {'assessment':…, 'scores':…},
    or None if the encounter isn't a SCAT6."""
    if not isinstance(enc, dict) or not enc:
        return None
    names, fields = _collect_fields(enc)
    hay = " ".join(names).lower() + " " + " ".join(fields.keys())
    if not re.search(r"scat[\s\-]?6|scat6", hay):
        return None

    a: Dict = {"encounter_fields": dict(fields)}
    direct: Dict = {}   # totals recorded directly in the template

    # ── header info ──
    a["date_of_examination"] = (
        (_find(fields, "date", "examination") or "").split(" ")[0] or _enc_date(enc))
    a["time_of_examination"] = _find(fields, "time", "examination") or ""
    a["examiner"] = _find(fields, "examiner") or _find(fields, "clinician") or ""
    a["notes"] = _find(fields, "clinical notes") or _find(fields, "additional notes") or ""
    a["neuro_exam"] = ""
    v = _find(fields, "neurological")
    if v:
        a["neuro_exam"] = "Abnormal" if "abnormal" in _norm(v) else "Normal"
    v = _find(fields, "concussion diagnosed") or _find(fields, "diagnosed", exclude=("past",))
    if v:
        t = _norm(v)
        a["concussion_diagnosed"] = ("Deferred" if "defer" in t
                                     else "Yes" if t.startswith("y") else
                                     "No" if t.startswith("n") else v)
    # baseline vs post-injury
    tpl_hay = hay
    v = _find(fields, "assessment type") or _find(fields, "baseline")
    if v and "baseline" in _norm(v):
        a["assessment_type"] = "baseline"
    elif v and ("post" in _norm(v) or "injury" in _norm(v)):
        a["assessment_type"] = "post_injury"
    elif "baseline" in tpl_hay:
        a["assessment_type"] = "baseline"
    else:
        a["assessment_type"] = "post_injury"

    # ── symptoms (22 items) ──
    sym_ratings, sym_found = [], 0
    for symptom in S.SYMPTOMS:
        needle = _norm(symptom).replace(" if applicable", "").strip()
        needle = re.sub(r"\s*\(.*\)$", "", needle).strip()
        val = None
        for label, value in fields.items():
            if needle and needle in label:
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
    else:
        direct["symptom_number"] = _int(_find(fields, "symptom", "number")
                                        or _find(fields, "number of symptoms"))
        direct["symptom_severity"] = _int(_find(fields, "symptom", "severity"))
    a["worse_physical"] = _yn(_find(fields, "physical activity"))
    a["worse_mental"] = _yn(_find(fields, "mental activity"))
    a["percent_normal"] = _int(_find(fields, "percent"))

    # ── orientation ──
    orient, o_found = [], 0
    for needles in _ORIENT_NEEDLES:
        v = _find(fields, *needles, exclude=("injury", "examination", "trial",
                                             "recovery", "recall", "birth"))
        i = _int(v)
        if i is not None and i in (0, 1):
            o_found += 1
            orient.append(i)
        else:
            orient.append(None)
    if o_found:
        a["orientation_answers"] = orient
    else:
        direct["orientation"] = _int(_find(fields, "orientation"))

    # ── immediate memory ──
    t1 = _int(_find(fields, "trial 1", exclude=("gait", "tandem", "dual")))
    t2 = _int(_find(fields, "trial 2", exclude=("gait", "tandem", "dual")))
    t3 = _int(_find(fields, "trial 3", exclude=("gait", "tandem", "dual")))
    if any(x is not None for x in (t1, t2, t3)):
        direct["im_trial1"], direct["im_trial2"], direct["im_trial3"] = t1, t2, t3
        direct["immediate_memory"] = sum(x or 0 for x in (t1, t2, t3))
    else:
        direct["immediate_memory"] = _int(_find(fields, "immediate memory"))
    for k in ("a", "b", "c"):
        v = _find(fields, "word list")
        if v and _norm(v) == k:
            a["word_list"] = k.upper()

    # ── concentration ──
    direct["digits_score"] = _int(_find(fields, "digit", "score")
                                  or _find(fields, "digits"))
    a["months_seconds"] = _num(_find(fields, "months", "time")
                               or _find(fields, "time to complete"))
    a["months_errors"] = _int(_find(fields, "months", "error")
                              or _find(fields, "number of errors"))
    direct["months_score"] = _int(_find(fields, "months", "score"))
    direct["concentration"] = _int(_find(fields, "concentration"))

    # ── delayed recall ──
    direct["delayed_recall"] = _int(_find(fields, "delayed recall"))
    direct["cognitive_total"] = _int(_find(fields, "cognitive", "total")
                                     or _find(fields, "total cognitive"))

    # ── GCS ──
    a["gcs_e"] = _int(_find(fields, "eye", exclude=("ocular",)))
    a["gcs_v"] = _int(_find(fields, "verbal"))
    a["gcs_m"] = _int(_find(fields, "motor", exclude=("ocular", "screen")))
    direct["gcs_total"] = _int(_find(fields, "gcs"))
    direct["maddocks"] = _int(_find(fields, "maddocks"))

    # ── mBESS ──
    a["mbess_double"] = _int(_find(fields, "double leg", exclude=("foam",)))
    a["mbess_tandem"] = _int(_find(fields, "tandem stance", exclude=("foam",)))
    a["mbess_single"] = _int(_find(fields, "single leg", exclude=("foam",)))
    a["mbess_foam_double"] = _int(_find(fields, "foam", "double"))
    a["mbess_foam_tandem"] = _int(_find(fields, "foam", "tandem"))
    a["mbess_foam_single"] = _int(_find(fields, "foam", "single"))
    direct["mbess_total"] = _int(_find(fields, "total errors", exclude=("foam",))
                                 or _find(fields, "bess", "total", exclude=("foam",)))
    a["foot_tested"] = _find(fields, "foot tested") or ""

    # ── tandem gait / dual task ──
    tg = [_num(_find(fields, "tandem", f"trial {i}")
               or _find(fields, "gait", f"trial {i}", exclude=("dual",)))
          for i in (1, 2, 3)]
    a["tg_times"] = tg
    direct["tg_fastest"] = _num(_find(fields, "tandem", "fastest")
                                or _find(fields, "gait", "fastest", exclude=("dual",)))
    direct["tg_average"] = _num(_find(fields, "tandem", "average")
                                or _find(fields, "gait", "average"))
    dual = [_num(_find(fields, "dual", f"trial {i}")) for i in (1, 2, 3)]
    a["dual_task_times"] = dual
    direct["dual_task_fastest"] = _num(_find(fields, "dual", "fastest"))

    # ── concussion history ──
    a["num_past_concussions"] = _int(_find(fields, "past", exclude=("diagnosed",))
                                     or _find(fields, "how many", "concussion"))
    a["dob"] = (_find(fields, "date of birth") or _find(fields, "dob") or "").split(" ")[0]

    # ── compute, then overlay recorded totals where items were absent ──
    scores = S.compute_all_scores(a)
    computed_flags = {
        "symptom_number": sym_found > 0, "symptom_severity": sym_found > 0,
        "orientation": o_found > 0,
        "immediate_memory": False, "im_trial1": False, "im_trial2": False,
        "im_trial3": False,
        "digits_score": False, "months_score": a.get("months_seconds") is not None,
        "concentration": False, "delayed_recall": False, "cognitive_total": False,
        "gcs_total": all(a.get(k) is not None for k in ("gcs_e", "gcs_v", "gcs_m")),
        "maddocks": False,
        "mbess_total": any(a.get(k) is not None
                           for k in ("mbess_double", "mbess_tandem", "mbess_single")),
        "tg_fastest": any(t is not None for t in tg),
        "tg_average": any(t is not None for t in tg),
        "dual_task_fastest": any(t is not None for t in dual),
    }
    for key, val in direct.items():
        if val is None:
            continue
        if not computed_flags.get(key, False):
            scores[key] = val
    # rebuild aggregates from the best available parts
    if not computed_flags["concentration"] and direct.get("concentration") is None:
        parts = [scores.get("digits_score"), scores.get("months_score")]
        if any(p not in (None, "") for p in parts):
            scores["concentration"] = sum(int(p or 0) for p in parts)
    if direct.get("cognitive_total") is None:
        comp = [scores.get(k) for k in ("orientation", "immediate_memory",
                                        "concentration", "delayed_recall")]
        if any(c not in (None, "", 0) for c in comp):
            scores["cognitive_total"] = sum(int(c or 0) for c in comp)

    a["source"] = "juvonno_intake"
    if encounter_id is not None:
        a["encounter_id"] = int(encounter_id)
    tab = names[0] if names else "SCAT6 intake"
    a.setdefault("sport_team", "")
    a["template_name"] = tab
    return {"assessment": a, "scores": scores}
