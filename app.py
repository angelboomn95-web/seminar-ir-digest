"""세미나·IR 일정 알림 — 로그인 화면 + 수집·미리보기·발송·캘린더 등록.

실행: 실행하기.bat  (또는 streamlit run app.py)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import streamlit as st

from collect import OUT, run
from google_auth import ALLOWED_DOMAIN, CREDENTIALS_FILE, load_creds, login, logout, user_email
from notify import build_message, load_settings, recipients, save_settings, send_mail, sync_calendar
from sources import KST

st.set_page_config(page_title="세미나·IR 일정 알림", page_icon="🗓️", layout="wide")
st.markdown("""<style>
div.stButton > button[kind="primary"]{background:#c73848;border-color:#c73848}
div.stButton > button[kind="primary"]:hover{background:#ad2c3b;border-color:#ad2c3b}
.center{text-align:center}
</style>""", unsafe_allow_html=True)

st.title("🗓️ 세미나·IR 일정 알림")
st.divider()


# ---------------------------------------------------------------- 로그인
@st.cache_data(ttl=3600, show_spinner=False)
def _email(token: str) -> str:
    return user_email(load_creds())


creds = load_creds()
if not creds:
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown("<h2 class='center'>세미나·IR 일정 알림</h2>", unsafe_allow_html=True)
        st.markdown(f"<p class='center'>@{ALLOWED_DOMAIN} Google 계정으로 로그인하면 주간 메일 발송과 "
                    "'세미나·IR' 캘린더 등록이 가능합니다.</p>", unsafe_allow_html=True)
        if not CREDENTIALS_FILE.exists():
            st.error("credentials.json이 없습니다. README의 '최초 설정'을 따라 OAuth 클라이언트 파일을 넣어 주세요.")
        elif st.button("Google 계정으로 로그인", type="primary", width="stretch"):
            try:
                with st.spinner("새 탭에서 로그인을 마쳐 주세요…"):
                    login()
                st.rerun()
            except Exception as e:
                st.error(f"로그인하지 못했습니다: {e}")
    st.caption("로그인 후 이 화면이 자동으로 바뀝니다. 브라우저에 열린 로그인 탭은 닫아도 됩니다.")
    st.stop()

me = _email(creds.token or "")
top_l, top_r = st.columns([4, 1])
top_l.markdown(f"로그인: **{me}**")
if top_r.button("로그아웃", width="stretch"):
    logout(); _email.clear(); st.rerun()

# ---------------------------------------------------------------- 설정
settings = load_settings()
with st.expander("받는 사람 · 캘린더 설정", expanded=False):
    to_text = st.text_area("받는 사람 (한 줄에 한 명)", "\n".join(settings["mail_to"]),
                           help="비워 두면 로그인한 본인에게만 보냅니다(시범 운영).")
    cal_id = st.text_input("캘린더 ID", settings["calendar_id"],
                           help="비워 두면 내 계정에서 '세미나·IR' 캘린더를 찾고, 없으면 새로 만듭니다.")
    if st.button("설정 저장"):
        settings["mail_to"] = [x.strip() for x in to_text.splitlines() if x.strip()]
        settings["calendar_id"] = cal_id.strip()
        save_settings(settings); st.success("저장했습니다")
to = recipients(creds, settings)
st.caption("받는 사람: " + (", ".join(to) + (" (본인만 · 시범 운영)" if not settings["mail_to"] else "")))

# ---------------------------------------------------------------- 수집
c1, c2 = st.columns([1, 3])
base = c1.date_input("기준일", date.today())
today = datetime(base.year, base.month, base.day, tzinfo=KST)
stamp = f"{today:%Y%m%d}"
weeks = int(json.loads((OUT.parent / "config.json").read_text(encoding="utf-8"))["rules"].get("weeks", 4))
end = today + timedelta(weeks=weeks) - timedelta(days=1)

if c2.button("① 행사 수집", type="primary"):
    with st.spinner("사이트에서 행사를 모으는 중… (1~2분)"):
        run(today)
    st.rerun()

ev_file, mail_file, ics_file = OUT / f"events_{stamp}.json", OUT / f"mail_{stamp}.html", OUT / f"events_{stamp}.ics"
if not ev_file.exists():
    st.info(f"{base:%Y-%m-%d} 기준 수집 결과가 없습니다. '① 행사 수집'을 눌러 주세요.")
    st.stop()

events = json.loads(ev_file.read_text(encoding="utf-8"))
report = json.loads((OUT / f"report_{stamp}.json").read_text(encoding="utf-8"))
st.subheader(f"{today:%m/%d}~{end:%m/%d} 행사 {len(events)}건")

b1, b2 = st.columns(2)
if b1.button("② '세미나·IR' 캘린더에 등록", width="stretch"):
    with st.spinner("캘린더에 등록하는 중…"):
        res = sync_calendar(creds, events)
    st.success(f"신규 {res['created']}건 · 갱신 {res['updated']}건")
    for t, e in res["failed"]:
        st.warning(f"실패: {t} — {e[:150]}")
confirm = b2.checkbox(f"{len(to)}명에게 보내는 것을 확인했습니다")
if b2.button("③ 메일 발송", width="stretch", disabled=not confirm):
    with st.spinner("보내는 중…"):
        msg = build_message(creds, mail_file.read_text(encoding="utf-8"), ics_file, len(events), today, end, to)
        send_mail(creds, msg)
    st.success(f"보냈습니다: {msg['Subject']}")

tab_mail, tab_list, tab_report = st.tabs(["메일 미리보기", "행사 목록", "사이트별 수집 결과"])
with tab_mail:
    st.iframe(mail_file.read_text(encoding="utf-8"), height=900)
with tab_list:
    st.dataframe([{"일시": e["start"][:16].replace("T", " "), "행사명": e["title"], "주최": e.get("host") or e["source"],
                   "장소": "온라인" if e.get("online") else e.get("place", ""), "비용": e.get("price", ""),
                   "출처": e["source"], "링크": e["url"]} for e in events],
                 width="stretch", hide_index=True,
                 column_config={"링크": st.column_config.LinkColumn("링크", display_text="열기")})
with tab_report:
    st.dataframe([{"사이트": r["site"], "수집": r["collected"], "통과": r["kept"],
                   "제외 사유": ", ".join(f"{k} {v}" for k, v in (r["dropped"] or {}).items()),
                   "오류": r["error"] or ""} for r in report], width="stretch", hide_index=True)
