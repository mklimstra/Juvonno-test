# training_dashboard.py — dashboard content + callbacks (comments removed, calendar open, month abbr, focus filter)
from __future__ import annotations
import os, sqlite3, requests, functools, traceback, re
from datetime import datetime
from typing import Dict, List, Union, Iterable, Tuple, Optional

import numpy as np
import pandas as pd

import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, dash_table, Input, Output, State, no_update

try:
    import plotly_calplot as pc
    PLOTLYCAL_AVAILABLE = True
except ImportError:
    PLOTLYCAL_AVAILABLE = False

import plotly.graph_objects as go

# ────────── API config ──────────
API_KEY = os.getenv("JUV_API_KEY")
BASE    = os.getenv("JUV_API_BASE", "https://csipacific.juvonno.com/api").rstrip("/")
HEADERS = {"accept": "application/json"}

def _require_api_key():
    if not API_KEY:
        raise RuntimeError(
            "Missing JUV_API_KEY. Set it in your deployment environment (e.g., Posit Connect → Variables)."
        )

def _get(path: str, **params):
    request_headers = dict(HEADERS)
    if API_KEY:
        request_headers.setdefault("x-api-key", API_KEY)
    params.setdefault("api_key", API_KEY)
    r = requests.get(f"{BASE}/{path.lstrip('/')}", params=params, headers=request_headers, timeout=20)
    r.raise_for_status()
    return r.json()

def _extract_rows(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("list", "results", "data", "customers", "items", "branches", "clinics", "locations", "sites"):
        block = payload.get(key)
        if isinstance(block, list):
            return block
        if isinstance(block, dict):
            for nested_key in ("list", "results", "data", "items", "branches", "clinics", "locations", "sites"):
                nested_block = block.get(nested_key)
                if isinstance(nested_block, list):
                    return nested_block

    for value in payload.values():
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            return value
    return []

def _extract_total(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("count", "total", "total_count", "recordsTotal"):
        val = payload.get(key)
        try:
            if val is not None:
                return int(val)
        except (TypeError, ValueError):
            continue
    return None

def _extract_has_more(payload) -> Optional[bool]:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("next"), str):
        return bool(payload.get("next"))
    if isinstance(payload.get("has_more"), bool):
        return payload.get("has_more")
    if isinstance(payload.get("hasNext"), bool):
        return payload.get("hasNext")
    return None

def _first_non_empty(*vals):
    for val in vals:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def _branch_id_from_obj(obj: Dict) -> Optional[int]:
    if not isinstance(obj, dict):
        return None
    candidates = [
        obj.get("branch_id"), obj.get("branchId"), obj.get("clinic_id"),
        obj.get("location_id"), obj.get("site_id")
    ]
    branch_obj = obj.get("branch")
    if isinstance(branch_obj, dict):
        candidates.extend([branch_obj.get("id"), branch_obj.get("branch_id")])
    clinic_obj = obj.get("clinic")
    if isinstance(clinic_obj, dict):
        candidates.extend([clinic_obj.get("id"), clinic_obj.get("clinic_id")])
    location_obj = obj.get("location")
    if isinstance(location_obj, dict):
        candidates.extend([location_obj.get("id"), location_obj.get("location_id")])
    for value in candidates:
        try:
            if value is None or value == "":
                continue
            return int(value)
        except (TypeError, ValueError):
            continue
    return None

def _branch_id_from_branch_row(obj: Dict) -> Optional[int]:
    if not isinstance(obj, dict):
        return None

    bid = _branch_id_from_obj(obj)
    if bid is not None:
        return bid

    try:
        val = obj.get("id")
        if val is not None and val != "":
            return int(val)
    except (TypeError, ValueError):
        pass
    return None

def _branch_name_from_obj(obj: Dict) -> str:
    if not isinstance(obj, dict):
        return ""

    candidates: List[str] = []
    for key in ("name", "branch_name", "clinic_name", "location_name", "site_name", "title", "label", "code"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append(val.strip())

    branch_obj = obj.get("branch")
    if isinstance(branch_obj, dict):
        for key in ("name", "branch_name", "title", "label", "code"):
            val = branch_obj.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())

    clinic_obj = obj.get("clinic")
    if isinstance(clinic_obj, dict):
        for key in ("name", "clinic_name", "title", "label", "code"):
            val = clinic_obj.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())

    location_obj = obj.get("location")
    if isinstance(location_obj, dict):
        for key in ("name", "location_name", "title", "label", "code"):
            val = location_obj.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())

    return candidates[0] if candidates else ""

