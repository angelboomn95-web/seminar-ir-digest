"""세미나·IR 일정 수집 → 필터 → 주간 메일 HTML 생성.

사용법:
    python collect.py                 # config.json 기준으로 수집, out/ 에 결과 저장
    python collect.py --today 2026-11-16   # 기준일 지정(시범용)

결과:
    out/events_YYYYMMDD.json   필터 통과 행사
    out/report_YYYYMMDD.json   사이트별 수집/통과 건수, 오류
    out/mail_YYYYMMDD.html     주간 메일 본문(미리보기)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from sources import KST, collector_for
from render import render_mail
from calendar_ics import event_key, to_ics

ROOT = Path(__file__).parent
OUT = ROOT / "out"


def kw_hit(text: str, kws: list[str]) -> str | None:
    """키워드 중 처음 걸리는 것. 영문 키워드(AI, IR, VC 등)는 단어 경계로 비교해 'FIRST' 같은 오탐을 막는다."""
    low = text.lower()
    for k in kws:
        k2 = k.strip()
        if not k2:
            continue
        if re.fullmatch(r"[A-Za-z&]+", k2):
            if re.search(rf"(?<![A-Za-z]){re.escape(k2.lower())}(?![A-Za-z])", low):
                return k2
        elif k2.lower() in low:
            return k2
    return None


OTHER_REGIONS = ["부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "충청",
                 "전북", "전남", "전라", "경북", "경남", "경상", "제주", "성남", "판교", "수원", "용인", "고양",
                 "송도", "창원", "포항", "천안", "청주", "전주", "아산", "대경권", "일본", "도쿄", "싱가포르", "미국",
                 "중국", "베트남", "부에노스아이레스", "해외", "Tokyo", "Singapore", "Japan", "Busan"]


def region_ok(place: str, title: str, regions: list[str]) -> bool:
    """장소에 서울 등 허용 지역이 있으면 통과. 지역명이 없으면(예: '삼정호텔') 다른 지역이 명시된 경우만 제외."""
    if not regions:
        return True
    if any(r in place for r in regions):
        return True
    text = place + " " + title
    return not any(o in text for o in OTHER_REGIONS)


NOTICE_DAYS = 14  # 행사일을 못 찾은 공지는 최근 2주 안에 올라온 것만 메일에 싣는다


def norm_title(t: str) -> str:
    return re.sub(r"[\s\[\]()<>「」『』〈〉【】\"'·,.:|/-]", "", t).lower()


# 창업자·기업 대상 참가자 모집 공고(행사를 보러 가는 자리가 아님). '청중평가단 모집', '참관 신청'은 남긴다.
RECRUIT_RE = re.compile(
    r"(참가|참여|입주|입점|지원|육성)\s*(기업|팀|스타트업|기관|사)\s*(모집|선발|공모)"
    r"|\d+\s*기\s*(모집|선발)|공개\s*모집|(기업|팀|스타트업)\s*(모집|선발)")
AUDIENCE_RE = re.compile(r"청중|참관|관람|방청|사전\s*등록")


def is_recruit(title: str) -> bool:
    return bool(RECRUIT_RE.search(title)) and not AUDIENCE_RE.search(title)


def deadline_passed(ev, today: datetime) -> bool:
    """신청 마감이 이미 지난 공고. 마감 필드가 있으면 그것을, 없으면 제목의 '(~8/2)', '(~6.18)' 표기를 본다."""
    if ev.deadline:
        return ev.deadline < today
    m = re.search(r"~\s*(\d{1,2})\s*[./]\s*(\d{1,2})(?!\d)", ev.title)
    if not m:
        return False
    mo, d = int(m.group(1)), int(m.group(2))
    try:
        dl = today.replace(month=mo, day=d, hour=23, minute=59)
    except ValueError:
        return False
    return dl < today and (today - dl).days < 240  # 연말에 내년 1~2월 마감을 지난 것으로 보지 않게


def _core(t: str) -> str:
    """중복 비교용 제목: 앞쪽 [주최]·(기관) 꼬리표, 연도, '참관 신청' 같은 꼬리말을 뗀다."""
    t = re.sub(r"^\s*([\[(【「][^\])】」]{0,30}[\])】」]\s*)+", "", t)
    t = re.sub(r"\b20\d\d\b|참관\s*신청|참가\s*신청|개최\s*안내|안내", "", t)
    return norm_title(t)


def same_event(a: str, b: str, same_time: bool = False) -> bool:
    """같은 날 열리는 두 공고가 같은 행사인지. 시작 시각까지 같으면(사이트마다 제목을 다르게 단 경우) 기준을 낮춘다."""
    a, b = _core(a), _core(b)
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 6 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= (0.5 if same_time else 0.72)


def run(today: datetime):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    rules = cfg["rules"]
    weeks = int(rules.get("weeks", 4))
    win_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    win_end = win_start + timedelta(days=7 * weeks)
    regions = [r for r in rules.get("regions", []) if r != "온라인"]
    allow_online = "온라인" in rules.get("regions", [])

    notice_from = win_start - timedelta(days=NOTICE_DAYS)
    report, kept, notices, seen = [], [], [], []
    for site in cfg["sites"]:
        if site.get("status") == "제외":
            continue
        c = collector_for(site["url"])
        row = {"site": site["name"], "collected": 0, "kept": 0, "dropped": {}, "error": None}
        if not c:
            row["error"] = "수집기 없음(수동 확인 대상)"
            report.append(row)
            continue
        fn, uses_kw = c
        include = list(dict.fromkeys(rules.get("include", []) + site.get("include", [])))
        exclude = list(dict.fromkeys(rules.get("exclude", []) + site.get("exclude", [])))
        search_kws = (site.get("include") or rules.get("include", [])) if uses_kw else []
        try:
            events = fn(site, search_kws)
        except Exception as e:  # 한 사이트 오류가 전체를 멈추지 않게
            row["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc(file=sys.stderr)
            report.append(row)
            continue
        row["collected"] = len(events)

        def drop(reason):
            row["dropped"][reason] = row["dropped"].get(reason, 0) + 1

        for ev in events:
            if not ev.start:
                # 게시판 공지 중 최근 글이고 키워드가 맞으면 '날짜 확인 필요'로 메일 하단에 따로 싣는다
                txt = " ".join([ev.title, ev.summary])
                if ev.posted and ev.posted >= notice_from and kw_hit(txt, include) and not kw_hit(txt, exclude) \
                        and not is_recruit(ev.title):
                    d = ev.to_dict(); d["matched"] = kw_hit(txt, include)
                    notices.append(d); drop("날짜 미확인(공지로 표시)")
                else:
                    drop("날짜 미확인")
                continue
            if not (win_start <= ev.start < win_end):
                drop("기간 밖"); continue
            if ev.online:
                if not allow_online:
                    drop("지역"); continue
            elif not region_ok(ev.place, ev.title, regions):
                drop("지역"); continue
            # 행사 플랫폼(키워드 검색형)은 등록자가 단 태그·요약이 넓어서 포함·제외 모두 제목으로만 판단한다
            ex = kw_hit(ev.title if uses_kw else " ".join([ev.title, ev.summary, " ".join(ev.tags)]), exclude)
            if ex:
                drop(f"제외 키워드({ex})"); continue
            if is_recruit(ev.title):
                drop("참가자 모집 공고"); continue
            if deadline_passed(ev, today):
                drop("신청 마감 지남"); continue
            text =ev.title if uses_kw else " ".join([ev.title, ev.summary, ev.host, " ".join(ev.tags)])
            inc = kw_hit(text, include)
            if not inc:
                drop("포함 키워드 없음"); continue
            day = ev.start.date()
            timed = ev.start.hour or ev.start.minute
            if any(d == day and same_event(t, ev.title, bool(timed) and s == ev.start) for t, d, s in seen):
                drop("중복"); continue
            seen.append((ev.title, day, ev.start))
            d = ev.to_dict()
            d["matched"] = inc
            kept.append(d)
            row["kept"] += 1
        report.append(row)

    kept.sort(key=lambda d: d["start"])
    OUT.mkdir(exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    (OUT / f"events_{stamp}.json").write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / f"report_{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    notices.sort(key=lambda d: d["posted"], reverse=True)
    html = render_mail(kept, today, win_start, win_end - timedelta(days=1), report, notices)
    (OUT / f"mail_{stamp}.html").write_text(html, encoding="utf-8")
    (OUT / f"events_{stamp}.ics").write_text(to_ics(kept), encoding="utf-8", newline="")
    write_upload(stamp, today, win_start, win_end - timedelta(days=1), kept, notices, report)
    return kept, report


def _slim(d: dict) -> dict:
    d = {k: v for k, v in d.items() if k != "tags"}
    if d.get("summary"):
        d["summary"] = d["summary"][:200]
    return d


def write_upload(stamp, today, start, end, kept, notices, report) -> Path:
    """'세미나·IR' 페이지의 '수집 결과 올리기'에 넣는 묶음 파일 (행사·공지·사이트별 결과)."""
    bundle = {
        "kind": "seminar-ir-upload", "version": 1, "stamp": stamp,
        "today": today.isoformat(), "winStart": start.isoformat(), "winEnd": end.isoformat(),
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "events": [{**_slim(e), "key": event_key(e)} for e in kept],
        "notices": [_slim(n) for n in notices],
        "report": report,
    }
    path = OUT / f"upload_{stamp}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", help="기준일 YYYY-MM-DD (기본: 오늘)")
    a = ap.parse_args()
    today = datetime.strptime(a.today, "%Y-%m-%d").replace(tzinfo=KST) if a.today else datetime.now(KST)
    kept, report = run(today)
    if sys.stdout:
        for r in report:
            print(f"{r['site']:<22} 수집 {r['collected']:>3}  통과 {r['kept']:>3}  {r['error'] or ''}  {r['dropped'] or ''}")
        print(f"총 {len(kept)}건 → 페이지에 올릴 파일: out/upload_{today:%Y%m%d}.json")


if __name__ == "__main__":
    main()
