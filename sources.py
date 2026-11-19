"""사이트별 행사 수집기.

각 수집기는 Event 목록을 돌려준다. 날짜·지역·키워드 필터는 collect.py에서 공통으로 적용한다.
사이트 구조가 바뀌면 이 파일의 해당 함수만 고치면 된다.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
TIMEOUT = 15


@dataclass
class Event:
    source: str            # 수집 사이트명
    title: str
    url: str
    start: datetime | None  # KST, 시간 모르면 00:00
    end: datetime | None = None
    has_time: bool = True
    host: str = ""
    place: str = ""
    online: bool = False
    price: str = ""         # "무료" / "유료(10,000원)" / ""
    deadline: datetime | None = None
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    posted: datetime | None = None  # 게시판형 사이트의 게시일. 행사일을 못 찾은 공지를 '날짜 확인 필요'로 보여줄 때 씀

    def to_dict(self):
        d = asdict(self)
        for k in ("start", "end", "deadline", "posted"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def _get(url, **kw):
    r = S.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


_KIRS_CA = None


def _get_kirs(url):
    """한국IR협의회 서버는 중간 인증서를 보내지 않아 파이썬 기본 검증이 실패한다(브라우저는 자동 보완).
    빠진 GlobalSign 중간 인증서(certs/, 2027-05 만료)를 기본 인증서 묶음에 더해 정상 검증한다.
    만료 후 오류가 나면 http://secure.globalsign.com/cacert/ 에서 새 중간 인증서를 받아 교체."""
    global _KIRS_CA
    if _KIRS_CA is None:
        import certifi
        import tempfile
        from pathlib import Path
        extra = (Path(__file__).parent / "certs" / "globalsign_alphassl_2025.pem").read_text()
        f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        f.write(Path(certifi.where()).read_text() + chr(10) + extra)
        f.close()
        _KIRS_CA = f.name
    return _get(url, verify=_KIRS_CA)


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _lines(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    return [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]


_KDATE = re.compile(r"(20\d{2})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?")
_KTIME = re.compile(r"(오전|오후|AM|PM|am|pm)?\s*(\d{1,2})\s*(?::|시)\s*(\d{2})?")


def parse_kdatetime(s: str, default_year: int | None = None):
    """'2026년 11월 18일(수) 오전 9시', '2026.11.14 (토) 14:00' 등 → (datetime, has_time)."""
    m = _KDATE.search(s)
    if not m:
        return None, False
    y, mo, d = map(int, m.groups())
    rest = s[m.end():]
    rest = re.sub(r"^\s*\([^)]*\)", "", rest)  # 요일
    t = _KTIME.search(rest[:25])
    hh = mm = 0
    has_time = False
    if t and t.group(2):
        hh = int(t.group(2)); mm = int(t.group(3) or 0)
        ap = (t.group(1) or "").lower()
        if ap in ("오후", "pm") and hh < 12:
            hh += 12
        if ap in ("오전", "am") and hh == 12:
            hh = 0
        has_time = hh < 24 and mm < 60
        if not has_time:
            hh = mm = 0
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=KST), has_time
    except ValueError:
        return None, False


_SDATE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:/|\.|월)\s*(\d{1,2})\s*일?\.?(?!\d)")


def parse_flex(s: str, ref: datetime | None = None):
    """연도가 있으면 parse_kdatetime, 없으면 '12/4(금), 9:00' '11월 20일 오후 2시' 형태를 기준일 근처 연도로 해석."""
    dt, ht = parse_kdatetime(s)
    if dt:
        return dt, ht
    ref = ref or datetime.now(KST)
    m = _SDATE.search(s)
    if not m:
        return None, False
    mo, d = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None, False
    y = ref.year + (1 if mo < ref.month - 6 else 0)
    return parse_kdatetime(f"{y}.{mo}.{d} " + s[m.end():])


