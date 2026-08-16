# juvonno_api.py — slim Juvonno API client for the SCAT6 intake app.
#
# Carries over the proven branch / group / athlete loading logic from the
# original training_dashboard.py, and adds customer-document push/pull
# (GET/POST /customers/{id}/documents) used to store SCAT6 PDFs and the
# per-athlete SCAT6 history CSV in Juvonno.
#
# API reference: https://app.swaggerhub.com/apis/globaloffice/juvonno/2.5.4
from __future__ import annotations
import os, re, base64, functools, mimetypes
from typing import Dict, List, Iterable, Optional, Tuple

import requests

# ────────── API config ──────────
API_KEY = os.getenv("JUV_API_KEY")
BASE    = os.getenv("JUV_API_BASE", "https://csipacific.juvonno.com/api").rstrip("/")
HEADERS = {"accept": "application/json"}


def _require_api_key():
    if not API_KEY:
        print("WARNING: JUV_API_KEY not set. Juvonno API calls will fail.")
        return False
    return True


def _redact(text: str) -> str:
    """Never let the API key appear in error messages / UI alerts."""
    if API_KEY and text:
        text = str(text).replace(API_KEY, "***")
    return str(text)


def _get(path: str, **params):
    if not API_KEY:
        raise RuntimeError("API_KEY not configured. Set JUV_API_KEY environment variable.")
    request_headers = dict(HEADERS)
    request_headers.setdefault("x-api-key", API_KEY)
    params.setdefault("api_key", API_KEY)
    try:
        r = requests.get(f"{BASE}/{path.lstrip('/')}", params=params, headers=request_headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"API request timeout for {path}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(_redact(f"API request failed for {path}: {e}"))


def _post_json(path: str, json_body: Dict | None = None, **params):
    """POST with API-key auth (used for upload_token)."""
    if not API_KEY:
        raise RuntimeError("API_KEY not configured. Set JUV_API_KEY environment variable.")
    headers = {"x-api-key": API_KEY, "accept": "application/json"}
    params.setdefault("api_key", API_KEY)
    r = requests.post(f"{BASE}/{path.lstrip('/')}", params=params, headers=headers,
                      json=(json_body or {}), timeout=15)
    if not r.ok:
        raise RuntimeError(_redact(
            f"POST {path} failed: {r.status_code} {r.reason} — {r.text[:300]}"))
    try:
        return r.json()
    except ValueError:
        return {"status": "ok", "raw": r.text}


def get_document_upload_token(customer_id: int) -> str:
    """POST /customers/{id}/documents/upload_token → single-use upload token.
    Per the Juvonno spec, document uploads must use this token (api_token in the
    multipart body) rather than the api_key."""
    js = _post_json(f"customers/{int(customer_id)}/documents/upload_token")
    if isinstance(js, dict):
        tok = js.get("token") or js.get("api_token") or js.get("upload_token")
        if not tok:
            for v in js.values():
                if isinstance(v, dict) and (v.get("token") or v.get("api_token")):
                    tok = v.get("token") or v.get("api_token")
                    break
        if tok:
            return str(tok)
    raise RuntimeError(f"upload_token response had no token (keys: "
                       f"{list(js.keys()) if isinstance(js, dict) else type(js)})")


def _post_multipart(path: str, files: Dict, data: Dict | None = None,
                    use_api_key: bool = False):
    """POST multipart/form-data. With use_api_key the api_key is attached
    (legacy fallback); the spec-compliant document upload authenticates via the
    api_token inside `data` instead."""
    headers = {"accept": "application/json"}
    params = {}
    if use_api_key:
        if not API_KEY:
            raise RuntimeError("API_KEY not configured. Set JUV_API_KEY environment variable.")
        headers["x-api-key"] = API_KEY
        params["api_key"] = API_KEY
    r = requests.post(f"{BASE}/{path.lstrip('/')}", params=params, headers=headers,
                      files=files, data=(data or {}), timeout=30)
    if not r.ok:
        raise RuntimeError(_redact(
            f"POST {path} failed: {r.status_code} {r.reason} — {r.text[:300]}"))
    try:
        return r.json()
    except ValueError:
        return {"status": "ok", "raw": r.text}


# ────────── payload helpers (unchanged from original app) ──────────
def _extract_rows(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("list", "results", "data", "customers", "items", "branches", "clinics",
                "locations", "sites", "documents"):
        block = payload.get(key)
        if isinstance(block, list):
            return block
        if isinstance(block, dict):
            for nested_key in ("list", "results", "data", "items", "branches", "clinics",
                               "locations", "sites", "documents"):
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
    for key in ("total", "total_count", "recordsTotal", "count"):
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


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _branch_id_from_obj(obj: Dict) -> Optional[int]:
    if not isinstance(obj, dict):
        return None
    candidates = [
        obj.get("branch_id"), obj.get("branchId"), obj.get("clinic_id"),
        obj.get("location_id"), obj.get("site_id")
    ]
    for key in ("branch", "clinic", "location"):
        sub = obj.get(key)
        if isinstance(sub, dict):
            candidates.extend([sub.get("id"), sub.get(f"{key}_id")])
    for value in candidates:
        try:
            if value is None or value == "":
                continue
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _group_names_from_customer(cust: Dict) -> List[str]:
    names: List[str] = []

    def _extract_from_container(container: Dict, search_keys: Iterable[str]):
        for src_key in search_keys:
            src = container.get(src_key)
            if src is None:
                continue
            if isinstance(src, list):
                for it in src:
                    if isinstance(it, str):
                        for token in re.split(r"[,;|]", it):
                            token_n = _norm(token)
                            if token_n:
                                names.append(token_n)
                    elif isinstance(it, dict):
                        raw = _first_non_empty(it.get("name"), it.get("label"),
                                               it.get("title"), it.get("group_name"))
                        if raw:
                            names.append(_norm(raw))
                        nested = it.get("group")
                        if isinstance(nested, dict):
                            raw_n = _first_non_empty(nested.get("name"), nested.get("label"),
                                                     nested.get("title"), nested.get("group_name"))
                            if raw_n:
                                names.append(_norm(raw_n))
            elif isinstance(src, dict):
                raw = _first_non_empty(src.get("name"), src.get("label"),
                                       src.get("title"), src.get("group_name"))
                if raw:
                    names.append(_norm(raw))
                nested = src.get("group")
                if isinstance(nested, dict):
                    raw_n = _first_non_empty(nested.get("name"), nested.get("label"),
                                             nested.get("title"), nested.get("group_name"))
                    if raw_n:
                        names.append(_norm(raw_n))
            elif isinstance(src, str):
                for token in re.split(r"[,;|]", src):
                    token_n = _norm(token)
                    if token_n:
                        names.append(token_n)

    group_keys = ("groups", "group", "customer_groups", "patient_groups", "tags",
                  "memberships", "member_groups", "assignments")
    _extract_from_container(cust, group_keys)
    for container_key in ("clinic", "location", "branch", "site"):
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
                rows_out.append(row); new_added += 1
                continue
            if rid_int in seen_ids:
                continue
            seen_ids.add(rid_int); rows_out.append(row); new_added += 1
        total = _extract_total(js)
        has_more = _extract_has_more(js)
        if total is not None and len(seen_ids) >= total:
            break
        if has_more is False:
            break
        if new_added == 0:
            break
    return rows_out


# ────────── Branches (loaded once at startup — lightweight) ──────────
def fetch_branches_and_clinics_direct() -> Dict[int, Dict]:
    all_branches: Dict[int, Dict] = {}
    for endpoint, base_params in (
        ("clinics/list", {}), ("clinics", {}),
        ("branches/list", {}), ("branches", {}),
        ("locations/list", {}), ("locations", {}),
    ):
        try:
            collected = _fetch_all_rows(endpoint, base_params, page_size=100, max_pages=500)
            for b in collected:
                if isinstance(b, dict) and b.get("id") is not None:
                    bid = int(b["id"])
                    all_branches.setdefault(bid, dict(b))
        except Exception:
            pass
    return all_branches


BRANCH_NAME_BY_ID: Dict[int, str] = {}
BRANCH_OPTS: List[Dict] = []
DIRECT_BRANCHES: Dict[int, Dict] = {}

_require_api_key()
if API_KEY:
    try:
        DIRECT_BRANCHES = fetch_branches_and_clinics_direct()
        if not DIRECT_BRANCHES:
            try:
                sample = _fetch_all_rows("customers/list", {"include": "clinic,location,branch"},
                                         page_size=100, max_pages=1)
                for c in sample:
                    bid = _branch_id_from_obj(c)
                    if bid is not None and bid not in DIRECT_BRANCHES:
                        for obj_key in ("clinic", "branch", "location"):
                            obj = c.get(obj_key)
                            if isinstance(obj, dict) and obj.get("id") and int(obj["id"]) == bid:
                                DIRECT_BRANCHES[bid] = dict(obj)
                                break
                        else:
                            DIRECT_BRANCHES[bid] = {"id": bid}
            except Exception as fe:
                print(f"  Branch fallback failed: {fe}")
        for bid, branch in DIRECT_BRANCHES.items():
            if isinstance(branch, dict):
                for name_key in ("name", "title", "label", "code", "clinic_name", "branch_name"):
                    val = branch.get(name_key)
                    if isinstance(val, str) and val.strip():
                        BRANCH_NAME_BY_ID[bid] = val.strip()
                        break
        BRANCH_OPTS = sorted(
            [{"label": BRANCH_NAME_BY_ID.get(bid, f"Branch {bid}"), "value": bid}
             for bid in sorted(DIRECT_BRANCHES.keys())],
            key=lambda o: (str(o.get("label", "")).casefold(), int(o.get("value", 0)))
        )
        print(f"Juvonno: {len(BRANCH_OPTS)} branches loaded")
    except Exception as e:
        print(f"Juvonno branch init failed: {e}")


# ────────── Customers / athletes ──────────
@functools.lru_cache(maxsize=4096)
def fetch_customer_detail(customer_id: int) -> Dict:
    try:
        js = _get(f"customers/{int(customer_id)}", include="full,groups,clinic,location")
        if isinstance(js, dict):
            return js.get("customer", js)
    except Exception:
        pass
    return {}


@functools.lru_cache(maxsize=64)
def get_athletes_for_branch(branch_id: int) -> List[Dict]:
    """Probe URL/param variants (Juvonno deployments differ) and return
    [{id, label, value, groups}] for all customers in the branch."""
    candidates = [
        (f"branches/{branch_id}/customers", {"page": 1, "count": 50, "include": "groups,clinic"}),
        (f"branches/{branch_id}/customers", {"page": 1, "count": 50}),
        (f"branches/{branch_id}/customers", {"page": 1, "results": 50}),
        (f"clinics/{branch_id}/customers",  {"page": 1, "count": 50, "include": "groups,clinic"}),
        (f"clinics/{branch_id}/customers",  {"page": 1, "count": 50}),
        ("customers/list", {"page": 1, "count": 50, "clinic_id": branch_id, "include": "groups,clinic"}),
        ("customers/list", {"page": 1, "count": 50, "clinic_id": branch_id}),
        ("customers/list", {"page": 1, "count": 50, "branch_id": branch_id}),
    ]
    for path, params in candidates:
        try:
            js = _get(path, **params)
            rows = _extract_rows(js)
            if not rows:
                continue
            athletes: List[Dict] = []
            seen: set[int] = set()

            def _collect(row_list):
                for c in row_list:
                    if not isinstance(c, dict) or c.get("id") is None:
                        continue
                    cid = int(c["id"])
                    if cid in seen:
                        continue
                    seen.add(cid)
                    first = (c.get("first_name") or "").strip()
                    last  = (c.get("last_name") or "").strip()
                    name  = f"{first} {last}".strip() or c.get("name", f"Athlete {cid}")
                    athletes.append({"id": cid, "label": name, "value": cid,
                                     "groups": _group_names_from_customer(c), "_raw": c})

            _collect(rows)
            page = 2
            page_size = params.get("results") or params.get("count") or 50
            while len(rows) >= page_size:
                js2 = _get(path, **{**params, "page": page})
                rows = _extract_rows(js2)
                if not rows:
                    break
                _collect(rows)
                page += 1

            # Enrich group-less athletes from customer detail (LRU-cached)
            for a in athletes:
                if not a["groups"]:
                    try:
                        detail = fetch_customer_detail(a["id"])
                        if isinstance(detail, dict) and detail:
                            merged = dict(a["_raw"]); merged.update(detail)
                            a["groups"] = (_group_names_from_customer(merged)
                                           or _group_names_from_customer(detail))
                    except Exception:
                        pass
            for a in athletes:
                a.pop("_raw", None)
            return sorted(athletes, key=lambda x: (x["label"].lower(), x["id"]))
        except Exception as e:
            print(f"  athletes probe failed {path}: {e}")
    return []


def athlete_demographics(customer_id: int) -> Dict:
    """Best-effort demographics for pre-filling the SCAT6 header."""
    d = fetch_customer_detail(int(customer_id)) or {}
    first = (d.get("first_name") or "").strip()
    last  = (d.get("last_name") or "").strip()
    dob = d.get("date_of_birth") or d.get("dob") or d.get("birth_date") or ""
    if isinstance(dob, str):
        dob = dob.split("T")[0]
    gender = d.get("gender") or d.get("sex") or ""
    return {
        "id": int(customer_id),
        "name": f"{first} {last}".strip(),
        "dob": dob or "",
        "sex": str(gender or ""),
        "chart_number": d.get("chart_number") or d.get("chart") or "",
        "language": d.get("language") or "",
        "email": d.get("email") or "",
    }


# ────────── Customer documents (push / pull) ──────────
def list_customer_documents(customer_id: int) -> List[Dict]:
    """GET /customers/{id}/documents → list of document dicts."""
    try:
        js = _get(f"customers/{int(customer_id)}/documents")
        rows = _extract_rows(js)
        return [r for r in rows if isinstance(r, dict)]
    except Exception as e:
        print(f"list_customer_documents({customer_id}): {e}")
        return []


def download_customer_document(customer_id: int, document_id: int) -> Tuple[str, bytes]:
    """GET /customers/{id}/documents/{docId} → (filename, raw bytes).
    The API returns the file content base64-encoded."""
    js = _get(f"customers/{int(customer_id)}/documents/{int(document_id)}")
    doc = js.get("document", js) if isinstance(js, dict) else {}
    name = (doc.get("name") or doc.get("filename") or doc.get("file_name")
            or f"document_{document_id}")
    payload = None
    for key in ("data", "file", "content", "file_data", "base64", "document"):
        val = doc.get(key)
        if isinstance(val, str) and len(val) > 16:
            payload = val
            break
        if isinstance(val, dict):
            for k2 in ("data", "content", "base64"):
                v2 = val.get(k2)
                if isinstance(v2, str) and len(v2) > 16:
                    payload = v2
                    break
    if payload is None:
        raise RuntimeError(f"No file payload found in document {document_id} response "
                           f"(keys: {list(doc.keys()) if isinstance(doc, dict) else type(doc)})")
    # strip possible data-URL prefix
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[-1]
    return name, base64.b64decode(payload)


def upload_customer_document(customer_id: int, file_bytes: bytes, filename: str,
                             description: str = "", date: str = "",
                             portal_visible: bool = False) -> Dict:
    """POST /customers/{id}/documents (multipart).

    Spec-compliant two-step flow: first obtain a single-use token from
    POST /customers/{id}/documents/upload_token, then upload with `api_token`
    in the form data (no api_key — Juvonno returns 403 otherwise). Falls back
    to direct api_key auth only if the token endpoint is unavailable."""
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def _payload():
        data = {"name": filename, "portal_visible": "1" if portal_visible else "0"}
        if description:
            data["description"] = description
        if date:
            data["date"] = date
        return {"file": (filename, file_bytes, mime)}, data

    path = f"customers/{int(customer_id)}/documents"
    try:
        token = get_document_upload_token(int(customer_id))
    except Exception as te:
        # Older instances without the token endpoint: try legacy direct upload.
        print(f"upload_token unavailable ({te}); trying direct api_key upload")
        files, data = _payload()
        return _post_multipart(path, files=files, data=data, use_api_key=True)

    files, data = _payload()
    data["api_token"] = token
    return _post_multipart(path, files=files, data=data, use_api_key=False)


def find_document_by_name(customer_id: int, filename: str) -> Optional[Dict]:
    """Return the newest document record whose name matches filename (case-insensitive)."""
    target = filename.strip().lower()
    matches = []
    for d in list_customer_documents(customer_id):
        name = str(d.get("name") or d.get("filename") or d.get("file_name") or "").strip().lower()
        if name == target or name == target.rsplit(".", 1)[0]:
            matches.append(d)
    if not matches:
        return None
    def _key(d):
        return str(d.get("date") or d.get("created_at") or ""), int(d.get("id") or 0)
    return sorted(matches, key=_key)[-1]
