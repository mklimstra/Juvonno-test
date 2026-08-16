# scat6_pdf.py — render a completed SCAT6 assessment as a clean clinical PDF
# (reportlab). Layout is a clinical summary of the SCAT6, not a reproduction
# of the copyrighted BJSM form artwork.
from __future__ import annotations
import io
from typing import Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, KeepTogether)

import scat6 as S

NAVY = colors.HexColor("#2b3a67")
LIGHT = colors.HexColor("#eef2f7")
ACCENT = colors.HexColor("#2f6fb2")

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_styles["Heading1"], fontSize=16, textColor=NAVY,
                    spaceAfter=2)
H2 = ParagraphStyle("H2", parent=_styles["Heading2"], fontSize=11, textColor=colors.white,
                    backColor=NAVY, borderPadding=(4, 4, 4), spaceBefore=10, spaceAfter=4,
                    leftIndent=0)
BODY = ParagraphStyle("Body", parent=_styles["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("Small", parent=_styles["BodyText"], fontSize=7.5, leading=9.5,
                       textColor=colors.HexColor("#555555"))


def _kv_table(pairs: List[tuple], ncols: int = 2) -> Table:
    rows, row = [], []
    for k, v in pairs:
        row.extend([Paragraph(f"<b>{k}</b>", BODY), Paragraph("—" if v in (None, "") else str(v), BODY)])
        if len(row) >= ncols * 2:
            rows.append(row); row = []
    if row:
        while len(row) < ncols * 2:
            row.append(Paragraph("", BODY))
        rows.append(row)
    widths = []
    for _ in range(ncols):
        widths.extend([1.55 * inch, (7.0 / ncols - 1.55) * inch])
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _score_table(rows: List[tuple]) -> Table:
    data = [[Paragraph("<b>Domain</b>", BODY), Paragraph("<b>Score</b>", BODY)]]
    for label, val in rows:
        data.append([Paragraph(label, BODY), Paragraph("—" if val in (None, "") else str(val), BODY)])
    t = Table(data, colWidths=[4.6 * inch, 2.4 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9d2dd")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def build_scat6_pdf(assessment: Dict, scores: Dict) -> bytes:
    a, s = assessment, scores
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch,
                            bottomMargin=0.6 * inch, leftMargin=0.7 * inch,
                            rightMargin=0.7 * inch,
                            title=f"SCAT6 — {a.get('athlete_name', '')}")
    story = []

    story.append(Paragraph("SCAT6™ — Sport Concussion Assessment Tool", H1))
    story.append(Paragraph(
        "For Adolescents (13 years +) &amp; Adults — for use by Health Care Professionals only. "
        "Scoring on the SCAT6 should not be used as a stand-alone method to diagnose concussion.",
        SMALL))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Athlete Information", H2))
    atype = (a.get("assessment_type") or "").replace("_", "-").title()
    story.append(_kv_table([
        ("Athlete", a.get("athlete_name")), ("Juvonno ID", a.get("athlete_id")),
        ("Date of birth", a.get("dob")), ("Sex", a.get("sex")),
        ("Dominant hand", a.get("dominant_hand")), ("Sport / Team", a.get("sport_team")),
        ("Examination date", a.get("date_of_examination")), ("Assessment type", atype),
        ("Date of injury", a.get("date_of_injury")), ("Time of injury", a.get("time_of_injury")),
        ("Examiner", a.get("examiner")), ("", ""),
    ]))

    story.append(Paragraph("Concussion History", H2))
    story.append(_kv_table([
        ("Past concussions", a.get("num_past_concussions")),
        ("Most recent", a.get("most_recent_concussion")),
        ("Recovery time (days)", a.get("recovery_days")),
        ("Primary symptoms", a.get("primary_symptoms")),
    ]))

    bg = a.get("background", {}) or {}
    if bg:
        story.append(Paragraph("Athlete Background", H2))
        story.append(_kv_table([(label, bg.get(key, "")) for key, label in S.BACKGROUND_ITEMS]
                               + [("Current medications", a.get("medications", ""))]))

    # Immediate assessment
    red = a.get("red_flags", [])
    red_names = [S.RED_FLAGS[i] for i, v in enumerate(red) if v] if isinstance(red, list) else []
    obs = a.get("observable_signs", [])
    obs_names = [S.OBSERVABLE_SIGNS[i] for i, v in enumerate(obs) if v == "Y"] \
        if isinstance(obs, list) else []
    story.append(Paragraph("Immediate Assessment / Neuro Screen", H2))
    story.append(_kv_table([
        ("Red flags", "; ".join(red_names) if red_names else "None reported"),
        ("Observable signs", "; ".join(obs_names) if obs_names else "None"),
        ("GCS (E+V+M)", s.get("gcs_total")),
        ("Cervical spine normal", a.get("cervical_normal")),
        ("Coordination / ocular normal", a.get("coord_ocular_normal")),
        ("Maddocks score (of 5)", s.get("maddocks")),
    ], ncols=1))

    # Symptom evaluation
    story.append(Paragraph("Symptom Evaluation", H2))
    ratings = a.get("symptom_ratings", [])
    endorsed = [f"{S.SYMPTOMS[i]} ({int(v)})" for i, v in enumerate(ratings)
                if v not in (None, "", 0) and i < len(S.SYMPTOMS)]
    story.append(_kv_table([
        ("Symptom number", f"{s.get('symptom_number', '—')} of 22"),
        ("Symptom severity", f"{s.get('symptom_severity', '—')} of 132"),
        ("Worse with physical activity", a.get("worse_physical")),
        ("Worse with mental activity", a.get("worse_mental")),
        ("Percent of normal", a.get("percent_normal")),
        ("If not 100%, why", a.get("percent_normal_why")),
    ]))
    if endorsed:
        story.append(Spacer(1, 3))
        story.append(Paragraph("<b>Endorsed symptoms (rating):</b> " + "; ".join(endorsed), BODY))

    # Cognitive screening
    story.append(Paragraph("Cognitive Screening (SAC)", H2))
    story.append(_score_table([
        ("Orientation (of 5)", s.get("orientation")),
        (f"Immediate memory — list {a.get('word_list', '')} "
         f"(T1 {s.get('im_trial1', '—')}, T2 {s.get('im_trial2', '—')}, T3 {s.get('im_trial3', '—')}) (of 30)",
         s.get("immediate_memory")),
        (f"Concentration — digits list {a.get('digit_list', '')} (of 4) + months (of 1) (of 5)",
         s.get("concentration")),
        ("Delayed recall (of 10)", s.get("delayed_recall")),
        ("Cognitive total (of 50)", s.get("cognitive_total")),
    ]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(
        f"Months in reverse: {a.get('months_seconds', '—')} s, "
        f"{a.get('months_errors', '—')} error(s) → {s.get('months_score', 0)} point.", SMALL))

    # Balance
    story.append(Paragraph("Coordination &amp; Balance Examination", H2))
    story.append(_score_table([
        ("mBESS — Double leg errors (of 10)", s.get("mbess_double")),
        ("mBESS — Tandem errors (of 10)", s.get("mbess_tandem")),
        ("mBESS — Single leg errors (of 10)", s.get("mbess_single")),
        ("mBESS — Total errors (of 30)", s.get("mbess_total")),
        ("mBESS on foam — Total errors (of 30)", s.get("mbess_foam_total")),
        ("Timed tandem gait — average (s)", s.get("tg_average")),
        ("Timed tandem gait — fastest (s)", s.get("tg_fastest")),
        ("Dual task — fastest (s)", s.get("dual_task_fastest")),
    ]))
    foot = a.get("foot_tested")
    if foot:
        story.append(Spacer(1, 2))
        story.append(Paragraph(
            f"Foot tested: {foot}. Surface: {a.get('test_surface', '—')}. "
            f"Footwear: {a.get('footwear', '—')}.", SMALL))

    # Decision
    story.append(Paragraph("Decision", H2))
    story.append(_kv_table([
        ("Neurological exam", a.get("neuro_exam")),
        ("Different from usual self", a.get("different_from_usual")),
        ("Concussion diagnosed?", a.get("concussion_diagnosed")),
    ], ncols=1))
    notes = (a.get("notes") or "").strip()
    if notes:
        story.append(Paragraph("Additional Clinical Notes", H2))
        story.append(Paragraph(notes.replace("\n", "<br/>"), BODY))

    story.append(Paragraph("Health Care Professional Attestation", H2))
    story.append(_kv_table([
        ("Name", a.get("examiner")),
        ("Title / Speciality", a.get("examiner_title")),
        ("Registration / license #", a.get("examiner_license")),
        ("Date", a.get("date_of_examination")),
    ]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Generated by the CSI Pacific SCAT6 intake tool. SCAT6 © Concussion in Sport Group — "
        "Echemendia RJ, et al. Br J Sports Med 2023;57:622–631. An athlete can score within "
        "normal limits on the SCAT6 and still have a concussion.", SMALL))

    doc.build([KeepTogether(x) if isinstance(x, Table) else x for x in story])
    return buf.getvalue()
