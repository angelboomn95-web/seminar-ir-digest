"""세미나·IR 일정 알림 — 구글 로그인 → 수집 기준 → 행사 확인 → 수집 결과 → 받는 사람 관리 → 메일 발송·캘린더 등록.

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
                st.error("구글 로그인 설정이 아직 없습니다. 관리자가 앱 Settings → Secrets에 [oauth] "
                         "(client_id, client_secret, redirect_uri)를 넣으면 로그인 버튼이 나타납니다. "
                         "(PC에서 실행할 때는 credentials.json) — README '최초 설정' 참고")
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

# 시트 ID는 비밀값이 아니다(시트 자체가 공유 권한으로 보호됨). Secrets가 없으면 config.json 값을 쓴다.
SHEET_ID = (secret("app", "recipients_sheet_id") or st.session_state.get("sheet_id")
            or json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("recipients_sheet_id", "")
            or local_settings().get("recipients_sheet_id", ""))
CALENDAR_ID = secret("app", "calendar_id") or local_settings().get("calendar_id", "")


# ================================================================ 수집 결과
def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def load_bundle(b: dict) -> None:
    if b.get("kind") != "seminar-ir-upload" or not isinstance(b.get("events"), list):
        raise ValueError("수집 결과 파일이 아닙니다. out/upload_날짜.json을 골라 주세요")
    st.session_state.bundle = b
    st.session_state.include = {e["key"]: True for e in b["events"]}


def config_rules() -> dict:
    r = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["rules"]
    return {"search": r.get("search", ["VC", "투자", "IR", "데모데이"]), "include": r.get("include", []),
            "exclude": r.get("exclude", []), "regions": r.get("regions", []), "weeks": int(r.get("weeks", 2))}


def config_sites() -> list[dict]:
    return [{"name": x["name"], "url": x["url"], "category": x.get("category", "")}
            for x in json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["sites"]
            if x.get("status") != "제외"]


def current_sites() -> list[dict]:
    """수집할 사이트: 시트 '수집 사이트' 탭 → 없으면 config.json 기본값."""
    if "sites" not in st.session_state:
        saved = rc.load_sites(creds, SHEET_ID) if SHEET_ID else None
        st.session_state.sites = saved or config_sites()
    return st.session_state.sites


def current_rules() -> dict:
    """수집에 쓸 필터 규칙: 시트 '필터 규칙' 탭 → 없으면 config.json 기본값."""
    if "rules" not in st.session_state:
        saved = rc.load_rules(creds, SHEET_ID) if SHEET_ID else None
        st.session_state.rules = {**config_rules(), **(saved or {})}
        st.session_state.rules_from_sheet = bool(saved)
    return st.session_state.rules


tab_rule, tab_ev, tab_rep, tab_to, tab_send = st.tabs(
    ["① 수집 기준", "② 행사 확인", "③ 수집 결과", "④ 받는 사람", "⑤ 보내기"])

with tab_ev:
    c1, c2, c3 = st.columns([1, 1.2, 2])
    base = c1.date_input("기준일", date.today())
    if c2.button("지금 수집하기", type="primary", width="stretch", help="9개 사이트에서 새로 모읍니다 (약 1분)"):
        from collect import OUT, run
        today = datetime(base.year, base.month, base.day, tzinfo=KST)
        with st.spinner("사이트에서 행사를 모으는 중… (1~2분)"):
            run(today, rules=current_rules(), sites=current_sites())
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
        st.markdown("#### 받는 사람 시트 연결")
        st.write("받는 사람은 구글 시트에 저장합니다. 앱 서버에는 파일을 오래 둘 수 없고, 코드 저장소는 공개라 "
                 "이메일 주소를 넣을 수 없기 때문입니다.")
        st.markdown(f"**이미 만든 시트가 있으면** 구글 드라이브에서 '{rc.TITLE}' 시트를 열고 주소창 링크를 붙여 넣으세요.")
        cx1, cx2 = st.columns([4, 1])
        link = cx1.text_input("시트 링크 또는 ID", label_visibility="collapsed",
                              placeholder="https://docs.google.com/spreadsheets/d/…/edit")
        if cx2.button("연결", type="primary", width="stretch", disabled=not link.strip()):
            import re as _re
            m = _re.search(r"/d/([A-Za-z0-9_-]{20,})", link) or _re.fullmatch(r"\s*([A-Za-z0-9_-]{20,})\s*", link)
            if not m:
                st.error("시트 링크에서 ID를 찾지 못했습니다. 주소창의 링크 전체를 붙여 넣어 주세요.")
            else:
                try:
                    rc.load(creds, m.group(1))
                    st.session_state.sheet_id = m.group(1)
                    save_local_settings(recipients_sheet_id=m.group(1))
                    st.rerun()
                except Exception as e:
                    st.error(f"시트를 열지 못했습니다. 나에게 공유된 '{rc.TITLE}' 시트인지 확인해 주세요.\n\n{e}")
        st.markdown("**처음이면** 새로 만드세요. 내 드라이브에 시트가 생깁니다.")
        st.caption("이미 만든 적이 있는데 또 만들면 같은 이름의 빈 시트가 하나 더 생깁니다.")
        if st.button("받는 사람 시트 새로 만들기"):
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
        if not (secret("app", "recipients_sheet_id")
                or json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("recipients_sheet_id")):
            st.warning("⚠️ 이 시트는 **이번 접속에서만 기억됩니다.** 앱이 재시작되면(업데이트·한동안 미사용) 다시 연결해야 합니다. "
                       "계속 쓰려면 Streamlit Secrets의 `[app]`에 아래 줄을 넣고, 시트를 팀원과 '편집자'로 공유해 주세요.")
            st.code(f'recipients_sheet_id = "{SHEET_ID}"', language="toml")
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


# ================================================================ 자동 발송 현황
AUTO_DAY, AUTO_HOUR = 0, 9  # 월요일 09:00 (.github/workflows/weekly.yml의 cron과 같게)
REPO = "hyeonwoo-dotcom/seminar-ir-digest"


@st.cache_data(ttl=600, show_spinner=False)
def last_auto_run() -> dict | None:
    """GitHub Actions 최근 실행(공개 저장소라 로그인 없이 조회)."""
    import requests
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/actions/workflows/weekly.yml/runs?per_page=1", timeout=10)
        runs = r.json().get("workflow_runs", [])
        return runs[0] if runs else None
    except Exception:
        return None


def auto_status() -> None:
    now = datetime.now(KST)
    days = (AUTO_DAY - now.weekday()) % 7
    nxt = (now + timedelta(days=days)).replace(hour=AUTO_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=7)
    people = st.session_state.get("people")
    if people is None and SHEET_ID:
        try:
            people = st.session_state.people = rc.load(creds, SHEET_ID)
        except Exception:
            people = []
    act = rc.active(people or [])
    log = rc.load_log(creds, SHEET_ID, 1) if SHEET_ID else []
    sender = log[0][1] if log and len(log[0]) > 1 else ""
    names = ", ".join(p["이름"] or p["이메일"] for p in act) if act else "없음 (보내는 사람 본인에게만 갑니다)"
    WDK = "월화수목금토일"
    with st.container(border=True):
        st.markdown(f"#### 📬 매주 **{WDK[AUTO_DAY]}요일 {AUTO_HOUR:02d}:00** 자동 발송")
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**받는 사람 {len(act)}명**  \n{names}")
        c2.markdown(f"**다음 발송**  \n{nxt.month}/{nxt.day}({WDK[nxt.weekday()]}) {nxt:%H:%M} · 향후 "
                    f"{config_rules()['weeks']}주 일정")
        run = last_auto_run()
        if run:
            t = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(KST)
            res = {"success": "✅ 성공", "failure": "❌ 실패", "cancelled": "중단"}.get(run.get("conclusion") or "",
                                                                                "⏳ 진행 중")
            c3.markdown(f"**지난 실행**  \n{t.month}/{t.day} {t:%H:%M} · {res} · [기록 보기]({run['html_url']})")
        else:
            c3.markdown("**지난 실행**  \n아직 없음")
        st.caption("받는 사람은 '④ 받는 사람'에서 '받기'를 체크하고 저장하면 다음 발송부터 반영됩니다. "
                   "메일은 '세미나·IR' 캘린더 정리와 함께 나갑니다."
                   + (f" 보내는 계정: {sender}" if sender else ""))


# ================================================================ 보내기
with tab_send:
    auto_status()
    b = st.session_state.get("bundle")
    if not b:
        st.info("수동으로 보내거나 미리 보려면 ② 행사 확인 탭에서 먼저 행사를 불러와 주세요.")
    else:
        evs = included_events()
        today, s, e = _dt(b["today"]), _dt(b["winStart"]), _dt(b["winEnd"])
        ipo = b.get("ipo") or []
        use_ipo = st.checkbox(f"메일 하단에 IPO IR 일정 붙이기 ({len(ipo)}건 · 한국IR협의회, 캘린더에는 안 넣음)",
                              value=bool(ipo), disabled=not ipo, key="use_ipo")
        html = render_mail(evs, today, s, e, b.get("report", []), b.get("notices", []), ipo if use_ipo else None)
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
            st.caption("받는 사람: " + (", ".join(team) if team else "없음 — ④ 받는 사람 탭에서 추가하세요"))
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
            st.caption(f"'세미나·IR' 캘린더를 지금 포함된 행사 {len(evs)}건과 똑같이 맞춥니다. 새 행사는 넣고, "
                       "이미 있는 행사는 내용을 갱신하고, 이번 목록에 없는 앞으로의 일정은 지웁니다. "
                       "(지난 일정과 직접 넣은 일정은 그대로 둡니다)")
            if st.button("'세미나·IR' 캘린더에 등록", width="stretch"):
                with st.spinner("등록하는 중…"):
                    res = sync_calendar(creds, evs, CALENDAR_ID or st.session_state.get("calendar_id"),
                                         prune_from=s, keep_sites={r["site"] for r in b.get("report", []) if r.get("error")})
                st.session_state.calendar_id = res["calendar_id"]
                save_local_settings(calendar_id=res["calendar_id"])
                st.success(f"신규 {res['created']}건 · 갱신 {res['updated']}건 · 삭제 {res['deleted']}건")
                for t, err in res["failed"]:
                    st.warning(f"실패: {t} — {err[:150]}")
        st.markdown("#### 메일 미리보기")
        st.iframe(html, height=900)

# ================================================================ 수집 기준
def _split(s: str) -> list[str]:
    import re as _re
    return list(dict.fromkeys(x.strip() for x in _re.split(r"[,，\n]", s or "") if x.strip()))


with tab_rule:
    r = current_rules()
    st.markdown("어떤 행사를 메일·캘린더에 담을지 정합니다. 저장하면 **다음 '지금 수집하기'부터** 적용되고, "
                "팀원 모두 같은 기준을 씁니다." + (f" · [구글 시트에서 보기]({rc.sheet_url(SHEET_ID)})" if SHEET_ID else ""))
    if not SHEET_ID:
        st.info("기준을 저장하려면 먼저 '④ 받는 사람' 탭에서 시트를 만들어 주세요. 그 전까지는 기본 기준으로 수집합니다.")
    elif not st.session_state.get("rules_from_sheet"):
        st.caption("아직 저장한 기준이 없어 기본 기준(config.json)을 보여 줍니다.")

    # 기본값으로 되돌릴 때 입력칸을 새로 만들도록 키에 버전 번호를 붙인다(화면에 예전 글자가 남는 문제 방지)
    rv = st.session_state.get("rule_ver", 0)
    c0, c1, c2 = st.columns([1.2, 1, 1])
    c0.markdown('<div style="font-size:14px;line-height:1.5">'
                '수집 사이트 — 이곳들에서 행사를 모읍니다</div>', unsafe_allow_html=True)
    with c0:
        from sources import collector_for
        site_rows = [{"사이트": x["name"], "구분": x.get("category", ""), "주소": x["url"]} for x in current_sites()]
        site_edit = st.data_editor(
            site_rows, num_rows="dynamic", hide_index=True, width="stretch", height=256, key=f"rule_sites_{rv}",
            column_config={"사이트": st.column_config.TextColumn("사이트", width="medium"),
                           "구분": st.column_config.TextColumn("구분", width="small"),
                           "주소": st.column_config.LinkColumn("주소", width="medium",
                                                             help="사이트 주소(https://…). 클릭하면 열립니다.")})
        new_sites = [{"name": str(r.get("사이트") or "").strip(), "url": str(r.get("주소") or "").strip(),
                      "category": str(r.get("구분") or "").strip()} for r in site_edit]
        new_sites = [x for x in new_sites if x["name"] and x["url"]]
        no_col = [x["name"] for x in new_sites if not collector_for(x["url"])]
        st.caption(f"수집 사이트 {len(new_sites)}곳 · 맨 아래 빈 줄에 추가, 줄 선택 후 휴지통으로 삭제")
        if no_col:
            st.warning("아직 읽는 방법(수집기)이 없어 수집되지 않는 사이트: " + ", ".join(no_col)
                       + " — 개발 요청이 필요합니다.")
    inc_txt = c1.text_area("포함 키워드 — 하나라도 들어 있어야 담습니다",
                           "\n".join(r["include"]), height=260, key=f"rule_inc_{rv}",
                           help="한 줄에 하나(또는 쉼표로 구분). 영문(AI, IR, VC 등)은 단어 단위로 찾아 'FAIR' 같은 단어에 걸리지 않습니다.")
    exc_txt = c2.text_area("제외 키워드 — 하나라도 들어 있으면 뺍니다",
                           "\n".join(r["exclude"]), height=260, key=f"rule_exc_{rv}",
                           help="포함 키워드보다 먼저 적용됩니다. 예: 채용, 재테크, 지원사업 설명회")
    c1.caption(f"{len(_split(inc_txt))}개 · 예: VC, 벤처투자, M&A, IPO, 세컨더리, IR, 데모데이, 산업 동향, AI, 딥테크")
    c2.caption(f"{len(_split(exc_txt))}개 · 예: 채용박람회, 잡페어, 지원사업 설명회, 창업교육")

    search_txt = st.text_input("행사 플랫폼 검색어 — 온오프믹스·이벤터스에서 이 단어로 찾은 뒤 위 키워드로 거릅니다",
                               ", ".join(r["search"]), key=f"rule_search_{rv}",
                               help="넓은 단어(AI, 세미나 등)를 넣으면 관련 없는 일반 행사가 크게 늘어납니다. 투자 관련 단어 위주로 넣으세요.")
    c3, c4 = st.columns(2)
    region_opts = list(dict.fromkeys(["서울", "온라인"] + r["regions"]))
    regions = c3.multiselect("지역 — 장소가 이 지역이거나 온라인이면 담습니다", region_opts, default=r["regions"],
                             accept_new_options=True, key=f"rule_regions_{rv}",
                             help="'온라인'을 빼면 온라인 행사는 담지 않습니다. 장소에 지역명이 없으면(예: 호텔 이름만) 다른 지역이 적혀 있을 때만 뺍니다.")
    weeks = config_rules()["weeks"]
    c4.text_input("메일에 담을 기간 (고정)", f"기준일부터 {weeks}주", disabled=True,
                  help="앱 수집·자동 발송 모두 같은 기간을 씁니다. 바꾸려면 config.json의 rules.weeks를 고칩니다.")

    with st.expander("기준과 상관없이 항상 빼는 것"):
        st.markdown("- 창업자·기업 대상 **참가자 모집 공고** ('참가기업 모집', 'N기 모집' 등 — '청중평가단 모집', '참관 신청'은 남김)\n"
                    "- **신청 마감이 지난** 공고\n"
                    "- 여러 사이트에 올라온 **같은 행사**(제목이 비슷하면 하나만)\n"
                    "- 온오프믹스·이벤터스는 등록자가 단 태그가 넓어서 **제목**으로만 키워드를 판단")

    b1, b2, _ = st.columns([1, 1, 2])
    if b1.button("기준 저장", type="primary", width="stretch", disabled=not SHEET_ID):
        new = {"search": _split(search_txt), "include": _split(inc_txt), "exclude": _split(exc_txt),
               "regions": regions, "weeks": weeks}
        if not new["include"]:
            st.error("포함 키워드가 하나도 없으면 아무 행사도 담기지 않습니다. 하나 이상 넣어 주세요.")
        elif not new_sites:
            st.error("수집 사이트가 하나도 없습니다. 하나 이상 넣어 주세요.")
        else:
            rc.save_rules(creds, SHEET_ID, new)
            rc.save_sites(creds, SHEET_ID, new_sites)
            st.session_state.sites = new_sites
            st.session_state.rules = new
            st.session_state.rules_from_sheet = True
            st.success("저장했습니다. '② 행사 확인 → 지금 수집하기'를 누르면 새 기준으로 다시 모읍니다.")
    if b2.button("기본값으로 되돌리기", width="stretch"):
        st.session_state.rules = config_rules()
        st.session_state.sites = config_sites()
        st.session_state.rule_ver = st.session_state.get("rule_ver", 0) + 1
        st.rerun()


    with st.expander("🔑 자동 발송 담당자 바꾸기 (관리자용)"):
        st.caption("자동 메일은 GitHub 저장소 Secrets의 GOOGLE_REFRESH_TOKEN 주인 계정으로 나갑니다. "
                   "담당자가 바뀌면 새 담당자가 로그인한 상태에서 아래 값을 꺼내 그 Secret을 바꿉니다(README '자동 발송 설정').")
        if st.checkbox(f"내 계정({me})의 토큰 보기", key="show_token"):
            rt = getattr(creds, "refresh_token", None)
            if rt:
                st.code(rt, language=None)
            else:
                st.warning("토큰이 없습니다. 로그아웃 후 다시 로그인하면 생깁니다.")


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
