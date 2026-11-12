"""행사 목록 → iCalendar(.ics) 파일 / 구글 캘린더 '일정 추가' 링크.

- UID는 (신청 링크 + 시작일)로 고정한다. 같은 파일을 다시 가져와도 구글 캘린더가 같은 일정으로 보고 갱신하므로
  주마다 새 .ics를 '세미나·IR' 공유 캘린더에 가져와도 중복 등록되지 않는다.
- 시간이 없는 행사는 종일 일정으로 넣는다.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


def _d(iso):
    return datetime.fromisoformat(iso) if iso else None


def event_key(ev: dict) -> str:
    """행사 고유 키(20자리 16진수). .ics UID와 구글 캘린더 이벤트 ID에 같이 쓴다.
    구글 이벤트 ID 허용 문자(0-9, a-v)에 16진수가 포함되므로 그대로 쓸 수 있다."""
    key = f"{ev['url']}|{ev['start'][:10]}|{ev['title'][:40]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def uid(ev: dict) -> str:
    return event_key(ev) + "@seminar-ir.dcamp"


def description(ev: dict) -> str:
    place = "온라인" if ev.get("online") else (ev.get("place") or "장소 확인 필요")
    lines = [
        ev.get("summary") or "",
        f"주최: {ev.get('host') or ev['source']}",
        f"장소: {place}",
        f"비용: {ev.get('price') or '확인 필요'}",
    ]
    if ev.get("deadline"):
        lines.append(f"신청 마감: {_d(ev['deadline']):%Y-%m-%d %H:%M}")
    lines.append(f"신청 링크: {ev['url']}")
    lines.append(f"(출처: {ev['source']})")
    return "\n".join(l for l in lines if l)


def _span(ev: dict):
    """(start, end, all_day). 시간 있는 행사는 종료시각 없으면 2시간으로 둔다.
    여러 날 행사(예: 11/30~12/8 오피스아워)도 첫날 일정으로만 넣는다(캘린더가 몇 주씩 막히지 않게)."""
    s = _d(ev["start"])
    if ev.get("has_time"):
        e = _d(ev.get("end"))
        if not e or e <= s or e.date() != s.date():
            e = s + timedelta(hours=2)
        return s, e, False
    return s, s + timedelta(days=1), True


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: 75바이트마다 줄 접기."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        c = ch.encode("utf-8")
        if len(cur) + len(c) > (75 if not out else 74):
            out.append(cur.decode("utf-8")); cur = b""
        cur += c
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def to_ics(events: list[dict], cal_name: str = "세미나·IR") -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//dcamp//seminar-ir-digest//KO", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(cal_name)}", "X-WR-TIMEZONE:Asia/Seoul"]
    for ev in events:
        s, e, all_day = _span(ev)
        place = "온라인" if ev.get("online") else (ev.get("place") or "")
        L += ["BEGIN:VEVENT", f"UID:{uid(ev)}", f"DTSTAMP:{now}"]
        if all_day:
            L += [f"DTSTART;VALUE=DATE:{s:%Y%m%d}", f"DTEND;VALUE=DATE:{e:%Y%m%d}"]
        else:
            L += [f"DTSTART:{s.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTEND:{e.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"]
        L += [f"SUMMARY:{_esc(ev['title'])}", f"LOCATION:{_esc(place)}",
              f"DESCRIPTION:{_esc(description(ev))}", f"URL:{ev['url']}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in L) + "\r\n"


def gcal_link(ev: dict) -> str:
    """구글 캘린더 '일정 만들기' 화면을 미리 채운 링크(받는 사람이 눌러 자기 캘린더에 추가)."""
    s, e, all_day = _span(ev)
    if all_day:
        dates = f"{s:%Y%m%d}/{e:%Y%m%d}"
    else:
        dates = f"{s.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}/{e.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    q = {"action": "TEMPLATE", "text": ev["title"], "dates": dates, "details": description(ev),
         "location": "온라인" if ev.get("online") else (ev.get("place") or ""), "ctz": "Asia/Seoul"}
    return "https://calendar.google.com/calendar/render?" + urlencode(q)
