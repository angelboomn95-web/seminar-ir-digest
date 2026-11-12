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


def run(today: datetime):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    rules = cfg["rules"]
    weeks = int(rules.get("weeks", 4))
    win_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    win_end = win_start + timedelta(days=7 * weeks)
    regions = [r for r in rules.get("regions", []) if r != "온라인"]
    allow_online = "온라인" in rules.get("regions", [])

    notice_from = win_start - timedelta(days=NOTICE_DAYS)
    report, kept, notices, seen = [], [], [], set()
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
                if ev.posted and ev.posted >= notice_from and kw_hit(txt, include) and not kw_hit(txt, exclude):
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
            text = " ".join([ev.title, ev.summary, ev.host, " ".join(ev.tags)])
            ex = kw_hit(" ".join([ev.title, ev.summary, " ".join(ev.tags)]), exclude)
            if ex:
                drop(f"제외 키워드({ex})"); continue
            inc = kw_hit(text, include)
            if not inc:
                drop("포함 키워드 없음"); continue
            key = (norm_title(ev.title)[:40], ev.start.date())
            if key in seen:
                drop("중복"); continue
            seen.add(key)
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
