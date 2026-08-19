# SCAT6 Intake Tool (Juvonno-connected)

A Dash app for practitioners to perform SCAT6 (Sport Concussion Assessment Tool 6)
intakes on athletes registered in Juvonno.

* Same OAuth2 login flow as other CSI Pacific dash apps (apps.csipacific.ca)
* Branch → Group → Athlete cascade loads registered athletes from Juvonno
* Full SCAT6 form: immediate assessment (observable signs, GCS, cervical spine,
  coordination/ocular, Maddocks), symptom scale, cognitive screening (orientation,
  immediate memory, concentration), mBESS (+ optional foam), timed tandem gait +
  dual task, delayed recall, decision & attestation — with live auto-scoring
* Juvonno is the source of truth: each saved assessment uploads one timestamped
  PDF (`SCAT6_<date>_<time>_athlete<id>.pdf`) with the full assessment data
  embedded in the PDF metadata and a "Go to SCAT6 Tool" button linking back to
  the app. No CSVs are stored in Juvonno.
* History: the History tab pulls the athlete's SCAT6 PDFs from Juvonno and
  scrapes them (embedded JSON, with a text-parse fallback for older PDFs) to
  rebuild the assessment table, the serial-comparison view (SCAT6 Step 6 domain
  table), and an exportable longitudinal CSV
* Offline resilience: every assessment saves to local SQLite (`scat6.db`) first
  and is queued if Juvonno is unreachable — retried automatically every 2 minutes
  or via "Sync now"; form inputs persist in the browser (localStorage) so a
  refresh or dropped connection mid-intake loses nothing
* UI: dash-mantine-components + dash-iconify (bootstrap kept only for the
  original navbar/footer)

## Files

| File | Purpose |
|---|---|
| `app.py` | Dash app: auth, athlete picker, SCAT6 form, history & documents |
| `scat6.py` | SCAT6 form content, scoring functions, CSV flattening |
| `scat6_store.py` | SQLite persistence of assessments |
| `scat6_pdf.py` | PDF generation (reportlab) |
| `juvonno_api.py` | Juvonno API client: branches, athletes, document push/pull |
| `layout/` | Navbar / footer components (unchanged) |

## Requirements

Create a `.env` file with:

```
CLIENT_ID=...            # OAuth app on apps.csipacific.ca (/o/applications/)
CLIENT_SECRET=...
SITE_URL=https://apps.csipacific.ca
APP_URL=...              # deployed URL of this app
JUV_API_KEY=...          # Juvonno API key
JUV_API_BASE=https://csipacific.juvonno.com/api   # optional, this is the default
SCAT6_TOOL_URL=...       # optional, link shown on generated PDFs (defaults to the app URL)
```

In live deployment set these in the platform's secrets mechanism
(e.g. Posit Connect Cloud → app settings → Variables).

Install and run:

```
pip install -r requirements.txt
python app.py
```

## Notes

* SCAT6 © Concussion in Sport Group — Echemendia RJ, et al. Br J Sports Med
  2023;57:622–631. For use by Health Care Professionals; scoring should not be
  used as a stand-alone method to diagnose concussion.
* Juvonno's public API has no document UPDATE/DELETE endpoints, so the PDF-per-
  assessment model (immutable documents) is the natural fit; history is
  reconstructed by reading those PDFs back.
