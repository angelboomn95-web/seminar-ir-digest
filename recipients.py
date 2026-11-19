"""메일 받는 사람 목록 — 구글 시트 '세미나·IR 메일 수신자'.

Streamlit Cloud는 서버에 파일을 오래 저장할 수 없고, 저장소는 공개라 이메일 주소를 코드에 넣을 수 없다.
그래서 수신자는 팀과 공유한 구글 시트에 둔다. 시트는 앱에서 처음 한 번 만들 수 있다.

시트 구조
- '수신자' 탭: 받기(TRUE/FALSE) | 이름 | 이메일 | 메모
- '발송기록' 탭: 보낸 시각 | 보낸 사람 | 받는 사람 수 | 제목 | 받는 사람
"""
from __future__ import annotations

import re
from datetime import datetime

from googleapiclient.discovery import build

TITLE = "세미나·IR 메일 수신자"
TAB = "수신자"
LOG_TAB = "발송기록"
HEADER = ["받기", "이름", "이메일", "메모"]
LOG_HEADER = ["보낸 시각", "보낸 사람", "받는 사람 수", "제목", "받는 사람"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _svc(creds):
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def create_sheet(creds) -> str:
    """수신자 시트를 새로 만들고 ID를 돌려준다(로그인한 사람의 드라이브에 생김)."""
    s = _svc(creds)
    sh = s.create(body={"properties": {"title": TITLE},
                        "sheets": [{"properties": {"title": TAB}}, {"properties": {"title": LOG_TAB}}]}).execute()
    sid = sh["spreadsheetId"]
    s.values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
        {"range": f"{TAB}!A1:D1", "values": [HEADER]},
        {"range": f"{LOG_TAB}!A1:E1", "values": [LOG_HEADER]},
    ]}).execute()
    return sid


def _truthy(v) -> bool:
    return str(v).strip().upper() in ("TRUE", "Y", "O", "1", "예", "✓")


def load(creds, sheet_id: str) -> list[dict]:
    rows = _svc(creds).values().get(spreadsheetId=sheet_id, range=f"{TAB}!A2:D").execute().get("values", [])
    out = []
    for r in rows:
        r = (r + ["", "", "", ""])[:4]
        if not any(c.strip() for c in r[1:3]):
            continue
        out.append({"받기": _truthy(r[0]), "이름": r[1].strip(), "이메일": r[2].strip(), "메모": r[3].strip()})
    return out


def save(creds, sheet_id: str, people: list[dict]) -> None:
    s = _svc(creds).values()
    s.clear(spreadsheetId=sheet_id, range=f"{TAB}!A2:D").execute()
    values = [["TRUE" if p.get("받기") else "FALSE", p.get("이름", ""), p.get("이메일", ""), p.get("메모", "")]
              for p in people]
    if values:
        s.update(spreadsheetId=sheet_id, range=f"{TAB}!A2", valueInputOption="RAW", body={"values": values}).execute()


def clean(people: list[dict]) -> tuple[list[dict], list[str]]:
    """빈 줄 제거, 이메일 형식·중복 확인. 반환: (정리된 목록, 문제 설명)"""
    out, problems, seen = [], [], set()
    for p in people:
        email = str(p.get("이메일") or "").strip()
        name = str(p.get("이름") or "").strip()
        if not email and not name:
            continue
        if not EMAIL_RE.match(email):
            problems.append(f"'{name or email}'의 이메일 형식이 올바르지 않습니다")
            continue
        if email.lower() in seen:
            problems.append(f"{email}이(가) 두 번 들어 있어 하나만 남겼습니다")
            continue
        seen.add(email.lower())
        out.append({"받기": bool(p.get("받기")), "이름": name, "이메일": email,
                    "메모": str(p.get("메모") or "").strip()})
    return out, problems


def active(people: list[dict]) -> list[dict]:
    return [p for p in people if p["받기"] and EMAIL_RE.match(p["이메일"])]


def log_send(creds, sheet_id: str, sender: str, to: list[str], subject: str) -> None:
    try:
        _svc(creds).values().append(
            spreadsheetId=sheet_id, range=f"{LOG_TAB}!A1", valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[datetime.now().strftime("%Y-%m-%d %H:%M"), sender, len(to), subject, ", ".join(to)]]},
        ).execute()
    except Exception:
        pass  # 기록 탭이 없거나 권한이 없어도 발송 자체는 성공으로 본다


