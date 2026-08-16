# scat6_pdf.py — render a completed SCAT6 assessment as a clinical PDF
# (reportlab), closely following the content of the official SCAT6 form
# (Echemendia RJ, et al. Br J Sports Med 2023;57:622-631), and read the data
# back out of PDFs pulled from Juvonno.
#
# Every generated PDF embeds the full assessment + scores as JSON in the PDF
# Subject metadata, so history can be reconstructed losslessly from the
# documents stored in Juvonno ("scraping"). A text-based fallback parses the
# Decision summary table of PDFs that lack the embedded JSON.
from __future__ import annotations
import io, os, json, re
from typing import Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle)

import scat6 as S

NAVY = colors.HexColor("#2b3a67")
LIGHT = colors.HexColor("#eef2f7")
ACCENT = colors.HexColor("#2f6fb2")
RED = colors.HexColor("#8f1f2f")
GREEN = colors.HexColor("#1d5b3c")

# Link back to the intake app, shown as a button on every generated PDF.
TOOL_URL = os.getenv(
    "SCAT6_TOOL_URL",
    "https://0199594c-6df2-cf52-c051-91a6b8901094.share.connect.posit.cloud/home")

JSON_MARKER = "SCAT6JSON:"

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_styles["Heading1"], fontSize=16, textColor=NAVY,
                    spaceAfter=0, spaceBefore=0)
SUB = ParagraphStyle("SUB", parent=_styles["BodyText"], fontSize=7.5, leading=9.5,
                     textColor=colors.HexColor("#555555"))
H2 = ParagraphStyle("H2", parent=_styles["Heading2"], fontSize=11, textColor=colors.white,
                    backColor=NAVY, borderPadding=(4, 4, 4), spaceBefore=10, spaceAfter=4)
H2R = ParagraphStyle("H2R", parent=H2, backColor=RED)
BODY = ParagraphStyle("Body", parent=_styles["BodyText"], fontSize=9, leading=12)
BODY_B = ParagraphStyle("BodyB", parent=BODY, fontName="Helvetica-Bold")
SMALL = ParagraphStyle("Small", parent=_styles["BodyText"], fontSize=7.5, leading=9.5,
                       textColor=colors.HexColor("#555555"))
BTN = ParagraphStyle("Btn", parent=_styles["BodyText"], fontSize=11, leading=13,
                     alignment=1)  # centered

FULL_W = 7.1 * inch


def _p(v, style=BODY):
    return Paragraph("—" if v in (None, "") else str(v), style)


