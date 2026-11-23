"""구글 계정 로그인(OAuth).

두 가지 방식:
- 웹(Streamlit Cloud): Secrets의 [oauth] 웹 클라이언트로 각자 자기 계정에 로그인. 로그인 정보는 그 사람의 세션에만 있다.
- 로컬 PC: credentials.json(데스크톱 앱 클라이언트)로 한 번 로그인 → token.json 저장.
  주간 자동 실행(run_weekly.py)은 GitHub Actions에서는 환경변수 토큰, PC에서는 token.json을 쓴다.

권한: 메일 보내기(gmail.send), 캘린더(calendar), 수신자 시트(spreadsheets), 로그인 이메일 확인.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).parent
CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"
ALLOWED_DOMAIN = "dcamp.kr"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]


class NotLoggedIn(RuntimeError):
    pass


def user_email(creds: Credentials) -> str:
    from googleapiclient.discovery import build

    info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
    return info.get("email", "")


def domain_ok(email: str) -> bool:
    return email.lower().endswith("@" + ALLOWED_DOMAIN)


# ---------------------------------------------------------------- 웹(Streamlit Cloud)
def auth_url(client_id: str, redirect_uri: str) -> str:
    """구글 로그인 화면 주소. PKCE 없이(리디렉트 후 세션이 끊겨도 동작하도록) — 회의실 예약 앱과 같은 방식."""
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
         "hd": ALLOWED_DOMAIN, "include_granted_scopes": "true"}
    return f"{AUTH_URI}?{urllib.parse.urlencode(q)}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> Credentials:
    r = requests.post(TOKEN_URI, data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                       "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data.get("error_description") or data["error"])
    granted = set((data.get("scope") or "").split())
    missing = [s for s in SCOPES if s not in granted and s != "openid"]
    if missing:
        raise RuntimeError("권한 일부를 허용하지 않았습니다. 다시 로그인해 모든 항목을 허용해 주세요: "
                           + ", ".join(m.rsplit("/", 1)[-1] for m in missing))
    return Credentials(token=data["access_token"], refresh_token=data.get("refresh_token"), token_uri=TOKEN_URI,
                       client_id=client_id, client_secret=client_secret, scopes=SCOPES)


def creds_from_json(s: str) -> Credentials:
    creds = Credentials.from_authorized_user_info(json.loads(s), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


# ---------------------------------------------------------------- 로컬 PC
def env_creds() -> Credentials | None:
    """GitHub Actions 자동 발송: 저장소 Secrets(GOOGLE_CLIENT_ID·SECRET·REFRESH_TOKEN)로 로그인."""
    rt, cid, cs = (os.environ.get(k, "").strip() for k in
                   ("GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"))
    if not (rt and cid and cs):
        return None
    creds = Credentials(token=None, refresh_token=rt, token_uri=TOKEN_URI, client_id=cid, client_secret=cs,
                        scopes=SCOPES)
    creds.refresh(Request())
    return creds


def load_creds() -> Credentials | None:
    """로그인 정보: 환경변수(GitHub Actions) → token.json(로컬 PC). 만료됐으면 자동 갱신."""
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        return env_creds()  # 토큰이 잘못됐으면 여기서 오류가 나서 원인이 로그에 남는다
    if not TOKEN_FILE.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except Exception:
        return None
    if not set(SCOPES) <= set(creds.scopes or []):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            return None
    return None


def require_creds() -> Credentials:
    creds = load_creds()
    if not creds:
        raise NotLoggedIn("구글 로그인이 필요합니다. GitHub Actions라면 저장소 Secrets(GOOGLE_CLIENT_ID·SECRET·REFRESH_TOKEN)를, "
                          "PC라면 'streamlit run app.py'로 로그인했는지 확인하세요")
    return creds


def login_local() -> Credentials:
    """브라우저 새 탭에서 구글 로그인 → token.json 저장. @dcamp.kr 계정만 허용."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError("credentials.json이 없습니다. README '최초 설정'을 참고해 만들어 주세요")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", hd=ALLOWED_DOMAIN,
                                  open_browser=True, success_message="로그인되었습니다. 이 탭은 닫아도 됩니다.")
    email = user_email(creds)
    if not domain_ok(email):
        raise PermissionError(f"@{ALLOWED_DOMAIN} 계정만 사용할 수 있습니다 (로그인: {email})")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def logout_local() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