def _branch_name_from_customer_obj(obj: Dict) -> str:
    if not isinstance(obj, dict):
        return ""

    for container_key, keys in (
        ("branch", ("name", "branch_name", "title", "label", "code")),
        ("clinic", ("name", "clinic_name", "title", "label", "code")),
        ("location", ("name", "location_name", "title", "label", "code")),
    ):
        container = obj.get(container_key)
        if isinstance(container, dict):
            for key in keys:
                val = container.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    for key in ("branch_name", "clinic_name", "location_name", "site_name"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return ""

def _group_names_from_customer(cust: Dict) -> List[str]:
    names: List[str] = []
    
    # Helper to extract groups from a container (clinic, location, or top-level)
    def _extract_from_container(container: Dict, search_keys: Iterable[str]):
        for src_key in search_keys:
            src = container.get(src_key)
            if isinstance(src, list):
                for it in src:
                    if isinstance(it, str):
                        for token in re.split(r"[,;|]", it):
                            token_n = _norm(token)
                            if token_n:
                                names.append(token_n)
                    elif isinstance(it, dict):
                        raw = _first_non_empty(
                            it.get("name"), it.get("label"), it.get("title"), it.get("group_name")
                        )
                        if raw:
                            names.append(_norm(raw))
                        nested_group = it.get("group")
                        if isinstance(nested_group, dict):
                            raw_nested = _first_non_empty(
                                nested_group.get("name"),
                                nested_group.get("label"),
                                nested_group.get("title"),
                                nested_group.get("group_name"),
                            )
                            if raw_nested:
                                names.append(_norm(raw_nested))
            elif isinstance(src, dict):
                raw = _first_non_empty(src.get("name"), src.get("label"), src.get("title"), src.get("group_name"))
                if raw:
                    names.append(_norm(raw))
                nested_group = src.get("group")
                if isinstance(nested_group, dict):
                    raw_nested = _first_non_empty(
                        nested_group.get("name"),
                        nested_group.get("label"),
                        nested_group.get("title"),
                        nested_group.get("group_name"),
                    )
                    if raw_nested:
                        names.append(_norm(raw_nested))
            elif isinstance(src, str):
                for token in re.split(r"[,;|]", src):
                    token_n = _norm(token)
                    if token_n:
                        names.append(token_n)
    
    group_keys = ("groups", "group", "customer_groups", "patient_groups", "tags")
    
    # Extract from top-level first
    _extract_from_container(cust, group_keys)
    
    # Extract from nested clinic/location/branch containers (clinic-aware)
    for container_key in ("clinic", "location", "branch"):
        container = cust.get(container_key)
        if isinstance(container, dict):
            _extract_from_container(container, group_keys)
    
    return sorted({n for n in names if n})

def _fetch_all_rows(endpoint: str, base_params: Dict, page_size: int = 100, max_pages: int = 500) -> List[Dict]:
    rows_out: List[Dict] = []
    seen_ids: set[int] = set()

    for page_index in range(max_pages):
        params = dict(base_params)
        params.update({
            "page": page_index + 1,
            "count": page_size,
            "limit": page_size,
            "offset": page_index * page_size,
        })
        js = _get(endpoint, **params)
        rows = [row for row in _extract_rows(js) if isinstance(row, dict)]
        if not rows:
            break

        new_added = 0
        for row in rows:
            rid = row.get("id")
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                rid_int = None

            if rid_int is None:
                rows_out.append(row)
                new_added += 1
                continue

            if rid_int in seen_ids:
                continue
            seen_ids.add(rid_int)
            rows_out.append(row)
            new_added += 1

        total = _extract_total(js)
        has_more = _extract_has_more(js)
        if total is not None and len(seen_ids) >= total:
            break
        if has_more is False:
            break
        if new_added == 0 or len(rows) < page_size:
            break

    return rows_out

def _customer_detail_safe(customer_id: int) -> Dict:
    try:
        js = _get(f"customers/{int(customer_id)}", include="full")
        if isinstance(js, dict):
            return js.get("customer", js)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400, 401, 403, 404):
            return {}
        raise
    except Exception:
        return {}
    return {}

def enrich_customers(customers: Dict[int, Dict]) -> Dict[int, Dict]:
    out: Dict[int, Dict] = {}
    for cid, customer in customers.items():
        base = dict(customer or {})
        needs_detail = (_branch_id_from_obj(base) is None) or (not _group_names_from_customer(base))
        if needs_detail:
            detail = _customer_detail_safe(int(cid))
            if isinstance(detail, dict) and detail:
                merged = dict(base)
                merged.update(detail)
                base = merged
        out[int(cid)] = base
    return out

# ────────── SQLite (kept for DB existence; not used here) ──────────
DB_PATH = os.path.join(os.path.dirname(__file__), "comments.db")

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_label TEXT,
            date TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def db_list_comments(customer_ids: Iterable[int] | None) -> List[Dict]:
    conn = _db(); cur = conn.cursor()
    if customer_ids:
        vals = [int(x) for x in customer_ids]
        q = ",".join("?" for _ in vals)
        cur.execute(f"""
          SELECT date, comment, customer_label, customer_id
          FROM comments
          WHERE customer_id IN ({q})
          ORDER BY date ASC, id ASC
        """, vals)
    else:
        cur.execute("SELECT date, comment, customer_label, customer_id FROM comments ORDER BY date ASC, id ASC")
    rows = cur.fetchall(); conn.close()
    return [{"Date": r[0], "Comment": r[1], "Athlete": r[2], "Athlete ID": r[3]} for r in rows]

# ────────── Customers / groups ──────────
def _norm(s: str) -> str:
    return (s or "").strip().lower()

def fetch_customers_full() -> Dict[int, Dict]:
    out: List[Dict] = []
    attempts = [
        ("customers/list", {"include": "groups,clinic,location"}),
        ("customers/list", {"include": "groups"}),
        ("customers", {"include": "groups,clinic,location"}),
        ("customers", {"include": "groups"}),
        ("customers/list", {"include": "groups,clinic,location", "status": "ACTIVE"}),
        ("customers/list", {"include": "groups", "status": "ACTIVE"}),
        ("customers", {"include": "groups,clinic,location", "status": "ACTIVE"}),
        ("customers", {"include": "groups", "status": "ACTIVE"}),
        ("customers/list", {}),
        ("customers", {}),
    ]

    for endpoint, base_params in attempts:
        collected: List[Dict] = []
        try:
            collected = _fetch_all_rows(endpoint, base_params, page_size=100)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (400, 401, 403, 404):
                collected = []
            else:
                raise

        if collected:
            out = collected
            break

    return {int(c["id"]): c for c in out if isinstance(c, dict) and c.get("id") is not None}

def fetch_available_branches(customers: Dict[int, Dict]) -> List[int]:
    branch_ids: set[int] = set()

    for endpoint in ("branches/list", "branches"):
        page = 1
        while True:
            try:
                js = _get(endpoint, page=page, count=100, limit=100)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (400, 401, 403, 404):
                    break
                raise

            rows = _extract_rows(js)
            if not rows:
                break
            for row in rows:
                bid = _branch_id_from_branch_row(row)
                if bid is not None:
                    branch_ids.add(bid)
            if len(rows) < 100:
                break
            page += 1

    for cust in customers.values():
        bid = _branch_id_from_obj(cust)
        if bid is not None:
            branch_ids.add(bid)

    return sorted(branch_ids)

def fetch_branch_name_map(customers: Dict[int, Dict]) -> Dict[int, str]:
    names: Dict[int, str] = {}

    for customer in customers.values():
        bid = _branch_id_from_obj(customer)
        bname = _branch_name_from_customer_obj(customer)
        if bid is not None and bname:
            names[bid] = bname

    for endpoint in ("branches/list", "branches"):
        page = 1
        while True:
            try:
                js = _get(endpoint, page=page, count=100, limit=100)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (400, 401, 403, 404):
                    break
                raise

            rows = _extract_rows(js)
            if not rows:
                break

            for row in rows:
                if not isinstance(row, dict):
                    continue
                bid = _branch_id_from_branch_row(row)
                bname = _branch_name_from_obj(row)
                if bid is not None and bname:
                    names[bid] = bname

            if len(rows) < 100:
                break
            page += 1

    return names