# ---------------------------------------------------------------- 온오프믹스
def onoffmix(site, keywords, pages=2):
    out, seen = [], set()
    for kw in keywords:
        for p in range(1, pages + 1):
            url = f"https://onoffmix.com/event/main?s={quote(kw)}&page={p}"
            soup = BeautifulSoup(_get(url), "html.parser")
            arts = soup.select("article.event_area")
            if not arts:
                break
            for a in arts:
                link = a.find("a", href=re.compile(r"/event/\d+"))
                if not link:
                    continue
                href = urljoin("https://onoffmix.com", link["href"])
                if href in seen:
                    continue
                seen.add(href)
                title = (a.select_one("h5.title") or {}).get("title") or _text(a.select_one("h5.title"))
                when = _text(a.select_one(".list_date_place .date"))
                place = _text(a.select_one(".list_date_place .place"))
                start, has_time = parse_kdatetime(when)
                pay = a.select_one(".payment_type")
                price = "무료" if pay and "free" in (pay.get("class") or []) else ("유료" if pay else "")
                tags = [t.get_text(strip=True).lstrip("#") for t in a.select(".list_event_tags .tag")]
                cat = _text(a.select_one(".category_type"))
                out.append(Event(site["name"], html.unescape(title), href, start, has_time=has_time,
                                 place=place, online="온라인" in place, price=price,
                                 summary=cat, tags=tags))
    return out


# ---------------------------------------------------------------- 이벤터스
def eventus(site, keywords, size=30):
    out, seen = [], set()
    api = "https://api.event-us.kr/api/v1/engine/search"
    hdr = {"Origin": "https://event-us.kr", "Referer": "https://event-us.kr/search"}
    for kw in keywords:
        body = {"query": kw, "page": {"current": 1, "size": size},
                "filters": {"all": [{"state": "Start"}, {"disclosure_status": "open"}, {"is_ignore": "false"}]},
                "boosts": {}, "sort": []}
        r = S.post(api, json=body, headers=hdr, timeout=TIMEOUT)
        r.raise_for_status()
        for res in r.json().get("results", []):
            g = lambda k: (res.get(k) or {}).get("raw") if isinstance(res.get(k), dict) else res.get(k)
            eid = g("id")
            if eid in seen:
                continue
            seen.add(eid)

            def utc2kst(s):
                if not s:
                    return None
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)

            online = g("event_system_type") == "online"
            money = g("min_money") or 0
            price = "무료" if not money else f"유료({int(money):,}원~)"
            place = "온라인" if online else " ".join(x for x in [g("address") or g("area_detail") or "", g("place") or ""] if x)
            desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", g("description") or ""))[:120]
            out.append(Event(site["name"], g("title") or "", f"https://event-us.kr/{g('subdomain')}/event/{eid}",
                             utc2kst(g("start_date")), utc2kst(g("close_date")), True,
                             host=g("app_title") or "", place=place.strip(), online=online, price=price,
                             deadline=utc2kst(g("register_due_date")), summary=desc,
                             tags=list(g("tags") or []) + [x for x in [g("event_type"), g("category"), g("category2")] if x]))
    return out


# ---------------------------------------------------------------- 한국벤처캐피탈협회 (공지·외부행사 게시판)
def kvca(site, keywords, max_posts=25):
    base = "https://www.kvca.or.kr"
    lst = f"{base}/Program/board/list.html?a_gb=board&a_cd=5&a_item=0&sm=3_1"
    soup = BeautifulSoup(_get(lst), "html.parser")
    links = []
    for a in soup.find_all("a", href=re.compile(r"listbody\.html.*po_no=\d+")):
        t = _text(a)
        if t and a["href"] not in [l[1] for l in links]:
            links.append((t, a["href"]))
    out = []
    for title, href in links[:max_posts]:
        url = urljoin(lst, href)
        try:
            L = _lines(_get(url))
        except Exception:
            continue
        # 본문 구조: ... 제목 / <제목> / 구분 / ... / 조회수 / <n> / 본문... / 첨부파일
        full_title, body = title, []
        if "제목" in L:
            k = L.index("제목")
            full_title = L[k + 1] if k + 1 < len(L) else title
        if "조회수" in L:
            k = L.index("조회수") + 2
            body = L[k:]
            for j, l in enumerate(body):
                if l.startswith("첨부파일") or l.startswith("이전글"):
                    body = body[:j]
                    break
        start, has_time, place, deadline, summary = scan_body(body, full_title)
        out.append(Event(site["name"], full_title, url, start, has_time=has_time, host="한국벤처캐피탈협회",
                         place=place[:80], online="온라인" in place or "온라인" in full_title or "웨비나" in full_title,
                         deadline=deadline, summary=summary[:120]))
    return out


