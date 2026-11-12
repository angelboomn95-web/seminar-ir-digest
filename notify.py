"""주간 메일 발송(로그인한 Gmail 계정) + '세미나·IR' 구글 캘린더 등록.

- 로그인: google_auth.py (token.json)
- 받는 사람: settings.json의 mail_to. 비어 있으면 로그인한 본인에게만 보낸다(시범 운영).
- 캘린더: settings.json의 calendar_id. 비어 있으면 로그인 계정에서 이름이 '세미나·IR'인 캘린더를 찾고,
  없으면 새로 만든 뒤 ID를 저장한다. 팀원 공유는 구글 캘린더 화면에서 한 번 해 둔다.
- 중복 방지: 이벤트 ID를 행사 키(신청 링크+날짜+제목 해시)로 고정. 다시 넣으면 기존 일정을 갱신한다.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_ics import _span, description, event_key
from google_auth import user_email

ROOT = Path(__file__).parent
SETTINGS = ROOT / "settings.json"
DEFAULTS = {"mail_to": [], "calendar_name": "세미나·IR", "calendar_id": ""}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS.exists():
        s.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    return s


def save_settings(s: dict) -> None:
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 메일
def recipients(creds, settings: dict | None = None) -> list[str]:
    s = settings or load_settings()
    return [a.strip() for a in s.get("mail_to", []) if a.strip()] or [user_email(creds)]


def build_message(creds, html: str, ics_path: Path | None, n_events: int, start: datetime, end: datetime,
                  to: list[str] | None = None) -> EmailMessage:
    me = user_email(creds)
    msg = EmailMessage()
    msg["Subject"] = f"[세미나·IR] 주간 일정 {start:%m/%d}~{end:%m/%d} ({n_events}건)"
    msg["From"] = formataddr(("세미나·IR 알림", me))
    msg["To"] = ", ".join(to or recipients(creds))
    msg.set_content("HTML 메일입니다. 메일 프로그램에서 HTML 보기를 켜 주세요.")
    msg.add_alternative(html, subtype="html")
    if ics_path and ics_path.exists():
        msg.add_attachment(ics_path.read_bytes(), maintype="text", subtype="calendar",
                           filename=f"세미나IR_{start:%Y%m%d}.ics")
    return msg


def send_mail(creds, msg: EmailMessage) -> None:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    build("gmail", "v1", credentials=creds, cache_discovery=False).users().messages().send(
        userId="me", body={"raw": raw}).execute()


# ---------------------------------------------------------------- 캘린더
def ensure_calendar(creds) -> str:
    """'세미나·IR' 캘린더 ID. 없으면 만든다."""
    s = load_settings()
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    if s["calendar_id"]:
        return s["calendar_id"]
    page = None
    while True:
        r = svc.calendarList().list(pageToken=page).execute()
        for c in r.get("items", []):
            if c.get("summary") == s["calendar_name"] and c.get("accessRole") in ("owner", "writer"):
                s["calendar_id"] = c["id"]; save_settings(s)
                return c["id"]
        page = r.get("nextPageToken")
        if not page:
            break
    cal = svc.calendars().insert(body={"summary": s["calendar_name"], "timeZone": "Asia/Seoul",
                                       "description": "투자전략팀 세미나·IR 일정 (자동 등록)"}).execute()
    s["calendar_id"] = cal["id"]; save_settings(s)
    return cal["id"]


def gcal_body(ev: dict) -> dict:
    s, e, all_day = _span(ev)
    if all_day:
        when = {"start": {"date": f"{s:%Y-%m-%d}"}, "end": {"date": f"{e:%Y-%m-%d}"}}
    else:
        when = {"start": {"dateTime": s.isoformat(), "timeZone": "Asia/Seoul"},
                "end": {"dateTime": e.isoformat(), "timeZone": "Asia/Seoul"}}
    return {
        "id": event_key(ev),
        "summary": ev["title"],
        "location": "온라인" if ev.get("online") else (ev.get("place") or ""),
        "description": description(ev),
        "source": {"title": ev.get("source") or "신청 링크", "url": ev["url"]},
        "transparency": "transparent",  # 일정이 '바쁨'으로 잡히지 않게
        "status": "confirmed",
        **when,
    }


def sync_calendar(creds, events: list[dict]) -> dict:
    """반환: {'calendar_id', 'created', 'updated', 'failed': [(제목, 오류)]}"""
    cal = ensure_calendar(creds)
    api = build("calendar", "v3", credentials=creds, cache_discovery=False).events()
    res = {"calendar_id": cal, "created": 0, "updated": 0, "failed": []}
    for ev in events:
        body = gcal_body(ev)
        try:
            api.insert(calendarId=cal, body=body).execute()
            res["created"] += 1
        except HttpError as e:
            if e.resp.status == 409:  # 이미 있는 ID → 내용 갱신(삭제됐던 일정도 다시 살아남)
                try:
                    api.update(calendarId=cal, eventId=body["id"], body=body).execute()
                    res["updated"] += 1
                except HttpError as e2:
                    res["failed"].append((ev["title"], str(e2)))
            else:
                res["failed"].append((ev["title"], str(e)))
    return res
