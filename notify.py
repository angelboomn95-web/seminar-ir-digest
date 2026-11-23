"""주간 메일 발송(로그인한 Gmail 계정) + '세미나·IR' 구글 캘린더 등록.

- 캘린더: 로그인 계정에서 이름이 '세미나·IR'인 캘린더를 찾고, 없으면 새로 만든다.
  팀원 공유는 구글 캘린더 화면에서 한 번 해 둔다.
- 중복 방지: 이벤트 ID를 행사 키(신청 링크+날짜+제목 해시)로 고정. 다시 넣으면 기존 일정을 갱신한다.
- 정리: 이번 수집에 없는 앞으로의 일정(이 앱이 넣은 것만)은 지운다. 지난 일정과 사람이 직접 넣은 일정은 그대로 둔다.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_ics import _span, description, event_key

CALENDAR_NAME = "세미나·IR"
APP_TAG = "seminar-ir-digest"
KEY_RE = re.compile(r"^[0-9a-f]{20}$")  # event_key 형식(이 앱이 넣은 일정)


# ---------------------------------------------------------------- 메일
def subject_for(n_events: int, start: datetime, end: datetime) -> str:
    return f"[세미나·IR] 주간 일정 {start:%m/%d}~{end:%m/%d} ({n_events}건)"


def build_message(sender: str, to: list[str], html: str, ics: str | None, subject: str,
                  ics_name: str = "세미나IR.ics") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("세미나·IR 알림", sender))
    msg["To"] = ", ".join(to)
    msg.set_content("HTML 메일입니다. 메일 프로그램에서 HTML 보기를 켜 주세요.")
    msg.add_alternative(html, subtype="html")
    if ics:
        msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar", filename=ics_name)
    return msg


def send_mail(creds, msg: EmailMessage) -> None:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    build("gmail", "v1", credentials=creds, cache_discovery=False).users().messages().send(
        userId="me", body={"raw": raw}).execute()


# ---------------------------------------------------------------- 캘린더
def ensure_calendar(creds, calendar_id: str | None = None) -> str:
    """'세미나·IR' 캘린더 ID. calendar_id를 주면 그대로 쓰고, 없으면 찾거나 만든다."""
    if calendar_id:
        return calendar_id
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    page = None
    while True:
        r = svc.calendarList().list(pageToken=page).execute()
        for c in r.get("items", []):
            if c.get("summary") == CALENDAR_NAME and c.get("accessRole") in ("owner", "writer"):
                return c["id"]
        page = r.get("nextPageToken")
        if not page:
            break
    cal = svc.calendars().insert(body={"summary": CALENDAR_NAME, "timeZone": "Asia/Seoul",
                                       "description": "투자전략팀 세미나·IR 일정 (자동 등록)"}).execute()
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
        "extendedProperties": {"private": {"source": APP_TAG, "site": (ev.get("source") or "")[:100]}},
        **when,
    }


def _ours(item: dict) -> bool:
    tag = ((item.get("extendedProperties") or {}).get("private") or {}).get("source")
    return tag == APP_TAG or bool(KEY_RE.match(item.get("id", "")))


def sync_calendar(creds, events: list[dict], calendar_id: str | None = None,
                  prune_from: datetime | None = None, keep_sites: set[str] | None = None) -> dict:
    """캘린더를 이번 수집 결과와 맞춘다.
    prune_from을 주면 그 시각 이후 일정 중 이번 목록에 없는 것(이 앱이 넣은 것만)을 지운다.
    keep_sites(이번에 수집 오류가 난 사이트)에서 온 일정은 목록에 없어도 지우지 않는다.
    반환: {'calendar_id', 'created', 'updated', 'deleted', 'failed': [(제목, 오류)]}"""
    cal = ensure_calendar(creds, calendar_id)
    api = build("calendar", "v3", credentials=creds, cache_discovery=False).events()
    res = {"calendar_id": cal, "created": 0, "updated": 0, "deleted": 0, "failed": []}
    keep = set()
    for ev in events:
        body = gcal_body(ev)
        keep.add(body["id"])
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

    # 수집이 통째로 실패해 0건일 때 캘린더가 비지 않도록, 행사가 있을 때만 정리한다
    if prune_from is not None and events:
        page = None
        while True:
            r = api.list(calendarId=cal, timeMin=prune_from.isoformat(), singleEvents=True,
                         maxResults=250, pageToken=page).execute()
            for it in r.get("items", []):
                if it.get("status") == "cancelled" or it["id"] in keep or not _ours(it):
                    continue
                site = ((it.get("extendedProperties") or {}).get("private") or {}).get("site")
                if keep_sites and site in keep_sites:
                    continue
                try:
                    api.delete(calendarId=cal, eventId=it["id"]).execute()
                    res["deleted"] += 1
                except HttpError as e:
                    res["failed"].append((it.get("summary", it["id"]), str(e)))
            page = r.get("nextPageToken")
            if not page:
                break
    return res