_FIELD = r"[\s\-·•○▶■□*※>]*"


def scan_body(body: list[str], title: str):
    """게시판 본문 줄에서 '일시: / 장소: / 신청기간:' 항목을 찾는다. → (start, has_time, place, deadline, summary)"""
    start = None; has_time = False; place = ""; deadline = None
    for l in body:
        if start is None and re.match(_FIELD + r"(행사\s*|교육\s*|개최\s*)?(일\s*시|일\s*정)\s*[:：]", l):
            start, has_time = parse_flex(l.split(":", 1)[-1].split("：", 1)[-1])
        if not place and re.match(_FIELD + r"(행사\s*|교육\s*)?장\s*소\s*[:：]", l):
            place = re.split(r"[:：]", l, 1)[1].strip()
        if deadline is None and re.match(_FIELD + r"(신청|접수)\s*(기간|마감)\s*[:：]", l):
            ds = list(_SDATE.finditer(l))
            if ds:
                deadline, _ = parse_flex(l[ds[-1].start():])
    if start is None:  # 본문 첫 문장·제목의 날짜 (예: 12월 4일(금), 11/20)
        for s_ in [*body[:3], title]:
            start, has_time = parse_flex(s_)
            if start:
                break
    summary = next((l for l in body if len(l) > 20 and not re.match(_FIELD + r"[가-힣\s]{2,6}[:：]", l)), "")
    return start, has_time, place, deadline, summary


# ---------------------------------------------------------------- 한국벤처투자 (기타공지)
def kvic(site, keywords, days=45):
    """기타공지 게시판. 최근 days일 안에 올라온 글만 본문을 연다. 행사일이 첨부파일에만 있는 글이 많아
    날짜를 못 찾으면 게시일(posted)만 채워 '날짜 확인 필요' 공지로 넘긴다."""
    lst = "https://www.kvic.or.kr/notice/kvic-notice/other-notice"
    t = _get(lst)
    rows = re.findall(r"board_view\((\d+)\)[^>]*>\s*([^<]*?)\s*</a>\s*</td>\s*<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>", t)
    cutoff = datetime.now(KST) - timedelta(days=days)
    out = []
    for pid, title, posted in rows:
        title = html.unescape(re.sub(r"<[^>]+>|\s+", " ", title)).strip()
        pdt = datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=KST)
        if pdt < cutoff:
            continue
        url = f"{lst}?pageNo=1&searchType=all&searchWord=&id={pid}"
        body = []
        try:
            L = _lines(_get(url))
            if "작성일자" in L:
                body = L[L.index("작성일자") + 2:]
                body = body[:next((j for j, l in enumerate(body) if l == "하단"), len(body))]
                body = [l for l in body if l not in ("바로보기", "내려받기", "첨부파일")]
        except Exception:
            pass
        start, has_time, place, deadline, summary = scan_body(body, title)
        out.append(Event(site["name"], title, url, start, has_time=has_time, host="한국벤처투자",
                         place=place[:80], online="온라인" in place, deadline=deadline,
                         summary=summary[:120], posted=pdt))
    return out


