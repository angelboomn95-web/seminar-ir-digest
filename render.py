"""주간 메일 HTML. 디캠프 뉴스 브리핑 정기메일처럼 날짜순 목록 + 항목별 핵심 정보 한눈에.

메일 클라이언트(Gmail/Outlook) 호환을 위해 table 레이아웃 + 인라인 스타일만 쓴다.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from calendar_ics import gcal_link

WD = "월화수목금토일"
WD_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

C_TEXT = "#1b2230"
C_MUTED = "#6b7280"
C_LINE = "#e5e7eb"
C_ACCENT = "#1f5fd1"
C_BAND = "#f3f5f8"


def _d(iso: str | None):
    return datetime.fromisoformat(iso) if iso else None


def _md(dt: datetime) -> str:
    return f"{dt.month}.{dt.day} ({WD[dt.weekday()]})"


def _item(ev: dict) -> str:
    s = _d(ev["start"])
    when = _md(s) + (f" {s:%H:%M}" if ev.get("has_time") and (s.hour or s.minute) else "")
    dl = _d(ev.get("deadline"))
    host = ev.get("host") or ev.get("source")
    place = "온라인" if ev.get("online") else (ev.get("place") or "장소 확인 필요")
    price = ev.get("price") or "비용 확인 필요"
    summary = (ev.get("summary") or "").strip()
    meta = [f"📍 {escape(place)}", f"💳 {escape(price)}"]
    if dl:
        meta.append(f"⏰ 신청마감 {_md(dl)}")
    return f"""
<tr><td style="padding:14px 0;border-bottom:1px solid {C_LINE};">
  <div style="font-size:12px;color:{C_ACCENT};font-weight:bold;">{escape(when)}</div>
  <div style="font-size:15px;font-weight:bold;margin:3px 0 2px;line-height:1.4;">
    <a href="{escape(ev['url'])}" style="color:{C_TEXT};text-decoration:none;">{escape(ev['title'])}</a></div>
  <div style="font-size:12px;color:{C_MUTED};">{escape(host)}{' | ' + escape(summary) if summary else ''}</div>
  <div style="font-size:12px;color:{C_TEXT};margin-top:5px;">{' &nbsp;·&nbsp; '.join(meta)}
    &nbsp; <a href="{escape(ev['url'])}" style="color:{C_ACCENT};font-weight:bold;">신청 링크 ›</a>
    &nbsp; <a href="{escape(gcal_link(ev))}" style="color:{C_MUTED};">캘린더에 추가</a></div>
  <div style="font-size:11px;color:{C_MUTED};margin-top:3px;">출처: {escape(ev['source'])} · 키워드: {escape(ev.get('matched',''))}</div>
</td></tr>"""


def _notice(ev: dict) -> str:
    p = _d(ev["posted"])
    return f"""
<tr><td style="padding:8px 0;border-bottom:1px solid {C_LINE};font-size:13px;">
  <a href="{escape(ev['url'])}" style="color:{C_TEXT};text-decoration:none;font-weight:bold;">{escape(ev['title'])}</a>
  <div style="font-size:11px;color:{C_MUTED};margin-top:2px;">{escape(ev['source'])} · 게시 {_md(p)}{' · ' + escape(ev['summary'][:60]) if ev.get('summary') else ''}</div>
</td></tr>"""


def _ipo(it: dict) -> str:
    d = _d(it["date"])
    when = _md(d) + (f" {d:%H:%M}" if it.get("has_time") else "")
    extra = f" · {escape(it['place'])}" if it.get("place") else ""
    return f"""
<tr><td style="padding:6px 0;border-bottom:1px solid {C_LINE};font-size:13px;">
  <span style="color:{C_ACCENT};font-weight:bold;display:inline-block;min-width:110px;">{escape(when)}</span>
  <a href="{escape(it['url'])}" style="color:{C_TEXT};text-decoration:none;font-weight:bold;">{escape(it['company'])}</a>
  <span style="color:{C_MUTED};font-size:12px;"> {escape(it.get('kind') or 'IPO IR')}{extra}</span>
