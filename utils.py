import requests
from settings import SITE_URL

def fetch_options(path, token, label_key, value_key, params=None, limit=1000):
    headers = {"Authorization": f"Bearer {token}"}

    if params and isinstance(params,dict):
        params.update({"limit": limit})
    else:
        params = {"limit": limit}

    resp = requests.get(f"{SITE_URL}{path}", params=params, headers=headers, timeout=5)
    resp.raise_for_status()

    # print("=========================")
    # print(path)

    items = resp.json()

    # print(items)

    if 'results' in items:
        rv = [{"label": item[label_key], "value": item[value_key]} for item in items["results"]]
    elif isinstance(items, list):
        rv = [{"label": val, "value": val} for val in items if val]
    else:
        rv = []

    return rv


def fetch_profiles(token, filters):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{SITE_URL}/api/registration/profile/"
    params = {**filters, "limit": 100, "offset": 0}  # choose a reasonable chunk size
    all_records = []

    print(params)

    while url:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        payload = r.json()
        all_records.extend(payload["results"])

        # Move to the next page
        url = payload.get("next")
        # Once we switch to using `next`, we no longer need `params`
        params = None

    return all_records

def restructure_profile(profile):
    record = {
        'id': profile['id'],
        'first_name': profile['person']['first_name'] if profile['person'] else None,
        'last_name': profile['person']['last_name'] if profile['person'] else None,
        'email': profile['person']['email'] if profile['person'] else None,
        'sport': profile['sport']['name'] if profile['sport'] else None,
        'enrollment_status': profile['current_enrollment']['enrollment_status'] if profile[
            'current_enrollment'] else None
    }

    return record