# NEW: detail fetch (for DOB, phone, etc.) with cache
@functools.lru_cache(maxsize=4096)
def fetch_customer_detail(customer_id: int) -> Dict:
    try:
        js = _get(f"customers/{int(customer_id)}", include="full,groups,clinic,location")
        if isinstance(js, dict):
            return js.get("customer", js)
    except Exception:
        pass
    return {}

_require_api_key()
CUSTOMERS = enrich_customers(fetch_customers_full())

def groups_of(cust: Dict) -> List[str]:
    return _group_names_from_customer(cust)

CID_TO_GROUPS  = {cid: groups_of(c) for cid, c in CUSTOMERS.items()}
CID_TO_BRANCH  = {cid: _branch_id_from_obj(c) for cid, c in CUSTOMERS.items()}
BRANCH_TO_CUSTOMER_IDS: Dict[int, set[int]] = {}
for cid, bid in CID_TO_BRANCH.items():
    if bid is not None:
        BRANCH_TO_CUSTOMER_IDS.setdefault(int(bid), set()).add(int(cid))

BRANCH_TO_GROUPS = {
    bid: sorted({g for cid in cids for g in CID_TO_GROUPS.get(cid, [])})
    for bid, cids in BRANCH_TO_CUSTOMER_IDS.items()
}

def _customer_branch(cid: int, cust: Optional[Dict] = None) -> Optional[int]:
    bid = CID_TO_BRANCH.get(int(cid))
    if bid is not None:
        return bid
    if isinstance(cust, dict):
        return _branch_id_from_obj(cust)
    return None

def _customer_groups(cid: int, cust: Optional[Dict] = None) -> List[str]:
    vals = CID_TO_GROUPS.get(int(cid), [])
    if vals:
        return vals
    if isinstance(cust, dict):
        return _group_names_from_customer(cust)
    return []

def groups_for_branches(branch_values) -> List[str]:
    selected: set[int] = set()
    for value in (branch_values or []):
        try:
            if value is None or value == "":
                continue
            selected.add(int(value))
        except (TypeError, ValueError):
            continue

    all_groups: set[str] = set()
    for cid, cust in CUSTOMERS.items():
        cgroups = _customer_groups(int(cid), cust)
        if not cgroups:
            continue

        if not selected:
            all_groups.update(cgroups)
            continue

        cbid = _customer_branch(int(cid), cust)
        if cbid in selected:
            all_groups.update(cgroups)

    return sorted(all_groups)

def fetch_groups_for_branches_dynamic(branch_ids: List[int]) -> List[str]:
    """Dynamic fetch of groups for selected branches, hitting API if needed."""
    if not branch_ids:
        return []
    
    all_groups: set[str] = set()
    targets = {int(b) for b in branch_ids}
    
    # First try cached data from customers
    for cid, cust in CUSTOMERS.items():
        cbid = _customer_branch(int(cid), cust)
        if cbid in targets:
            cgroups = _customer_groups(int(cid), cust)
            all_groups.update(cgroups)
    
    # Additionally, fetch customers directly for each selected clinic/branch
    # This bypasses the initial cache and gets fresh data
    for bid in targets:
        try:
            # Try fetching customers for this clinic directly
            for endpoint_template in (f"clinics/{bid}", f"branches/{bid}", f"locations/{bid}"):
                try:
                    # Try to get clinic/branch details with customer list
                    js = _get(endpoint_template, include="customers,groups")
                    
                    # Extract clinic/branch object
                    for wrap_key in ("clinic", "branch", "location"):
                        obj = js.get(wrap_key)
                        if isinstance(obj, dict):
                            # Get groups from the clinic/branch itself
                            grps = _group_names_from_customer(obj)
                            all_groups.update(grps)
                            
                            # Get customers list if available
                            for cust_key in ("customers", "customer"):
                                cust_data = obj.get(cust_key)
                                if isinstance(cust_data, list):
                                    for c in cust_data:
                                        if isinstance(c, dict):
                                            grps = _group_names_from_customer(c)
                                            all_groups.update(grps)
                    
                    # Also try top-level extraction
                    if isinstance(js, dict):
                        grps = _group_names_from_customer(js)
                        all_groups.update(grps)
                except requests.HTTPError:
                    pass
            
            # Try fetching customer list filtered by clinic/branch
            for endpoint in ("customers/list", "customers"):
                try:
                    # Try with clinic_id or branch_id filter
                    js = _get(endpoint, clinic_id=bid, include="groups", count=100)
                    rows = _extract_rows(js)
                    for row in rows:
                        if isinstance(row, dict):
                            grps = _group_names_from_customer(row)
                            all_groups.update(grps)
                except requests.HTTPError:
                    pass
                
                try:
                    # Try with branch_id filter
                    js = _get(endpoint, branch_id=bid, include="groups", count=100)
                    rows = _extract_rows(js)
                    for row in rows:
                        if isinstance(row, dict):
                            grps = _group_names_from_customer(row)
                            all_groups.update(grps)
                except requests.HTTPError:
                    pass
                    
                try:
                    # Try with location_id filter
                    js = _get(endpoint, location_id=bid, include="groups", count=100)
                    rows = _extract_rows(js)
                    for row in rows:
                        if isinstance(row, dict):
                            grps = _group_names_from_customer(row)
                            all_groups.update(grps)
                except requests.HTTPError:
                    pass
        
        except Exception:
            pass
    
    return sorted(all_groups)


BRANCH_NAME_BY_ID = fetch_branch_name_map(CUSTOMERS)
BRANCH_IDS     = sorted(set(fetch_available_branches(CUSTOMERS)) | set(BRANCH_NAME_BY_ID.keys()))
BRANCH_OPTS    = sorted(
    [{"label": BRANCH_NAME_BY_ID.get(bid, f"Branch {bid}"), "value": bid} for bid in BRANCH_IDS],
    key=lambda o: (str(o.get("label", "")).casefold(), int(o.get("value", 0)))
)
# Get all groups from all branches (used for initial display)
ALL_GROUPS     = sorted(fetch_groups_for_branches_dynamic(BRANCH_IDS) or {g for lst in CID_TO_GROUPS.values() for g in lst})
GROUP_OPTS     = [{"label": g.title(), "value": g} for g in ALL_GROUPS]