# ---------------------------------------------------------------- 디캠프 (모집 중 프로그램)
def dcamp(site, keywords):
    """dcamp.kr 상단 메뉴에 걸린 '모집중' 프로그램 상세(/program/<id>)를 읽는다. 목록 API가 없어 메뉴 링크가 기준."""
    base = "https://dcamp.kr"
    ids = set()
    for p in ("", "/program/officehour", "/program/dday", "/program/investment"):
        try:
            ids |= set(re.findall(r"/program/(\d{5,})", _get(base + p)))
        except Exception:
            pass
    out = []
    for pid in sorted(ids, reverse=True):
        url = f"{base}/program/{pid}"
        try:
            L = _lines(_get(url))
        except Exception:
            continue
        if "Share" not in L:
            continue
        k = L.index("Share")
        title = L[k - 1]
        get_after = lambda key: L[L.index(key, k) + 1] if key in L[k:] else ""
        apply_rng, when, place = get_after("신청"), get_after("일시"), get_after("장소")
        tags = []
        for l in L[k + 3:]:
            if l == "신청":
                break
            tags.append(l.lstrip("#"))
        start, has_time = parse_kdatetime(when)
        end = None
        if "~" in when:
            end, _ = parse_kdatetime(when.split("~", 1)[1])
        deadline = parse_kdatetime(apply_rng.split("~", 1)[1])[0] if "~" in apply_rng else None
        intro = [i for i, l in enumerate(L) if l == "프로그램 소개"]
        summary = ""
        if intro:
            summary = next((l for l in L[intro[-1] + 1:intro[-1] + 30]
                            if len(l) > 20 and not re.search(r"(로|길|대로)\s*\d+", l)), "")  # 주소 줄 건너뜀
        out.append(Event(site["name"], title, url, start, end, has_time, host="디캠프", place=place[:80],
                         online="온라인" in place, price="무료", deadline=deadline,
                         summary=summary[:120], tags=tags))
    return out


# ---------------------------------------------------------------- KIND 상장사 IR 일정
KIND_KEEP = re.compile(r"세미나|탐방|기업설명회|설명회|상장|공모|IPO|합병|공개매수|분할|M&A|인수")
KIND_DROP = re.compile(r"실적|결산|NDR|Non-Deal|Corporate Day|Corp-day|Conference|Summit|컨퍼런스 참")


