"""구글 계정 로그인(OAuth). 한 번 로그인하면 token.json에 저장돼 주간 자동 실행에서도 그대로 쓴다.

필요 파일: credentials.json — Google Cloud 콘솔에서 만든 'OAuth 클라이언트 ID(데스크톱 앱)' 파일.
권한: 메일 보내기(gmail.send), 캘린더 읽기·쓰기(calendar), 로그인한 이메일 확인.
"""
from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).parent
CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"
ALLOWED_DOMAIN = "dcamp.kr"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]


class NotLoggedIn(RuntimeError):
    pass


def load_creds() -> Credentials | None:
    """저장된 로그인 정보. 만료됐으면 자동 갱신. 없거나 권한이 바뀌었으면 None."""
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
        raise NotLoggedIn("구글 로그인이 필요합니다. '실행하기.bat'으로 앱을 열고 로그인하세요")
    return creds


def login() -> Credentials:
    """브라우저 새 탭에서 구글 로그인 → token.json 저장. @dcamp.kr 계정만 허용."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError("credentials.json이 없습니다. README '최초 설정'을 참고해 만들어 주세요")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  hd=ALLOWED_DOMAIN, open_browser=True,
                                  success_message="로그인되었습니다. 이 탭은 닫아도 됩니다.")
    email = user_email(creds)
    if not email.lower().endswith("@" + ALLOWED_DOMAIN):
        raise PermissionError(f"@{ALLOWED_DOMAIN} 계정만 사용할 수 있습니다 (로그인: {email})")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def logout() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def user_email(creds: Credentials) -> str:
    from googleapiclient.discovery import build

    info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
    return info.get("email", "")