# ────────── Appointments (all known branches) ──────────
def fetch_branch_appts(branch=1) -> List[Dict]:
    rows, page = [], 1
    while True:
        js = _get(f"appointments/list/{branch}", start_date="2000-01-01", status="all", page=page, count=100)
        block = _extract_rows(js)
        if not block: break
        rows.extend(block)
        if len(block) < 100: break
        page += 1
    return rows

def fetch_all_branch_appts(branch_ids: List[int]) -> List[Dict]:
    all_appts: List[Dict] = []
    targets = branch_ids or [1]
    for bid in targets:
        try:
            all_appts.extend(fetch_branch_appts(int(bid)))
        except requests.HTTPError:
            continue
    return all_appts

BRANCH_APPTS     = fetch_all_branch_appts(BRANCH_IDS)
CID_TO_APPTS: Dict[int, List[Dict]] = {}
for ap in BRANCH_APPTS:
    cust = ap.get("customer", {})
    if isinstance(cust, dict) and cust.get("id"):
        cid = int(cust["id"])
        CID_TO_APPTS.setdefault(cid, []).append(ap)
        if CID_TO_BRANCH.get(cid) is None:
            ap_branch = _branch_id_from_obj(ap)
            if ap_branch is not None:
                CID_TO_BRANCH[cid] = ap_branch

if not BRANCH_IDS:
    BRANCH_IDS = sorted({b for b in CID_TO_BRANCH.values() if b is not None})
    BRANCH_OPTS = sorted(
        [{"label": BRANCH_NAME_BY_ID.get(bid, f"Branch {bid}"), "value": bid} for bid in BRANCH_IDS],
        key=lambda o: (str(o.get("label", "")).casefold(), int(o.get("value", 0)))
    )

# ────────── Encounters / Training Status ──────────
FLAGS = [{}, {"include": "fields"}, {"include": "answers"}, {"full": 1}]

@functools.lru_cache(maxsize=1024)
def fetch_encounter(eid: int) -> Dict:
    for root in (f"encounters/{eid}", f"encounters/charts/{eid}", f"encounters/intakes/{eid}"):
        for f in FLAGS:
            try:
                js = _get(root, **f)
                return js.get("encounter", js) if isinstance(js, dict) else js
            except requests.HTTPError as e:
                if e.response.status_code in (400, 404): continue
                raise
    return {}

def extract_training_status(enc_payload: Union[Dict, List]) -> str:
    valid = {
        "Full participation without injury/illness/other health problems",
        "Full participation with injury/illness/other health problems",
        "Reduced participation with injury/illness/other health problems",
        "No participation due to injury/illness/other health problems",
        "No participation unrelated to injury/illness/other health problems",
    }
    stack: List[Union[Dict, List]] = [enc_payload] if isinstance(enc_payload, (dict, list)) else []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node_id   = str(node.get("id", "")).lower()
            raw_val   = str(node.get("value", "")).strip()
            node_val  = " ".join(raw_val.split())
            node_name = " ".join((node.get("name") or node.get("label") or node.get("title") or "").split()).lower()
            if node_id == "id_select_2" and node_val in valid: return node_val
            if "training status" in node_name and node_val in valid: return node_val
            for v in node.values():
                if isinstance(v, (dict, list)): stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return ""

def _encounter_sort_dt(enc: Dict) -> pd.Timestamp:
    if not isinstance(enc, dict):
        return pd.Timestamp.min
    for key in ("date", "encounter_date", "modification_date", "modified_at", "creation_date", "created_at"):
        raw = enc.get(key)
        if raw:
            ts = pd.to_datetime(raw, errors="coerce")
            if not pd.isna(ts):
                return ts
    return pd.Timestamp.min

def latest_training_status_for_appt(aid: int) -> str:
    eids = encounter_ids_for_appt(aid)
    if not eids:
        return ""

    candidates: List[Tuple[pd.Timestamp, int, str]] = []
    for eid in set(int(x) for x in eids):
        enc = fetch_encounter(eid)
        status = extract_training_status(enc)
        if not status:
            continue
        candidates.append((_encounter_sort_dt(enc), int(eid), status))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]

@functools.lru_cache(maxsize=2048)
def encounter_ids_for_appt(aid: int) -> List[int]:
    try:
        js = _get("encounters/appointment", appointment_id=aid)
    except requests.HTTPError:
        return []
    ids: List[int] = []
    for key in ("charts", "intakes"):
        arr = js.get(key)
        if isinstance(arr, list):
            for val in arr:
                try: ids.append(int(val))
                except (ValueError, TypeError): pass
    return ids

# ── Appointment-level complaints ──
@functools.lru_cache(maxsize=4096)
def list_complaints_for_appt(aid: int) -> List[Dict]:
    try:
        js = _get(f"appointments/{aid}/complaints")
    except requests.HTTPError:
        return []
    if isinstance(js, list): return js
    if isinstance(js, dict) and isinstance(js.get("list"), list): return js["list"]
    return []

# ── Complaint detail for enrichment (fills Onset/Priority/Status if missing)
@functools.lru_cache(maxsize=4096)
def fetch_complaint_detail(complaint_id: int) -> Dict:
    try:
        js = _get(f"complaints/{int(complaint_id)}", include="full")
        if isinstance(js, dict):
            # API may wrap
            if "complaint" in js and isinstance(js["complaint"], dict):
                return js["complaint"]
            return js
    except Exception:
        pass
    return {}

# ── Athlete-level complaints (merge) ──────────
def _fmt_date(val) -> str:
    if not val: return ""
    try: return pd.to_datetime(str(val)).strftime("%Y-%m-%d")
    except Exception: return str(val)

