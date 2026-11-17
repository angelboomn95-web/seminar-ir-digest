"""세미나·IR 일정 알림 — 구글 로그인 → 행사 확인 → 받는 사람 관리 → 메일 발송·캘린더 등록.

실행
- Streamlit Cloud: 저장소를 연결하고 Secrets에 [oauth], [app]을 넣는다(.streamlit/secrets.toml.example 참고).
- 로컬 PC: streamlit run app.py  (Secrets에 [oauth]가 없으면 credentials.json으로 로컬 로그인)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

import google_auth as ga
import recipients as rc
from calendar_ics import to_ics
from notify import build_message, send_mail, subject_for, sync_calendar
from render import render_mail
from sources import KST

ROOT = Path(__file__).parent
SETTINGS = ROOT / "settings.json"  # 로컬 실행 때만 쓰는 값(수신자 시트 ID 등)

st.set_page_config(page_title="세미나·IR 일정 알림", page_icon="🗓️", layout="wide")
st.title("🗓️ 세미나·IR 일정 알림")


def secret(section: str, key: str, default=""):
    try:
        return st.secrets[section][key]
    except Exception:
        return default


def local_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_local_settings(**kw) -> None:
    s = local_settings(); s.update(kw)
    try:
        SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ================================================================ 로그인
CLIENT_ID = secret("oauth", "client_id")
CLIENT_SECRET = secret("oauth", "client_secret")
REDIRECT_URI = secret("oauth", "redirect_uri")
WEB = bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


def login_screen(button) -> None:
    st.markdown("---")
    st.markdown(
        "<h2 style='text-align:center;margin-top:2rem;'>세미나·IR 일정 알림</h2>"
        f"<p style='text-align:center;color:#6b7280;'>@{ga.ALLOWED_DOMAIN} Google 계정으로 로그인하면 "
        "주간 메일 발송과 '세미나·IR' 캘린더 등록이 가능합니다.</p>", unsafe_allow_html=True)
    _, mid, _ = st.columns([2, 1, 2])
    with mid:
        button()
    st.caption("로그인 후 새 탭에서 앱이 열립니다. 이 탭은 닫아도 됩니다." if WEB
               else "로그인 창이 새 탭으로 열립니다. 로그인을 마치면 이 화면이 바뀝니다.")
    st.markdown("---")
    st.stop()


if WEB:
    if "creds" not in st.session_state:
        code = st.query_params.get("code")
        if code:
            try:
                creds = ga.exchange_code(code, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
                email = ga.user_email(creds)
                st.query_params.clear()
                if not ga.domain_ok(email):
                    st.error(f"@{ga.ALLOWED_DOMAIN} 계정만 사용할 수 있습니다. (로그인한 계정: {email})")
                else:
                    st.session_state.creds = creds.to_json()
                    st.session_state.email = email
                    st.rerun()
            except Exception as e:
                st.query_params.clear()
                st.error(f"로그인하지 못했습니다: {e}")
    if "creds" not in st.session_state:
        login_screen(lambda: st.link_button("Google 계정으로 로그인", ga.auth_url(CLIENT_ID, REDIRECT_URI),
                                            type="primary", width="stretch"))
    try:
        creds = ga.creds_from_json(st.session_state.creds)
        st.session_state.creds = creds.to_json()
    except Exception:
        for k in ("creds", "email"):
            st.session_state.pop(k, None)
        st.rerun()
    me = st.session_state.email
else:
    creds = ga.load_creds()
    if not creds:
        def _local_btn():
            if not ga.CREDENTIALS_FILE.exists():
                st.error("credentials.json이 없습니다. README '최초 설정'을 참고해 주세요.")
            elif st.button("Google 계정으로 로그인", type="primary", width="stretch"):
                try:
                    with st.spinner("새 탭에서 로그인을 마쳐 주세요…"):
                        ga.login_local()
                    st.rerun()
                except Exception as e:
                    st.error(f"로그인하지 못했습니다: {e}")
        login_screen(_local_btn)
    if "email" not in st.session_state:
        st.session_state.email = ga.user_email(creds)
    me = st.session_state.email

top = st.columns([5, 1])
top[0].caption(f"**{me}** 로그인됨")
if top[1].button("로그아웃", width="stretch"):
    if not WEB:
        ga.logout_local()
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()

SHEET_ID = secret("app", "recipients_sheet_id") or st.session_state.get("sheet_id") or local_settings().get("recipients_sheet_id", "")
CALENDAR_ID = secret("app", "calendar_id") or local_settings().get("calendar_id", "")


# ================================================================ 수집 결과
def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def load_bundle(b: dict) -> None:
    if b.get("kind") != "seminar-ir-upload" or not isinstance(b.get("events"), list):
        raise ValueError("수집 결과 파일이 아닙니다. out/upload_날짜.json을 골라 주세요")
    st.session_state.bundle = b
    st.session_state.include = {e["key"]: True for e in b["events"]}


tab_ev, tab_to, tab_send, tab_rep = st.tabs(["① 행사 확인", "② 받는 사람", "③ 보내기", "사이트별 수집 결과"])

with tab_ev:
    c1, c2, c3 = st.columns([1, 1.2, 2])
    base = c1.date_input("기준일", date.today())
    if c2.button("지금 수집하기", type="primary", width="stretch", help="9개 사이트에서 새로 모읍니다 (약 1분)"):
        from collect import OUT, run
        today = datetime(base.year, base.month, base.day, tzinfo=KST)
        with st.spinner("사이트에서 행사를 모으는 중… (약 1분)"):
            run(today)
        load_bundle(json.loads((OUT / f"upload_{today:%Y%m%d}.json").read_text(encoding="utf-8")))
        st.rerun()
    up = c3.file_uploader("또는 PC에서 수집한 결과 파일 올리기 (out/upload_날짜.json)", type=["json"])
    if up is not None and st.session_state.get("uploaded_name") != up.name + str(up.size):
        try:
            load_bundle(json.loads(up.getvalue().decode("utf-8")))
            st.session_state.uploaded_name = up.name + str(up.size)
            st.rerun()
        except Exception as e:
            st.error(str(e))

    b = st.session_state.get("bundle")
    if not b:
        st.info("'지금 수집하기'를 누르거나 결과 파일을 올리면 행사 목록이 나옵니다.")
    else:
        inc = st.session_state.include
        s, e = _dt(b["winStart"]), _dt(b["winEnd"])
        st.caption(f"기간 **{s:%m/%d} ~ {e:%m/%d}** · 수집 {b.get('generatedAt', '')[:16].replace('T', ' ')} · "
                   "메일과 캘린더에서 뺄 행사는 '포함' 체크를 해제하세요.")
        rows = [{"포함": inc.get(ev["key"], True),
                 "일시": ev["start"][5:16].replace("T", " ").replace(" 00:00", ""),
                 "행사명": ev["title"], "주최": ev.get("host") or ev["source"],
                 "장소": "온라인" if ev.get("online") else (ev.get("place") or ""),
                 "비용": ev.get("price") or "", "신청": ev["url"]} for ev in b["events"]]
        edited = st.data_editor(rows, hide_index=True, width="stretch", key=f"ev_{b['stamp']}",
                                disabled=["일시", "행사명", "주최", "장소", "비용", "신청"],
                                column_config={"포함": st.column_config.CheckboxColumn(width="small"),
                                               "신청": st.column_config.LinkColumn(display_text="열기")})
        for ev, row in zip(b["events"], edited):
            inc[ev["key"]] = bool(row["포함"])
        n_in = sum(1 for ev in b["events"] if inc.get(ev["key"], True))
        st.caption(f"{len(b['events'])}건 중 **{n_in}건** 포함")


def included_events() -> list[dict]:
    b = st.session_state.get("bundle") or {}
    inc = st.session_state.get("include", {})
    return [e for e in b.get("events", []) if inc.get(e["key"], True)]


# ================================================================ 받는 사람
with tab_to:
    if not SHEET_ID:
        st.markdown("#### 받는 사람 목록 만들기")
        st.write("받는 사람은 구글 시트에 저장합니다. 앱 서버에는 파일을 오래 둘 수 없고, 코드 저장소는 공개라 "
                 "이메일 주소를 넣을 수 없기 때문입니다. 아래 버튼을 누르면 내 드라이브에 "
                 f"'{rc.TITLE}' 시트가 생깁니다.")
        if st.button("받는 사람 시트 만들기", type="primary"):
            sid = rc.create_sheet(creds)
            st.session_state.sheet_id = sid
            save_local_settings(recipients_sheet_id=sid)
            st.rerun()
    else:
        if "people" not in st.session_state:
            try:
                st.session_state.people = rc.load(creds, SHEET_ID)
            except Exception as e:
                st.error("받는 사람 시트를 읽지 못했습니다. 시트가 나에게 공유돼 있는지 확인해 주세요. "
                         f"([시트 열기]({rc.sheet_url(SHEET_ID)}))\n\n{e}")
                st.stop()
        if not secret("app", "recipients_sheet_id"):
            st.info(f"시트를 만들었습니다. 팀원도 같은 목록을 쓰려면 Streamlit Secrets의 `[app]`에 "
                    f"`recipients_sheet_id = \"{SHEET_ID}\"`를 넣고, 시트를 팀원과 '편집자'로 공유해 주세요.")
        st.markdown(f"사람을 추가하려면 표 맨 아래 빈 줄에 입력하고 **저장**을 누르세요. "
                    f"'받기'를 해제하면 목록에는 남고 메일만 가지 않습니다. · [구글 시트에서 열기]({rc.sheet_url(SHEET_ID)})")
        people_edit = st.data_editor(
            st.session_state.people or [{"받기": True, "이름": "", "이메일": "", "메모": ""}],
            num_rows="dynamic", hide_index=True, width="stretch", key="people_editor",
            column_order=["받기", "이름", "이메일", "메모"],
            column_config={"받기": st.column_config.CheckboxColumn(default=True, width="small"),
                           "이메일": st.column_config.TextColumn(width="medium")})
        b1, b2 = st.columns([1, 1])
        if b1.button("저장", type="primary", width="stretch"):
            cleaned, problems = rc.clean(people_edit)
            for p in problems:
                st.warning(p)
            rc.save(creds, SHEET_ID, cleaned)
            st.session_state.people = cleaned
            st.session_state.pop("people_editor", None)
            st.success(f"{len(cleaned)}명을 저장했습니다.")
            st.rerun()
        if b2.button("시트에서 다시 불러오기", width="stretch"):
            st.session_state.pop("people", None); st.session_state.pop("people_editor", None)
            st.rerun()

        act = rc.active(st.session_state.people)
        st.markdown(f"#### 실제로 메일을 받는 사람 · {len(act)}명")
        if act:
            st.dataframe([{"이름": p["이름"], "이메일": p["이메일"], "메모": p["메모"]} for p in act],
                         hide_index=True, width="stretch")
        else:
            st.caption("'받기'가 체크된 사람이 없습니다. 지금 보내면 나에게만 갑니다.")
        log = rc.load_log(creds, SHEET_ID)
        if log:
            with st.expander("최근 발송 기록"):
                st.dataframe([dict(zip(rc.LOG_HEADER, (r + [""] * 5)[:5])) for r in log],
                             hide_index=True, width="stretch")


# ================================================================ 보내기
with tab_send:
    b = st.session_state.get("bundle")
    if not b:
        st.info("① 행사 확인 탭에서 먼저 행사를 불러와 주세요.")
    else:
        evs = included_events()
        today, s, e = _dt(b["today"]), _dt(b["winStart"]), _dt(b["winEnd"])
        html = render_mail(evs, today, s, e, b.get("report", []), b.get("notices", []))
        ics = to_ics(evs)
        subject = subject_for(len(evs), s, e)
        people = st.session_state.get("people")
        if people is None and SHEET_ID:
            try:
                people = st.session_state.people = rc.load(creds, SHEET_ID)
            except Exception:
                people = []
        team = [p["이메일"] for p in rc.active(people or [])]

        st.markdown(f"**제목** {subject}")
        col_m, col_c = st.columns(2)
        with col_m:
            st.markdown("#### 메일")
            st.caption("받는 사람: " + (", ".join(team) if team else "없음 — ② 받는 사람 탭에서 추가하세요"))
            if st.button("나에게 테스트 발송", width="stretch"):
                send_mail(creds, build_message(me, [me], html, ics, "[테스트] " + subject, f"세미나IR_{b['stamp']}.ics"))
                st.success(f"{me}로 보냈습니다. 받은편지함에서 모양을 확인해 보세요.")
            ok = st.checkbox(f"{len(team)}명에게 보내는 것을 확인했습니다", disabled=not team)
            if st.button(f"받는 사람 {len(team)}명에게 발송", type="primary", width="stretch", disabled=not (team and ok)):
                send_mail(creds, build_message(me, team, html, ics, subject, f"세미나IR_{b['stamp']}.ics"))
                if SHEET_ID:
                    rc.log_send(creds, SHEET_ID, me, team, subject)
                st.success(f"{len(team)}명에게 보냈습니다.")
        with col_c:
            st.markdown("#### 캘린더")
            st.caption(f"포함된 행사 {len(evs)}건을 내 구글 캘린더의 '세미나·IR'에 넣습니다. 없으면 새로 만들고, "
                       "이미 넣은 행사는 내용만 갱신합니다.")
            if st.button("'세미나·IR' 캘린더에 등록", width="stretch"):
                with st.spinner("등록하는 중…"):
                    res = sync_calendar(creds, evs, CALENDAR_ID or st.session_state.get("calendar_id"))
                st.session_state.calendar_id = res["calendar_id"]
                save_local_settings(calendar_id=res["calendar_id"])
                st.success(f"신규 {res['created']}건 · 갱신 {res['updated']}건")
                for t, err in res["failed"]:
                    st.warning(f"실패: {t} — {err[:150]}")
        st.markdown("#### 메일 미리보기")
        st.iframe(html, height=900)


# ================================================================ 사이트별 결과
with tab_rep:
    b = st.session_state.get("bundle")
    if not b:
        st.info("행사를 불러오면 사이트별 수집 결과가 나옵니다.")
    else:
        st.dataframe([{"사이트": r["site"], "수집": r["collected"], "통과": r["kept"],
                       "빠진 이유": ", ".join(f"{k} {v}" for k, v in (r["dropped"] or {}).items()),
                       "상태": r["error"] or "정상"} for r in b.get("report", [])],
                     hide_index=True, width="stretch")