def load_log(creds, sheet_id: str, n: int = 10) -> list[list[str]]:
    try:
        rows = _svc(creds).values().get(spreadsheetId=sheet_id, range=f"{LOG_TAB}!A2:E").execute().get("values", [])
        return rows[-n:][::-1]
    except Exception:
        return []


# ---------------------------------------------------------------- 필터 규칙('필터 규칙' 탭)
# 한 줄에 하나: 구분 | 값.  구분은 아래 다섯 가지. 시트에서 직접 줄을 추가·삭제해도 된다.
RULES_TAB = "필터 규칙"
RULE_KINDS = {"플랫폼 검색어": "search", "포함 키워드": "include", "제외 키워드": "exclude", "지역": "regions",
              "기간(주)": "weeks"}


def _tabs(creds, sheet_id: str) -> list[str]:
    meta = _svc(creds).get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def load_rules(creds, sheet_id: str) -> dict | None:
    """시트에 저장된 필터 규칙. 탭이 없거나 비어 있으면 None(→ config.json 기본값을 쓴다)."""
    try:
        rows = _svc(creds).values().get(spreadsheetId=sheet_id, range=f"{RULES_TAB}!A2:B").execute().get("values", [])
    except Exception:
        return None
    out = {"search": [], "include": [], "exclude": [], "regions": []}
    for r in rows:
        if len(r) < 2 or not r[1].strip():
            continue
        key = RULE_KINDS.get(r[0].strip())
        if key == "weeks":
            try:
                out["weeks"] = max(1, min(8, int(float(r[1]))))
            except ValueError:
                pass
        elif key:
            if r[1].strip() not in out[key]:
                out[key].append(r[1].strip())
    out = {k: v for k, v in out.items() if v}  # 비어 있는 항목은 기본값(config.json)을 쓴다
    return out if out.get("include") else None


def save_rules(creds, sheet_id: str, rules: dict) -> None:
    s = _svc(creds)
    if RULES_TAB not in _tabs(creds, sheet_id):
        s.batchUpdate(spreadsheetId=sheet_id,
                      body={"requests": [{"addSheet": {"properties": {"title": RULES_TAB}}}]}).execute()
    rows = [["구분", "값"]]
    for label, key in RULE_KINDS.items():
        vals = [rules.get(key)] if key == "weeks" else rules.get(key, [])
        rows += [[label, str(v)] for v in vals if str(v).strip()]
    s.values().clear(spreadsheetId=sheet_id, range=f"{RULES_TAB}!A:B").execute()
    s.values().update(spreadsheetId=sheet_id, range=f"{RULES_TAB}!A1", valueInputOption="RAW",
                      body={"values": rows}).execute()


# ---------------------------------------------------------------- 수집 사이트('수집 사이트' 탭)
SITES_TAB = "수집 사이트"
SITES_HEADER = ["사이트", "주소", "구분"]


def load_sites(creds, sheet_id: str) -> list[dict] | None:
    """시트에 저장된 수집 사이트. 탭이 없거나 비어 있으면 None(→ config.json 기본값)."""
    try:
        rows = _svc(creds).values().get(spreadsheetId=sheet_id, range=f"{SITES_TAB}!A2:C").execute().get("values", [])
    except Exception:
        return None
    out = []
    for r in rows:
        r = (r + ["", "", ""])[:3]
        if r[0].strip() and r[1].strip():
            out.append({"name": r[0].strip(), "url": r[1].strip(), "category": r[2].strip()})
    return out or None


def save_sites(creds, sheet_id: str, sites: list[dict]) -> None:
    s = _svc(creds)
    if SITES_TAB not in _tabs(creds, sheet_id):
        s.batchUpdate(spreadsheetId=sheet_id,
                      body={"requests": [{"addSheet": {"properties": {"title": SITES_TAB}}}]}).execute()
    rows = [SITES_HEADER] + [[x["name"], x["url"], x.get("category", "")] for x in sites]
    s.values().clear(spreadsheetId=sheet_id, range=f"{SITES_TAB}!A:C").execute()
    s.values().update(spreadsheetId=sheet_id, range=f"{SITES_TAB}!A1", valueInputOption="RAW",
                      body={"values": rows}).execute()