def _extract_name(rec: Dict) -> str:
    for k in ("name", "title", "problem", "injury", "body_part", "complaint"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    return ""

def _norm_complaint_fields(rec: Dict) -> Dict:
    title    = _extract_name(rec)
    onset    = (rec.get("onset_date") or rec.get("onsetDate") or rec.get("onset") or
                rec.get("start_date") or rec.get("date") or rec.get("injury_onset") or "")
    priority = (rec.get("priority") or rec.get("priority_name") or rec.get("priorityName") or
                rec.get("priority_level") or "")
    status   = (rec.get("status") or rec.get("status_name") or rec.get("statusName") or
                rec.get("state") or rec.get("complaint_status") or "")
    cid      = rec.get("id") or rec.get("complaint_id") or rec.get("complaintId") or None

    # Enrich if sparse and we have an id
    if (not onset or not priority or not status) and cid:
        detail = fetch_complaint_detail(int(cid))
        if isinstance(detail, dict):
            onset2 = (detail.get("onset_date") or detail.get("onsetDate") or detail.get("start_date") or
                      detail.get("date") or detail.get("injury_onset") or "")
            priority2 = (detail.get("priority") or detail.get("priority_name") or detail.get("priorityName") or
                         detail.get("priority_level") or "")
            status2   = (detail.get("status") or detail.get("status_name") or detail.get("statusName") or
                         detail.get("state") or detail.get("complaint_status") or "")
            onset    = onset or onset2
            priority = str(priority or priority2).strip()
            status   = str(status or status2).strip()

    return {"Id": cid, "Title": title, "Onset": _fmt_date(onset),
            "Priority": str(priority).strip(), "Status": (str(status).strip() or "—")}

@functools.lru_cache(maxsize=512)
def fetch_customer_complaints(customer_id: int) -> List[Dict]:
    out: List[Dict] = []

    # 1) Customer-level
    try:
        js = _get(f"customers/{customer_id}/complaints", include="full", page=1, count=100)
        block = js.get("list", js)
        if isinstance(block, list): out.extend(block)
        page = 2
        while True:
            js2 = _get(f"customers/{customer_id}/complaints", include="full", page=page, count=100)
            blk = js2.get("list", js2)
            if not isinstance(blk, list) or not blk: break
            out.extend(blk)
            if len(blk) < 100: break
            page += 1
    except requests.HTTPError:
        pass

    # 2) Global search by customer_id
    try:
        page = 1
        while True:
            js = _get("complaints/list", customer_id=customer_id, page=page, count=100)
            block = js.get("list", js)
            if not isinstance(block, list) or not block: break
            out.extend(block)
            if len(block) < 100: break
            page += 1
    except requests.HTTPError:
        pass

    # 3) Appointment-level + inline
    for ap in CID_TO_APPTS.get(customer_id, []):
        for rec in list_complaints_for_appt(ap.get("id")):
            out.append(rec)
        comp_inline = ap.get("complaint")
        if isinstance(comp_inline, dict):
            name = _extract_name(comp_inline)
            if name: out.append({"name": name, "id": comp_inline.get("id")})

    # Normalize + dedupe
    normed = [_norm_complaint_fields(r) for r in out if isinstance(r, dict)]
    dedup: Dict[Tuple, Dict] = {}
    for r in normed:
        key = (r.get("Id") or (r.get("Title") or "").casefold(),)
        if key in dedup:
            prev = dedup[key]
            for f in ("Priority", "Status", "Onset", "Title"):
                if (not prev.get(f)) and r.get(f): prev[f] = r[f]
        else:
            dedup[key] = r

    def _sort_key(d):
        try: return (0, pd.to_datetime(d["Onset"]))
        except Exception: return (1, pd.Timestamp.min)

    return sorted(dedup.values(), key=_sort_key, reverse=True)

# ────────── Pastel palette (table + calendar) ──────────
STATUS_ORDER = [
    "Full participation without injury/illness/other health problems",
    "Full participation with injury/illness/other health problems",
    "Reduced participation with injury/illness/other health problems",
    "No participation due to injury/illness/other health problems",
    "No participation unrelated to injury/illness/other health problems",
]
PASTEL_COLOR = {
    STATUS_ORDER[0]: "#BDE7BD",
    STATUS_ORDER[1]: "#D6F2C6",
    STATUS_ORDER[2]: "#FFD9A8",
    STATUS_ORDER[3]: "#F5B1B1",
    STATUS_ORDER[4]: "#D8C6F0",
}
COLOR_LIST = [PASTEL_COLOR[s] for s in STATUS_ORDER]
STATUS_CODE = {s: i for i, s in enumerate(STATUS_ORDER)}

def tidy_date_str(raw) -> str:
    if isinstance(raw, dict): raw = raw.get("start", "")
    raw = raw or ""
    return raw.split("T", 1)[0] if isinstance(raw, str) else str(raw)

def dot_html(hex_color: str, size: int = 10, mr: int = 8) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )

def discrete_colorscale_from_hexes(hexes: List[str]) -> list:
    n = len(hexes)
    if n == 0: return []
    if n == 1: return [[0.0, hexes[0]], [1.0, hexes[0]]]
    eps = 1e-6
    stops = [[0.0, hexes[0]]]
    for i in range(1, n):
        p = i / (n - 1)
        stops.append([max(p - eps, 0.0), hexes[i-1]])
        stops.append([p, hexes[i]])
    stops[-1][0] = 1.0
    return stops

# ────────── Clickable card headers (plus/minus) ──────────
LIGHT_GREY = "#f2f3f5"

def clickable_header(title: str, click_id: str, symbol_id: str, header_id: str):
    return dbc.CardHeader(
        html.Div(
            [html.Span(id=symbol_id, children="−", className="me-2"),
             html.Span(title, className="fw-semibold")],
            id=click_id, n_clicks=0,
            style={"cursor":"pointer","userSelect":"none","padding":"0.75rem 1rem","width":"100%"},
            className="d-flex align-items-center",
        ),
        id=header_id,
        className="bg-light",
        style={"backgroundColor": LIGHT_GREY, "borderBottom": "1px solid #e9ecef"},
    )

CARD_STYLE = {"overflow": "hidden", "border": "1px solid #e9ecef", "borderRadius": "0.5rem", "backgroundColor": "white"}

