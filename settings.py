import os
from dotenv import load_dotenv
load_dotenv()

SITE_URL = os.environ.get("SITE_URL","http://127.0.0.1:8000")
APP_URL = os.environ.get("APP_URL","http://127.0.0.1:8050")


# using spotify as an example
AUTH_URL = f"{SITE_URL}/o/authorize"
TOKEN_URL = f"{SITE_URL}/o/token/"
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

API_PEOPLE_URL = f"{SITE_URL}/api/registration/profile/"
API_ME_URL = f"{SITE_URL}/api/csiauth/me/"