</td></tr>"""


def render_mail(events: list[dict], today: datetime, start: datetime, end: datetime, report: list[dict],
                notices: list[dict] | None = None, ipo: list[dict] | None = None) -> str:
    # 주 단위로 묶기
    groups: dict[str, list[dict]] = {}
    for ev in events:
        s = _d(ev["start"])
        wk = (s.date() - start.date()).days // 7
        groups.setdefault(wk, []).append(ev)
    body = []
    for wk in sorted(groups):
        items = groups[wk]
        a = _d(items[0]["start"])
        label = "이번 주" if wk == 0 else ("다음 주" if wk == 1 else f"{wk + 1}주 차")
        body.append(f"""
<tr><td style="padding:22px 0 4px;"><div style="font-size:13px;font-weight:bold;color:{C_TEXT};
  border-left:3px solid {C_ACCENT};padding-left:8px;">{label} <span style="color:{C_MUTED};font-weight:normal;">({len(items)}건)</span></div></td></tr>""")
        body.extend(_item(e) for e in items)
    if not events:
        body.append(f'<tr><td style="padding:28px 0;color:{C_MUTED};">이번 기간에 조건에 맞는 행사가 없습니다.</td></tr>')
    if notices:
        body.append(f"""
<tr><td style="padding:26px 0 4px;"><div style="font-size:13px;font-weight:bold;color:{C_TEXT};
  border-left:3px solid {C_MUTED};padding-left:8px;">날짜 확인 필요 공지 <span style="color:{C_MUTED};font-weight:normal;">
  ({len(notices)}건 · 행사일이 첨부파일 등에만 있어 자동으로 읽지 못한 최근 공지)</span></div></td></tr>""")
        body.extend(_notice(n) for n in notices)
    if ipo:
        body.append(f"""
<tr><td style="padding:26px 0 4px;"><div style="font-size:13px;font-weight:bold;color:{C_TEXT};
  border-left:3px solid {C_MUTED};padding-left:8px;">IPO IR 일정 <span style="color:{C_MUTED};font-weight:normal;">
  ({len(ipo)}건 · 코스닥 상장 예정 기업 설명회, 한국IR협의회 · 주로 기관투자자 대상이라 참고용)</span></div></td></tr>""")
        body.extend(_ipo(i) for i in ipo)

    report = [r for r in report if "IPO IR" not in r["site"]]  # 하단 IPO 목록은 사이트 수에서 뺀다
    manual = [r["site"] for r in report if r.get("error", "") and r["error"].startswith("수집기 없음")]
    failed = [f"{r['site']}" for r in report if r.get("error") and not r["error"].startswith("수집기 없음")]
    foot = f"수집 사이트 {sum(1 for r in report if not r.get('error'))}곳 자동 수집"
    if failed:
        foot += f" · 오류 {', '.join(failed)}"
    if manual:
        foot += f" · 수동 확인 {len(manual)}곳"

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>주간 세미나·IR 일정</title></head>
<body style="margin:0;background:#ffffff;font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;color:{C_TEXT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">
  <tr><td style="padding:0 0 12px;border-bottom:2px solid {C_TEXT};">
    <table role="presentation" width="100%"><tr>
      <td style="font-size:20px;font-weight:bold;letter-spacing:-0.5px;">디캠프</td>
      <td align="right" style="font-size:12px;color:{C_MUTED};">투자전략팀 &nbsp; {today:%Y.%m.%d} ({WD_EN[today.weekday()]})</td>
    </tr></table></td></tr>
  <tr><td style="padding:20px 0 6px;font-size:22px;font-weight:bold;">{today:%Y.%m.%d} 주간 세미나·IR 일정</td></tr>
  <tr><td style="font-size:13px;line-height:1.7;color:{C_TEXT};">
    안녕하세요, 투자전략팀입니다.<br>
    {_md(start)} ~ {_md(end)} 기간의 세미나·교육·IR/데모데이 일정 <b>{len(events)}건</b>을 날짜순으로 정리했습니다.<br>
    <span style="color:{C_MUTED};">각 행사의 '캘린더에 추가'를 누르면 내 구글 캘린더에 바로 넣을 수 있습니다.</span></td></tr>
  {''.join(body)}
  <tr><td style="padding:24px 0 0;font-size:11px;color:{C_MUTED};line-height:1.6;background:#fff;">
    <div style="background:{C_BAND};padding:10px 12px;">{escape(foot)}<br>
    수집 사이트·키워드는 세미나·IR 앱의 '수집 기준' 탭에서 조정합니다.</div>
    <div style="padding-top:12px;">Copyright {today:%Y} 디캠프, All rights reserved.</div></td></tr>
</table></td></tr></table></body></html>"""