# ────────── Public layout builder ──────────
def layout_body():
    return dbc.Container([
        html.H3("Training Group", className="mt-1"),

        # Filters / selection
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="branch", options=BRANCH_OPTS, multi=True,
                                 placeholder="Select branch(es)…"), md=4),
            dbc.Col(dcc.Dropdown(id="grp", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s)…"), md=4),
            dbc.Col(dbc.Button("Load", id="go", color="primary", className="w-100"), md=2),
        ], className="g-2"),
        html.Hr(),
        html.Div(id="customer-checklist-container"),
        html.Br(),

        # 0) Athlete Summary (open)
        dbc.Card([
            clickable_header("Athlete Summary", "hdr-summary", "sym-summary", "hdr-summary-container"),
            dbc.Collapse(dbc.CardBody(html.Div(id="athlete-summary-container", style={"paddingTop":"0.5rem"})),
                         id="col-summary", is_open=True)
        ], className="mb-3", style=CARD_STYLE),

        # 1) Training-Status Calendar (OPEN by default)
        dbc.Card([
            clickable_header("Training-Status Calendar", "hdr-cal", "sym-cal", "hdr-cal-container"),
            dbc.Collapse(dbc.CardBody([
                dcc.Dropdown(id="focus-complaint", placeholder="Focus complaint (optional)…",
                             clearable=True, style={"maxWidth":"420px"}, className="mb-2"),
                html.Div(id="calendar-heatmap-container"),
            ], style={"paddingTop":"0.5rem"}), id="col-cal", is_open=True)
        ], className="mb-3", style=CARD_STYLE),

        # 2) Appointments table (closed)
        dbc.Card([
            clickable_header("Appointments", "hdr-table", "sym-table", "hdr-table-container"),
            dbc.Collapse(dbc.CardBody(html.Div(id="appointment-table-container", style={"paddingTop":"0.5rem"})),
                         id="col-table", is_open=False)
        ], className="mb-4", style=CARD_STYLE),

        dcc.Store(id="selected-athletes-map", data={}),
        dbc.Alert(id="msg", is_open=False, duration=0, color="danger"),
    ], fluid=True)