def _grid_style(header=True):
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d2dd")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), NAVY),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT])]
    else:
        cmds += [("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT])]
    return TableStyle(cmds)


def _kv_table(pairs: List[tuple], ncols: int = 2) -> Table:
    rows, row = [], []
    for k, v in pairs:
        row.extend([Paragraph(f"<b>{k}</b>", BODY), _p(v)])
        if len(row) >= ncols * 2:
            rows.append(row); row = []
    if row:
        while len(row) < ncols * 2:
            row.append(Paragraph("", BODY))
        rows.append(row)
    widths = []
    for _ in range(ncols):
        widths.extend([1.65 * inch, FULL_W / ncols - 1.65 * inch])
    t = Table(rows, colWidths=widths)
    t.setStyle(_grid_style(header=False))
    return t


def _item_table(header_cells: List[str], rows: List[List], widths: List[float]) -> Table:
    data = [[Paragraph(f"<b>{h}</b>", BODY) for h in header_cells]]
    for r in rows:
        data.append([c if isinstance(c, Paragraph) else _p(c) for c in r])
    t = Table(data, colWidths=widths)
    t.setStyle(_grid_style(header=True))
    return t


def _button(text: str, url: str) -> Table:
    p = Paragraph(f'<link href="{url}"><font color="white"><b>{text}</b></font></link>', BTN)
    t = Table([[p]], colWidths=[1.9 * inch], rowHeights=[0.32 * inch])
    style = [("BACKGROUND", (0, 0), (-1, -1), ACCENT),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("BOX", (0, 0), (-1, -1), 1, ACCENT)]
    try:
        t.setStyle(TableStyle(style + [("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    except Exception:
        t.setStyle(TableStyle(style))
    return t


def _yn_p(v):
    if v == "Y" or v is True or v == "Yes":
        return Paragraph("<b>Y</b>", BODY)
    if v == "N" or v is False or v == "No":
        return Paragraph("N", BODY)
    return Paragraph("—", BODY)


def _score01(v):
    return _p("—" if v in (None, "") else str(v))


# ══════════ Build ══════════
def build_scat6_pdf(assessment: Dict, scores: Dict) -> bytes:
    a, s = assessment, scores
    buf = io.BytesIO()
    embedded = JSON_MARKER + json.dumps({"assessment": a, "scores": s}, default=str)
    doc = SimpleDocTemplate(
        buf, pagesize=letter, topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        title=f"SCAT6 — {a.get('athlete_name', '')} — {a.get('date_of_examination', '')}",
        author="CSI Pacific SCAT6 Intake Tool",
        subject=embedded)
    el = []

    # Header row: title + button
    head = Table([[
        [Paragraph("SCAT6™ — Sport Concussion Assessment Tool", H1),
         Paragraph("For Adolescents (13 years +) &amp; Adults — for use by Health Care "
                   "Professionals only.", SUB)],
        _button("Go to SCAT6 Tool", TOOL_URL),
    ]], colWidths=[FULL_W - 2.0 * inch, 2.0 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    el.append(head)
    el.append(Spacer(1, 4))

    # ── Athlete information ──
    el.append(Paragraph("Athlete Information", H2))
    atype = (a.get("assessment_type") or "").replace("_", "-").title()
    el.append(_kv_table([
        ("Athlete name", a.get("athlete_name")), ("ID number (Juvonno)", a.get("athlete_id")),
        ("Date of birth", a.get("dob")), ("Sex", a.get("sex")),
        ("Date of examination", a.get("date_of_examination")),
        ("Time of examination", a.get("time_of_examination")),
        ("Assessment type", atype), ("Dominant hand", a.get("dominant_hand")),
        ("Date of injury", a.get("date_of_injury")), ("Time of injury", a.get("time_of_injury")),
        ("Time since injury", a.get("time_since_injury")),
        ("Sport / Team / School", a.get("sport_team")),
        ("Examiner", a.get("examiner")), ("", ""),
    ]))

    # ── Concussion history ──
    el.append(Paragraph("Concussion History", H2))
    el.append(_kv_table([
        ("Past diagnosed concussions", a.get("num_past_concussions")),
        ("Most recent concussion", a.get("most_recent_concussion")),
        ("Recovery time (days)", a.get("recovery_days")),
        ("Primary symptoms", a.get("primary_symptoms")),
    ]))

    # ── Athlete background ──
    bg = a.get("background", {}) or {}
    el.append(Paragraph("Athlete Background", H2))
    el.append(_item_table(
        ["Has the athlete ever been…", "Y/N"],
        [[Paragraph(label, BODY), _yn_p(bg.get(key))] for key, label in S.BACKGROUND_ITEMS],
        [FULL_W - 0.7 * inch, 0.7 * inch]))
    el.append(Spacer(1, 3))
    el.append(_kv_table([("Notes", a.get("background_notes")),
                         ("Current medications", a.get("medications"))], ncols=1))

    # ── Red flags ──
    el.append(Paragraph("Red Flags (Box 1)", H2R))
    red = a.get("red_flags", [])
    red_rows = []
    for i, flag in enumerate(S.RED_FLAGS):
        present = bool(red[i]) if isinstance(red, list) and i < len(red) else False
        red_rows.append([Paragraph(flag, BODY),
                         Paragraph("<b><font color='#8f1f2f'>YES</font></b>", BODY)
                         if present else Paragraph("—", BODY)])
    el.append(_item_table(["Red flag", "Present"], red_rows,
                          [FULL_W - 0.9 * inch, 0.9 * inch]))

    # ── Step 1: Observable signs ──
    el.append(Paragraph("Immediate Assessment — Step 1: Observable Signs", H2))
    obs_ctx = ", ".join(a.get("obs_context") or []) or "—"
    el.append(Paragraph(f"<b>Context:</b> {obs_ctx}", BODY))
    obs = a.get("observable_signs", [])
    el.append(_item_table(
        ["Sign", "Y/N"],
        [[Paragraph(sign, BODY),
          _yn_p(obs[i] if isinstance(obs, list) and i < len(obs) else None)]
         for i, sign in enumerate(S.OBSERVABLE_SIGNS)],
        [FULL_W - 0.7 * inch, 0.7 * inch]))

    # ── Step 2: GCS ──
    el.append(Paragraph("Immediate Assessment — Step 2: Glasgow Coma Scale", H2))
    el.append(_item_table(
        ["Best Eye Response (E)", "Best Verbal Response (V)", "Best Motor Response (M)",
         "GCS Total (E+V+M)"],
        [[_p(a.get("gcs_e")), _p(a.get("gcs_v")), _p(a.get("gcs_m")),
          Paragraph(f"<b>{s.get('gcs_total') if s.get('gcs_total') is not None else '—'}"
                    f" / 15</b>", BODY)]],
        [FULL_W / 4] * 4))

    # ── Step 3: Cervical spine ──
    el.append(Paragraph("Immediate Assessment — Step 3: Cervical Spine", H2))
    cerv = a.get("cervical_items", [])
    el.append(_item_table(
        ["Question", "Y/N"],
        [[Paragraph(q, BODY),
          _yn_p(cerv[i] if isinstance(cerv, list) and i < len(cerv) else None)]
         for i, q in enumerate(S.CERVICAL_ITEMS)],
        [FULL_W - 0.7 * inch, 0.7 * inch]))

    # ── Step 4: Coordination & ocular/motor ──
    el.append(Paragraph("Immediate Assessment — Step 4: Coordination & Ocular/Motor Screen", H2))
    coord = a.get("coord_ocular_items", [])
    coord_rows = [[Paragraph(q, BODY),
                   _yn_p(coord[i] if isinstance(coord, list) and i < len(coord) else None)]
                  for i, q in enumerate(S.COORD_OCULAR_ITEMS)]
    el.append(_item_table(["Question", "Y/N"], coord_rows,
                          [FULL_W - 0.7 * inch, 0.7 * inch]))
    if (a.get("ocular_description") or "").strip():
        el.append(Spacer(1, 3))
        el.append(Paragraph(f"<b>If abnormal, description:</b> "
                            f"{a.get('ocular_description')}", BODY))

    # ── Step 5: Maddocks ──
    el.append(Paragraph("Immediate Assessment — Step 5: Memory Assessment "
                        "Maddocks Questions", H2))
    mad = a.get("maddocks_answers", [])
    mad_rows = [[Paragraph(q, BODY),
                 _score01(mad[i] if isinstance(mad, list) and i < len(mad) else None)]
                for i, q in enumerate(S.MADDOCKS_QUESTIONS)]
    mad_rows.append([Paragraph("<b>Maddocks Score</b>", BODY),
                     Paragraph(f"<b>{s.get('maddocks', '—')} / 5</b>", BODY)])
    el.append(_item_table(["Question (1 point each)", "Score"], mad_rows,
                          [FULL_W - 0.9 * inch, 0.9 * inch]))

    # ── Symptom evaluation (all 22 items) ──
    el.append(Paragraph("Off-Field — Step 2: Symptom Evaluation", H2))
    ratings = a.get("symptom_ratings", [])
    half = (len(S.SYMPTOMS) + 1) // 2
    sym_rows = []
    for r in range(half):
        row = []
        for idx in (r, r + half):
            if idx < len(S.SYMPTOMS):
                v = ratings[idx] if idx < len(ratings) else 0
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = 0
                row.extend([Paragraph(S.SYMPTOMS[idx], BODY),
                            Paragraph(f"<b>{v}</b>" if v > 0 else "0", BODY)])
            else:
                row.extend([Paragraph("", BODY), Paragraph("", BODY)])
        sym_rows.append(row)
    t = Table([[Paragraph("<b>Symptom</b>", BODY), Paragraph("<b>0–6</b>", BODY)] * 2]
              + sym_rows,
              colWidths=[FULL_W / 2 - 0.5 * inch, 0.5 * inch] * 2)
    t.setStyle(_grid_style(header=True))
    el.append(t)
    el.append(Spacer(1, 3))
    el.append(_kv_table([
        ("Symptom number", f"{s.get('symptom_number', '—')} of 22"),
        ("Symptom severity", f"{s.get('symptom_severity', '—')} of 132"),
        ("Worse with physical activity", a.get("worse_physical")),
        ("Worse with mental activity", a.get("worse_mental")),
        ("Percent of normal", a.get("percent_normal")),
        ("If not 100%, why", a.get("percent_normal_why")),
    ]))

    # ── Cognitive screening ──
    el.append(Paragraph("Off-Field — Step 3: Cognitive Screening (SAC) — Orientation", H2))
    orient = a.get("orientation_answers", [])
    orows = [[Paragraph(q, BODY),
              _score01(orient[i] if isinstance(orient, list) and i < len(orient) else None)]
             for i, q in enumerate(S.ORIENTATION_QUESTIONS)]
    orows.append([Paragraph("<b>Orientation Score</b>", BODY),
                  Paragraph(f"<b>{s.get('orientation', '—')} / 5</b>", BODY)])
    el.append(_item_table(["Question (1 point each)", "Score"], orows,
                          [FULL_W - 0.9 * inch, 0.9 * inch]))

    el.append(Paragraph("Off-Field — Step 3: Immediate Memory", H2))
    wl = a.get("word_list", "A")
    words = S.WORD_LISTS.get(wl, S.WORD_LISTS["A"])
    trials = a.get("im_trials", [[], [], []])
    im_rows = []
    for wi, w in enumerate(words):
        row = [Paragraph(w, BODY)]
        for t_i in range(3):
            hit = (trials[t_i][wi] if isinstance(trials, list) and len(trials) > t_i
                   and isinstance(trials[t_i], list) and len(trials[t_i]) > wi else 0)
            row.append(Paragraph("<b>1</b>" if hit else "0", BODY))
        im_rows.append(row)
    im_rows.append([Paragraph("<b>Trial totals</b>", BODY),
                    Paragraph(f"<b>{s.get('im_trial1', '—')}</b>", BODY),
                    Paragraph(f"<b>{s.get('im_trial2', '—')}</b>", BODY),
                    Paragraph(f"<b>{s.get('im_trial3', '—')}</b>", BODY)])
    el.append(_item_table(
        [f"Word (List {wl})", "Trial 1", "Trial 2", "Trial 3"], im_rows,
        [FULL_W - 3 * 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch]))
    el.append(Spacer(1, 3))
    el.append(_kv_table([
        ("Immediate Memory Score", f"{s.get('immediate_memory', '—')} of 30"),
        ("Time last trial completed", a.get("im_time_completed")),
    ]))

    el.append(Paragraph("Off-Field — Step 3: Concentration", H2))
    dl = a.get("digit_list", "A")
    pairs = S.DIGIT_LISTS.get(dl, S.DIGIT_LISTS["A"])
    digs = a.get("digit_rows", [])
    dig_rows = []
    for i, (s1, s2) in enumerate(pairs):
        v = digs[i] if isinstance(digs, list) and i < len(digs) else None
        dig_rows.append([Paragraph(f"{s1}  <font color='#888888'>(alt: {s2})</font>", BODY),
                         _score01(v)])
    dig_rows.append([Paragraph("<b>Digits Score</b>", BODY),
                     Paragraph(f"<b>{s.get('digits_score', '—')} / 4</b>", BODY)])
    el.append(_item_table([f"Digits Backward (List {dl}, 1 point per row)", "Score"],
                          dig_rows, [FULL_W - 0.9 * inch, 0.9 * inch]))
    el.append(Spacer(1, 3))
    el.append(_kv_table([
        ("Months in reverse — time (s)", a.get("months_seconds")),
        ("Months in reverse — errors", a.get("months_errors")),
        ("Months Score (of 1)", s.get("months_score")),
        ("Concentration Score (of 5)", s.get("concentration")),
    ]))

    # ── Balance ──
    el.append(Paragraph("Off-Field — Step 4: Coordination & Balance — Modified BESS", H2))
    el.append(_kv_table([
        ("Foot tested (non-dominant)", a.get("foot_tested")),
        ("Testing surface", a.get("test_surface")),
        ("Footwear", a.get("footwear")), ("", ""),
    ]))
    el.append(Spacer(1, 3))
    foam_any = s.get("mbess_foam_total") is not None
    bess_rows = [
        ["Double leg stance (of 10)", s.get("mbess_double"),
         a.get("mbess_foam_double") if foam_any else "—"],
        ["Tandem stance (of 10)", s.get("mbess_tandem"),
         a.get("mbess_foam_tandem") if foam_any else "—"],
        ["Single leg stance (of 10)", s.get("mbess_single"),
         a.get("mbess_foam_single") if foam_any else "—"],
        [Paragraph("<b>Total errors (of 30)</b>", BODY),
         Paragraph(f"<b>{s.get('mbess_total') if s.get('mbess_total') is not None else '—'}</b>", BODY),
         Paragraph(f"<b>{s.get('mbess_foam_total') if foam_any else '—'}</b>", BODY)],
    ]
    el.append(_item_table(["Stance (errors)", "Firm surface", "Foam (optional)"],
                          bess_rows, [FULL_W - 2.6 * inch, 1.3 * inch, 1.3 * inch]))

    el.append(Paragraph("Off-Field — Step 4: Timed Tandem Gait & Dual Task", H2))
    tg = a.get("tg_times", []) or []
    while len(tg) < 3:
        tg.append(None)
    dual = a.get("dual_task_times", []) or []
    while len(dual) < 3:
        dual.append(None)
    el.append(_item_table(
        ["", "Trial 1 (s)", "Trial 2 (s)", "Trial 3 (s)", "Average (s)", "Fastest (s)"],
        [[Paragraph("<b>Single task</b>", BODY), tg[0], tg[1], tg[2],
          s.get("tg_average"), s.get("tg_fastest")],
         [Paragraph("<b>Dual task</b>", BODY), dual[0], dual[1], dual[2], "—",
          s.get("dual_task_fastest")]],
        [1.5 * inch] + [(FULL_W - 1.5 * inch) / 5] * 5))
    el.append(Spacer(1, 3))
    el.append(_kv_table([
        ("Dual-task counting errors", a.get("dual_task_errors")),
        ("Trials not completed — why", a.get("tg_incomplete_reason")),
    ]))

    # ── Delayed recall ──
    el.append(Paragraph("Off-Field — Step 5: Delayed Recall", H2))
    dr = a.get("dr_hits", [])
    recalled = [words[i] for i, h in enumerate(dr)
                if h and i < len(words)] if isinstance(dr, list) else []
    el.append(_kv_table([
        ("Time started", a.get("dr_time_started")),
        (f"Words recalled (List {wl})", ", ".join(recalled) if recalled else "None"),
        ("Delayed Recall Score", f"{s.get('delayed_recall', '—')} of 10"), ("", ""),
    ]))

    # ── Step 6: Decision (summary table — stable labels, used by the scraper) ──
    el.append(Paragraph("Step 6: Decision", H2))
    dec_rows = [
        ("Neurological exam", a.get("neuro_exam")),
        ("Symptom number (of 22)", s.get("symptom_number")),
        ("Symptom severity (of 132)", s.get("symptom_severity")),
        ("Orientation (of 5)", s.get("orientation")),
        ("Immediate memory (of 30)", s.get("immediate_memory")),
        ("Concentration (of 5)", s.get("concentration")),
        ("Delayed recall (of 10)", s.get("delayed_recall")),
        ("Cognitive total (of 50)", s.get("cognitive_total")),
        ("mBESS total errors (of 30)", s.get("mbess_total")),
        ("Tandem gait fastest (s)", s.get("tg_fastest")),
        ("Dual task fastest (s)", s.get("dual_task_fastest")),
        ("Maddocks (of 5)", s.get("maddocks")),
        ("GCS (of 15)", s.get("gcs_total")),
    ]
    el.append(_item_table(
        ["Domain", "Result"],
        [[Paragraph(f"<b>{k}</b>", BODY), _p(v)] for k, v in dec_rows],
        [FULL_W - 1.6 * inch, 1.6 * inch]))
    el.append(Spacer(1, 3))
    el.append(_kv_table([
        ("Different from usual self", a.get("different_from_usual")),
        ("Concussion diagnosed?", a.get("concussion_diagnosed")),
    ]))
    notes = (a.get("notes") or "").strip()
    if notes:
        el.append(Paragraph("Additional Clinical Notes", H2))
        el.append(Paragraph(notes.replace("\n", "<br/>"), BODY))

    # ── Attestation ──
    el.append(Paragraph("Health Care Professional Attestation", H2))
    el.append(Paragraph("I am an HCP and I have personally administered or supervised "
                        "the administration of this SCAT6.", SMALL))
    el.append(Spacer(1, 2))
    el.append(_kv_table([
        ("Name", a.get("examiner")),
        ("Title / Speciality", a.get("examiner_title")),
        ("Registration / license #", a.get("examiner_license")),
        ("Date", a.get("date_of_examination")),
    ]))
    el.append(Spacer(1, 8))
    el.append(Paragraph(
        f'Generated by the CSI Pacific SCAT6 intake tool — '
        f'<link href="{TOOL_URL}" color="#2f6fb2"><u>Go to SCAT6 Tool</u></link>. '
        "SCAT6 © Concussion in Sport Group — Echemendia RJ, et al. Br J Sports Med "
        "2023;57:622–631. Scoring on the SCAT6 should not be used as a stand-alone "
        "method to diagnose concussion; an athlete can score within normal limits "
        "and still have a concussion.", SMALL))

    doc.build(el)
    return buf.getvalue()


# ══════════ Read back (scrape) ══════════
_FALLBACK_PATTERNS = {
    "symptom_number": r"Symptom number \(of 22\)\s+([\d.]+)",
    "symptom_severity": r"Symptom severity \(of 132\)\s+([\d.]+)",
    "orientation": r"Orientation \(of 5\)\s+([\d.]+)",
    "immediate_memory": r"Immediate memory \(of 30\)\s+([\d.]+)",
    "concentration": r"Concentration \(of 5\)\s+([\d.]+)",
    "delayed_recall": r"Delayed recall \(of 10\)\s+([\d.]+)",
    "cognitive_total": r"Cognitive total \(of 50\)\s+([\d.]+)",
    "mbess_total": r"mBESS total errors \(of 30\)\s+([\d.]+)",
    "tg_fastest": r"Tandem gait fastest \(s\)\s+([\d.]+)",
    "dual_task_fastest": r"Dual task fastest \(s\)\s+([\d.]+)",
    "maddocks": r"Maddocks \(of 5\)\s+([\d.]+)",
    "gcs_total": r"GCS \(of 15\)\s+([\d.]+)",
}


def extract_assessment_from_pdf(pdf_bytes: bytes) -> Optional[Dict]:
    """Recover {'assessment':…, 'scores':…} from a SCAT6 PDF.

    Primary path: the JSON blob embedded in the PDF Subject metadata (lossless).
    Fallback: parse the Decision summary table from the page text (scores only)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return None

    # 1) Embedded JSON
    try:
        subject = (reader.metadata or {}).get("/Subject") or ""
        if isinstance(subject, str) and subject.startswith(JSON_MARKER):
            js = json.loads(subject[len(JSON_MARKER):])
            if isinstance(js, dict) and "assessment" in js and "scores" in js:
                return js
    except Exception:
        pass

    # 2) Text fallback (older PDFs without embedded JSON)
    try:
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None
    if "SCAT6" not in text:
        return None
    scores: Dict = {}
    for key, pat in _FALLBACK_PATTERNS.items():
        m = re.search(pat, text)
        if m:
            try:
                val = float(m.group(1))
                scores[key] = int(val) if val.is_integer() else val
            except ValueError:
                pass
    assessment: Dict = {}
    m = re.search(r"Date of examination\s+([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    if m:
        assessment["date_of_examination"] = m.group(1)
    m = re.search(r"Athlete name\s+([^\n]+)", text)
    if m:
        assessment["athlete_name"] = m.group(1).strip()
    m = re.search(r"Examiner\s+([^\n]+)", text)
    if m:
        assessment["examiner"] = m.group(1).strip()
    m = re.search(r"Neurological exam\s+(Normal|Abnormal)", text)
    if m:
        assessment["neuro_exam"] = m.group(1)
    m = re.search(r"Concussion diagnosed\?\s+([^\n]+)", text)
    if m:
        assessment["concussion_diagnosed"] = m.group(1).strip()
    if not scores and not assessment:
        return None
    return {"assessment": assessment, "scores": scores, "partial": True}