def kind(site, keywords, days=35):
    """한국거래소 KIND의 IR 일정. 한 달에 200건 안팎인데 대부분 기관 대상 NDR·실적발표라
    공개 세미나·탐방IR·기업설명회·M&A/IPO 관련 설명회만 남긴다(영문 중복 공시 제외)."""
    api = "https://kind.krx.co.kr/corpgeneral/irschedule.do"
    now = datetime.now(KST)
    frm, to = now.strftime("%Y-%m-%d"), (now + timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for page in range(1, 6):
        data = dict(method="searchIRScheduleSub", forward="searchirschedule_sub", currentPageSize="100",
                    pageIndex=str(page), orderMode="4", orderStat="A", marketType="",
                    fromDate=frm, toDate=to, searchFromDate=frm, searchToDate=to)
        r = S.post(api, data=data, timeout=TIMEOUT, headers={"Referer": api})
        r.raise_for_status()
        r.encoding = "utf-8"
        rows = BeautifulSoup(r.text, "html.parser").select("tbody tr")
        for tr in rows:
            td = [_text(x) for x in tr.find_all("td")]
            if len(td) < 6:
                continue
            _, corp, title, place, day, tm = td[:6]
            if not re.search(r"[가-힣]", title) or KIND_DROP.search(title) or not KIND_KEEP.search(title):
                continue
            has_time = bool(re.match(r"\d{1,2}:\d{2}", tm))
            start, _ = parse_kdatetime(f"{day} {tm if has_time else ''}")
            out.append(Event(site["name"], f"[{corp}] {title}", api + "?method=searchIRScheduleMain&gubun=iRSchedule",
                             start, has_time=has_time, host=corp, place=place if place != "-" else "",
                             online="온라인" in place, summary="상장사 IR(KIND 공시). 기관 대상일 수 있어 참석 가능 여부 확인 필요",
                             tags=["IR", "기업설명회"]))
        if len(rows) < 100:
            break
    return out


# ---------------------------------------------------------------- 스타트업얼라이언스
def startupall(site, keywords, max_posts=12):
    base = "https://startupall.kr"
    ids = sorted({int(x) for x in re.findall(r"/programs/info/(\d+)", _get(f"{base}/programs/info"))}, reverse=True)
    out = []
    for pid in ids[:max_posts]:
        url = f"{base}/programs/info/{pid}"
        try:
            L = _lines(_get(url))
        except Exception:
            continue
        title = next((l for l in L if l.startswith("[")), "")
        if not title:
            continue
        start = None; has_time = False; place = ""
        for i, l in enumerate(L):
            if l == "일시" and start is None:
                start, has_time = parse_kdatetime(L[i + 1])
            if l.startswith("일시:"):
                s2, h2 = parse_kdatetime(l)
                if s2:
                    start, has_time = s2, h2
            if l == "장소" and not place:
                place = L[i + 1]
        out.append(Event(site["name"], title, url, start, has_time=has_time, host="스타트업얼라이언스",
                         place=place[:80], online=("온라인" in title or "온라인" in place), price="무료"))
    return out


# ---------------------------------------------------------------- 넥스트라운드(KDB)
def nextround(site, keywords, max_posts=10):
    base = "https://www.nextround.kr"
    ids = sorted({int(x) for x in re.findall(r"/nextround/detail/(\d+)", _get(f"{base}/nextround"))}, reverse=True)
    out = []
    now = datetime.now(KST)
    for rid in ids[:max_posts]:
        url = f"{base}/nextround/detail/{rid}"
        try:
            L = _lines(_get(url))
        except Exception:
            continue
        line = next((l for l in L if l.startswith("[R ")), "")
        if not line:
            continue
        # 한 줄에 "[R 933] Tokyo / 11.18. (수) 2PM" 형태
        title, _, when = line.partition(" / ")
        if not when:
            i = L.index(line)
            when = L[i + 1] if i + 1 < len(L) else ""
        # "11.18. (수) 2PM", "11.27(금) 1:00PM (@양재 aT센터)", "11.24(화)"
        m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.?\s*(?:\([^)]*\))?\s*(?:(\d{1,2})(?::(\d{2}))?\s*(AM|PM))?", when, re.I)
        start = None; has_time = False
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            y = now.year + (1 if mo < now.month - 6 else 0)
            hh = int(m.group(3)) if m.group(3) else 0
            mm = int(m.group(4)) if m.group(4) else 0
            ap = (m.group(5) or "").upper()
            if ap == "PM" and hh < 12:
                hh += 12
            if ap == "AM" and hh == 12:
                hh = 0
            has_time = bool(m.group(3))
            try:
                start = datetime(y, mo, d, hh, mm, tzinfo=KST)
            except ValueError:
                pass
        at = re.search(r"\(@\s*([^)]+)\)", when)
        sessions = [l.strip("[]") for l in L if l.startswith("[IR Session")]
        out.append(Event(site["name"], f"KDB NextRound {title}", url, start, has_time=has_time, host="KDB산업은행",
                         place=at.group(1).strip() if at else "NextRound 라운지(여의도) 또는 공지 장소", price="무료",
                         summary=" · ".join(sessions)[:120] or "스타트업 IR 라운드", tags=["IR", "데모데이"]))
    return out