# ────────── Callback registration ──────────
def register_callbacks(app: dash.Dash):

    # Helpers for header toggle
    def _toggle(open_: bool) -> bool: return not open_
    def _sym(open_: bool) -> str:     return "−" if open_ else "+"
    def _hdr_style(open_: bool) -> dict:
        return {"backgroundColor": LIGHT_GREY,
                "borderBottom": "1px solid #e9ecef" if open_ else "0px solid transparent"}

    # Collapsible toggles
    @app.callback(
        Output("col-summary","is_open"),
        Output("sym-summary","children"),
        Output("hdr-summary-container","style"),
        Input("hdr-summary","n_clicks"),
        State("col-summary","is_open"),
        prevent_initial_call=True)
    def toggle_summary(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("col-cal","is_open"),
        Output("sym-cal","children"),
        Output("hdr-cal-container","style"),
        Input("hdr-cal","n_clicks"),
        State("col-cal","is_open"),
        prevent_initial_call=True)
    def toggle_cal(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("col-table","is_open"),
        Output("sym-table","children"),
        Output("hdr-table-container","style"),
        Input("hdr-table","n_clicks"),
        State("col-table","is_open"),
        prevent_initial_call=True)
    def toggle_table(n, is_open):
        new = _toggle(is_open); return new, _sym(new), _hdr_style(new)

    @app.callback(
        Output("grp", "options"),
        Output("grp", "value"),
        Input("branch", "value"),
        State("grp", "value"),
    )
    def sync_group_options_by_branch(selected_branches, selected_groups):
        # Use dynamic fetching to get groups for selected branches
        groups = fetch_groups_for_branches_dynamic(selected_branches or [])
        
        # Debug logging
        if selected_branches:
            branch_names = [BRANCH_NAME_BY_ID.get(int(b), f"Branch {b}") for b in selected_branches]
            print(f"\n=== Group Sync Debug ===")
            print(f"Selected branches: {selected_branches}")
            print(f"Branch names: {branch_names}")
            print(f"Groups found: {groups}")
            print(f"Number of groups: {len(groups)}")
        
        opts = [{"label": g.title(), "value": g} for g in groups]
        selected_groups_norm = [_norm(g) for g in (selected_groups or [])]
        allowed_set = {o["value"] for o in opts}
        pruned_selected = [g for g in selected_groups_norm if g in allowed_set]
        return opts, pruned_selected

    # ① Load groups → Athlete selector
    @app.callback(
        Output("customer-checklist-container", "children"),
        Output("msg", "children"), Output("msg", "is_open"),
        Input("go", "n_clicks"),
        State("branch", "value"),
        State("grp", "value"),
        prevent_initial_call=True,
    )
    def make_customer_selector(n_clicks, branch_raw, groups_raw):
        if not groups_raw and not branch_raw:
            return no_update, "Select at least one branch or group.", True
        targets = {_norm(g) for g in (groups_raw or [])}
        branch_targets = {int(v) for v in (branch_raw or [])}
        matching = [
            {"label": f"{c['first_name']} {c['last_name']} (ID {cid})", "value": cid}
            for cid, c in CUSTOMERS.items()
            if ((not targets) or (targets & set(_customer_groups(cid, c))))
            and ((not branch_targets) or (_customer_branch(cid, c) in branch_targets))
        ]
        if not matching:
            return html.Div("No patients match the selected branch/group filters."), "", False
        selector = dcc.Dropdown(
            id="cust-select",
            options=matching,
            value=None,
            placeholder="Select athlete…",
            clearable=False,
            style={"maxWidth": 420},
        )
        return selector, "", False

    # ②A When athlete changes → update map + focus options + reset focus to ALL
    @app.callback(
        Output("selected-athletes-map", "data"),
        Output("focus-complaint", "options"),
        Output("focus-complaint", "value"),
        Output("msg", "children", allow_duplicate=True),
        Output("msg", "is_open", allow_duplicate=True),
        Input("cust-select", "value"),
        prevent_initial_call=True,
    )
    def update_focus_options(selected_cid):
        try:
            if not selected_cid:
                return {}, [], None, "", False

            cid = int(selected_cid)
            cust = CUSTOMERS.get(cid, {})
            label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()
            id_to_label = {cid: label}

            # Build union of complaint names (customer + appointments)
            appointment_complaints: set[str] = set()
            customer_complaints_union: set[str] = set()

            for c in fetch_customer_complaints(cid):
                n = (c.get("Title") or "").strip()
                if n: customer_complaints_union.add(n)

            for ap in CID_TO_APPTS.get(cid, []):
                names: List[str] = []
                for rec in list_complaints_for_appt(ap.get("id")):
                    nm = _extract_name(rec)
                    if nm: names.append(nm)
                comp_inline = ap.get("complaint")
                if isinstance(comp_inline, dict):
                    nm = _extract_name(comp_inline)
                    if nm: names.append(nm)
                names = sorted(set(n.strip() for n in names if n.strip()))
                if names: appointment_complaints.update(names)

            all_names = sorted({n for n in (appointment_complaints | customer_complaints_union) if n})
            opts = [{"label":"All complaints","value":"__ALL__"}] + [{"label": n, "value": n} for n in all_names]

            return id_to_label, opts, "__ALL__", "", False
        except Exception:
            tb = traceback.format_exc()
            print("\n=== update_focus_options error ===\n", tb)
            return {}, [], None, "Error preparing focus options.", True

    # ②B Athlete or Focus change → rebuild calendar & table
    @app.callback(
        Output("calendar-heatmap-container", "children"),
        Output("appointment-table-container", "children"),
        Output("msg", "children", allow_duplicate=True),
        Output("msg", "is_open", allow_duplicate=True),
        Input("cust-select", "value"),
        Input("focus-complaint", "value"),
        prevent_initial_call=True,
    )
    def show_calendar_and_table(selected_cid, focus_value):
        try:
            if not selected_cid:
                return "", html.Div("Select an athlete."), "", False

            cid = int(selected_cid)
            rows = []

            # Gather rows with status + complaint names
            for ap in CID_TO_APPTS.get(cid, []):
                aid = ap.get("id")
                date_str = tidy_date_str(ap.get("date"))
                status = latest_training_status_for_appt(int(aid)) if aid else ""

                names: List[str] = []
                for rec in list_complaints_for_appt(aid):
                    nm = _extract_name(rec)
                    if nm: names.append(nm)
                comp_inline = ap.get("complaint")
                if isinstance(comp_inline, dict):
                    nm = _extract_name(comp_inline)
                    if nm: names.append(nm)

                names = sorted(set(n.strip() for n in names if n.strip()))
                rows.append({
                    "Date":            date_str,
                    "Training Status": status,
                    "Complaint Names": "; ".join(names) if names else "",
                })

            if not rows:
                return html.Div("No appointments found."), html.Div(), "", False

            df = pd.DataFrame(rows)
            df["Date"] = pd.to_datetime(df["Date"], format="%Y-%m-%d", errors="coerce")
            df = df.dropna(subset=["Date"]).sort_values(["Date"]).reset_index(drop=True)

            # Apply focus filter
            work = df.copy()
            if focus_value and focus_value != "__ALL__":
                mask = work["Complaint Names"].str.contains(
                    rf"(^|;\s*){re.escape(focus_value)}($|;\s*)", case=False, na=False
                )
                work = work[mask].copy()

            # Table
            def build_status_cell(s: str) -> str:
                col = PASTEL_COLOR.get(s)
                return f"{dot_html(col)}{s}" if col else (s or "")

            work["Status"] = work["Training Status"].apply(build_status_cell)
            table = dash_table.DataTable(
                id="appt-table",
                data=work.assign(Date=work["Date"].dt.strftime("%Y-%m-%d"))[[
                    "Date","Status","Complaint Names"
                ]].rename(columns={"Complaint Names":"Complaints"}).to_dict("records"),
                columns=[
                    {"name":"Date","id":"Date"},
                    {"name":"Status","id":"Status","presentation":"markdown"},
                    {"name":"Complaints","id":"Complaints"},
                ],
                markdown_options={"html": True},
                page_size=12,
                style_table={"overflowX":"auto"},
                style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","borderBottom":"1px solid #e9ecef"},
                style_cell={"padding":"9px","fontSize":14,
                            "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                            "textAlign":"left"},
                style_data={"borderBottom":"1px solid #eceff4"},
                style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
            )

            # Calendar (forward-fill to daily)
            df_valid = work[work["Training Status"].isin(STATUS_ORDER)].copy()
            if df_valid.empty:
                return html.Div("No valid date/status for calendar."), table, "", False

            df_valid = df_valid.sort_values("Date").drop_duplicates("Date", keep="last")
            df_valid["Status Code"] = df_valid["Training Status"].map(STATUS_CODE)

            full_index = pd.date_range(start=df_valid["Date"].min(),
                                       end=pd.Timestamp("today").normalize(), freq="D")
            heat_df = pd.DataFrame({"Date": full_index})
            heat_df = heat_df.merge(df_valid[["Date","Status Code"]], on="Date", how="left").sort_values("Date")
            heat_df["Status Code"] = heat_df["Status Code"].ffill().fillna(-1).astype(int)
            heat_df = heat_df[heat_df["Status Code"] >= 0].copy()

            if not PLOTLYCAL_AVAILABLE:
                return html.Div([
                    html.P("Cannot draw calendar heatmap: 'plotly-calplot' is not installed."),
                    html.P("Install with: pip install plotly-calplot"),
                ]), table, "", False

            colorscale = discrete_colorscale_from_hexes(COLOR_LIST)
            fig_cal = pc.calplot(heat_df, x="Date", y="Status Code", colorscale=colorscale)

            # Hide legend / colorbar
            heatmap: Optional[go.Heatmap] = next((t for t in fig_cal.data if isinstance(t, go.Heatmap)), None)
            if heatmap is not None:
                heatmap.showscale = False
                heatmap.zmin = 0
                heatmap.zmax = 4
                heatmap.xgap = 2
                heatmap.ygap = 2
            fig_cal.update_layout(showlegend=False)

            # Styling
            fig_cal.update_layout(
                title_text=f"Calendar Heatmap: {int(heat_df['Date'].dt.year.max())}",
                margin=dict(l=18, r=18, t=46, b=10),
                height=480,
                paper_bgcolor="white",
                plot_bgcolor="white",
                font=dict(family="system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                          size=13, color="#111111"),
                title_font_color="#111111",
            )
            fig_cal.update_xaxes(tickfont=dict(color="#111111"))
            fig_cal.update_yaxes(tickfont=dict(color="#111111"))

            # Abbreviate month names in all annotation texts (robust rewrite)
            MONTHS = {
                "January":"Jan","February":"Feb","March":"Mar","April":"Apr","May":"May","June":"Jun",
                "July":"Jul","August":"Aug","September":"Sep","October":"Oct","November":"Nov","December":"Dec"
            }
            month_pattern = re.compile(r"\b(" + "|".join(MONTHS.keys()) + r")\b")
            new_annotations = []
            for ann in (fig_cal.layout.annotations or []):
                try:
                    jd = ann.to_plotly_json()
                    txt = str(jd.get("text", "") or "")
                    if txt:
                        jd["text"] = month_pattern.sub(lambda m: MONTHS[m.group(1)], txt)
                    new_annotations.append(jd)
                except Exception:
                    new_annotations.append(ann)
            if new_annotations:
                fig_cal.update_layout(annotations=new_annotations)

            cal_graph = dcc.Graph(id="cal-graph", figure=fig_cal, config={"displayModeBar": False})
            return cal_graph, table, "", False

        except Exception:
            tb = traceback.format_exc()
            print("\n=== show_calendar_and_table error ===\n", tb)
            return html.Div("Error building calendar."), html.Div([
                html.P("Unexpected error in processing:"),
                html.Pre(tb),
            ]), "", True

    # Athlete Summary (listen to current selection directly)
    @app.callback(
        Output("athlete-summary-container", "children"),
        Input("cust-select", "value"),
    )
    def render_athlete_summary(focus_id):
        cid = int(focus_id) if focus_id else None
        if not cid or cid not in CUSTOMERS:
            return html.Div("Select an athlete to see demographics, current status, and complaints.", className="text-muted")

        # Merge list row + detail row so DOB & friends are present
        cust_list = CUSTOMERS.get(cid, {})
        cust_full = fetch_customer_detail(cid) or {}
        def _first_nonempty(*vals):
            for v in vals:
                if isinstance(v, str) and v.strip(): return v.strip()
            return ""

        first = _first_nonempty(cust_full.get("first_name"), cust_list.get("first_name"))
        last  = _first_nonempty(cust_full.get("last_name"),  cust_list.get("last_name"))
        label = f"{first} {last} (ID {cid})".strip()

        dob   = _first_nonempty(cust_full.get("dob"), cust_full.get("birthdate"), cust_list.get("dob"), cust_list.get("birthdate"))
        sex   = _first_nonempty(cust_full.get("sex"), cust_full.get("gender"), cust_list.get("sex"), cust_list.get("gender"))
        email = _first_nonempty(cust_full.get("email"), cust_list.get("email"))
        phone = _first_nonempty(cust_full.get("phone"), cust_full.get("mobile"), cust_list.get("phone"), cust_list.get("mobile"))

        chips = [html.Span(g.title(), className="badge bg-light text-dark me-1 mb-1",
                           style={"border":"1px solid #e3e6eb"}) for g in CID_TO_GROUPS.get(cid, [])]

        # Current training status (forward-filled)
        appts = CID_TO_APPTS.get(cid, [])
        status_rows: List[Tuple[pd.Timestamp, str]] = []
        for ap in appts:
            aid = ap.get("id")
            date_str = tidy_date_str(ap.get("date"))
            dt = pd.to_datetime(date_str, errors="coerce")
            if pd.isna(dt): continue
            s = latest_training_status_for_appt(int(aid)) if aid else ""
            if s: status_rows.append((dt.normalize(), s))
        current_status = ""
        if status_rows:
            df_s = pd.DataFrame(status_rows, columns=["Date","Status"]).sort_values("Date")
            df_s = df_s.drop_duplicates("Date", keep="last")
            full_idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
            df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
            df_full["Status"] = df_full["Status"].ffill()
            current_status = str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""

        dot_color = PASTEL_COLOR.get(current_status, "#e6e6e6")
        big_dot = html.Span(style={
            "display":"inline-block","width":"18px","height":"18px",
            "borderRadius":"50%","background":dot_color,
            "border":"1px solid rgba(0,0,0,.25)","marginRight":"10px"
        })

        # Complaints table with Onset / Priority / Status
        complaints = fetch_customer_complaints(cid)
        if complaints:
            comp_rows = [{"Title": c.get("Title",""), "Onset": c.get("Onset",""),
                          "Priority": c.get("Priority",""), "Status": c.get("Status","")} for c in complaints]
            comp_table = dash_table.DataTable(
                columns=[{"name":"Title","id":"Title"},
                         {"name":"Onset","id":"Onset"},
                         {"name":"Priority","id":"Priority"},
                         {"name":"Status","id":"Status"}],
                data=comp_rows, page_size=5,
                style_header={"fontWeight":"600","backgroundColor":"#fafbfc"},
                style_cell={"padding":"6px","fontSize":13,
                            "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                            "textAlign":"left"},
                style_data={"borderBottom":"1px solid #eee"},
                style_table={"overflowX":"auto"},
            )
        else:
            comp_table = html.Div("No complaints found.", className="text-muted")

        return dbc.Row([
            dbc.Col([
                html.H5(label, className="mb-2"),
                html.Div(chips, className="mb-2"),
                html.Div([
                    html.Span("Current Status: ", className="fw-semibold me-1"),
                    big_dot, html.Span(current_status or "—")
                ], className="mb-2"),
                html.Div([
                    html.Div([html.Span("DOB: ", className="fw-semibold"), html.Span(dob or "—")]),
                    html.Div([html.Span("Sex: ", className="fw-semibold"), html.Span(sex or "—")]),
                    html.Div([html.Span("Email: ", className="fw-semibold"), html.Span(email or "—")]),
                    html.Div([html.Span("Phone: ", className="fw-semibold"), html.Span(phone or "—")]),
                ], style={"fontSize":"14px"})
            ], md=5),
            dbc.Col([
                html.Div("Complaints", className="fw-semibold mb-2"),
                comp_table
            ], md=7),
        ])