# ---------------------------------------------------------------- 자본시장연구원 세미나
_MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def kcmi(site, keywords):
    """자본시장연구원 세미나 목록(연도별 한 페이지). 앞으로 열릴 세미나만 상세(프로그램) 페이지에서
    '일시 : 2026년 11월 27일 (금), 15:00~17:30', '주최 : …'를 읽는다. 1년에 10건 안팎."""
    base = "https://www.kcmi.re.kr/seminar/"
    now = datetime.now(KST)
    years = [now.year] + ([now.year + 1] if now.month >= 11 else [])
    out = []
    for y in years:
        try:
            soup = BeautifulSoup(_get(f"{base}seminar_list?syear={y}"), "html.parser")
        except Exception:
            if y == now.year:
                raise
            continue
        for it in soup.select(".month_item-seminar"):
            m = re.search(r"smidx_(\d+)", it.get("id", ""))
            d = it.select_one(".date")
            right = it.select_one(".link_right")
            if not (m and d and right and right.strong):
                continue
            dm = re.search(r"(20\d\d)\s*([A-Za-z]{3})\s*(\d{1,2})", _text(d))
            if not dm or dm.group(2).title() not in _MON:
                continue
            start = datetime(int(dm.group(1)), _MON[dm.group(2).title()], int(dm.group(3)), tzinfo=KST)
            if start.date() < now.date():
                continue
            url = f"{base}seminar_program?eno={m.group(1)}"
            ev = Event(site["name"], _text(right.strong), url, start, has_time=False, host="자본시장연구원",
                       place=_text(right.find("b")), summary=_text(right.find("p")).rstrip(". ")[:120],
                       tags=["세미나"])
            try:  # 상세: 시간·주최
                txt = " ".join(_lines(_get(url)))
                im = re.search(r"일시\s*:?\s*(.{0,60})", txt)
                if im:
                    dt, ht = parse_kdatetime(im.group(1))
                    if dt and dt.date() == start.date():
                        ev.start, ev.has_time = dt, ht
                        em = re.search(r"~\s*(\d{1,2}):(\d{2})", im.group(1))
                        if ht and em:
                            ev.end = dt.replace(hour=int(em.group(1)), minute=int(em.group(2)))
                hm = re.search(r"주최\s*:?\s*(.+?)\s+(?:주관|후원|프로그램|참가신청|장소)", txt)
                if hm:
                    ev.host = hm.group(1).strip()[:60]
            except Exception:
                pass
            ev.online = "온라인" in ev.place and "오프라인" not in ev.place
            out.append(ev)
    return out


# ---------------------------------------------------------------- 한국IR협의회 IPO IR 일정(메일 하단 별도 목록)
def kirs_ipo(start: datetime, end: datetime) -> list[dict]:
    """코스닥 상장 예정 기업의 IPO IR 일정(한국IR협의회 IR캘린더). 행사 목록·캘린더에는 넣지 않고
    메일 하단에 짧게 붙인다. 반환: [{'date': iso, 'has_time', 'company', 'kind', 'place', 'url'}]"""
    cal = "https://www.kirs.or.kr/support/schedule.html"
    months, cur = [], start.replace(day=1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = (cur + timedelta(days=32)).replace(day=1)
    out, seen = [], set()
    for y, mo in months:
        t = _get_kirs(f"{cal}?year={y}&mon={mo}")
        for day, cell in re.findall(r"<td valign=top class='[^']*'>\s*(\d{1,2})(.*?)</td>", t, re.S):
            for no, name in re.findall(r"title='(\d+)'>([^<]+)</a>", cell):
                if no in seen:
                    continue
                seen.add(no)
                dt = datetime(y, mo, int(day), tzinfo=KST)
                if not (start.date() <= dt.date() <= end.date()):
                    continue
                item = {"date": dt.isoformat(), "has_time": False, "company": html.unescape(name).strip(),
                        "kind": "IPO IR", "place": "", "url": f"{cal}?year={y}&mon={mo}"}
                try:
                    L = _lines(_get_kirs(f"https://www.kirs.or.kr/function.php?template=E200&action=view_schedule&no={no}"))
                    kv = {L[i]: L[i + 1] for i in range(len(L) - 1)
                          if L[i] in ("회사명", "IR형태", "일자", "장소", "내용")}
                    when, ht = parse_kdatetime(kv.get("일자", ""))
                    if when:
                        item["date"], item["has_time"] = when.isoformat(), ht
                    item["kind"] = kv.get("IR형태") or "IPO IR"
                    if kv.get("장소") and kv["장소"] not in ("내용",):
                        item["place"] = kv["장소"]
                except Exception:
                    pass
                out.append(item)
    out.sort(key=lambda d: d["date"])
    return out


# 도메인 → 수집기
COLLECTORS = {
    "onoffmix.com": (onoffmix, True),     # (함수, 검색 키워드 사용 여부)
    "event-us.kr": (eventus, True),
    "kvca.or.kr": (kvca, False),
    "startupall.kr": (startupall, False),
    "nextround.kr": (nextround, False),
    "kvic.or.kr": (kvic, False),
    "dcamp.kr": (dcamp, False),
    "kind.krx.co.kr": (kind, False),
    "kcmi.re.kr": (kcmi, False),
}


def collector_for(url: str):
    for dom, c in COLLECTORS.items():
        if dom in url:
            return c
    return None
