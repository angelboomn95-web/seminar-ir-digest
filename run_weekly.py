"""주간 실행: 수집 → 캘린더 등록 → 메일 발송. 작업 스케줄러는 이 파일을 실행한다.

    python run_weekly.py --dry-run        # 수집·메일 생성만, 발송·등록 안 함 (기본 동작 확인용)
    python run_weekly.py                  # 실제 발송 + 캘린더 등록
    python run_weekly.py --no-calendar    # 메일만
    python run_weekly.py --no-mail        # 캘린더만
    python run_weekly.py --today 2026-11-16

실행 기록: logs/run_YYYYMMDD.log
발송·등록에는 구글 로그인(token.json)이 필요하다. PC에서 app.py를 열어 한 번 로그인하면 된다.
받는 사람은 settings.json의 recipients_sheet_id 시트에서 읽는다(앱에서 시트를 만들면 자동 저장).
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from collect import OUT, run
import recipients as rc
from calendar_ics import to_ics
from google_auth import require_creds, user_email
from notify import build_message, send_mail, subject_for, sync_calendar
from sources import KST

ROOT = Path(__file__).parent
LOGS = ROOT / "logs"


def sheet_rules() -> dict | None:
    """앱 '수집 기준' 탭에서 저장한 규칙(시트). 로그인·시트가 없으면 None → config.json 기본값."""
    try:
        from google_auth import load_creds
        settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        creds = load_creds()
        return rc.load_rules(creds, settings["recipients_sheet_id"]) if creds else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--no-calendar", action="store_true")
    a = ap.parse_args()

    today = datetime.strptime(a.today, "%Y-%m-%d").replace(tzinfo=KST) if a.today else datetime.now(KST)
    stamp = f"{today:%Y%m%d}"
    LOGS.mkdir(exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"{datetime.now(KST):%H:%M:%S} {msg}"
        log_lines.append(line)
        try:
            print(line)
        except Exception:
            pass

    ok = True
    try:
        kept, report = run(today, rules=sheet_rules())
        errs = [r for r in report if r["error"] and "수동 확인" not in r["error"]]
        log(f"수집 완료: {len(kept)}건 / 사이트 오류 {len(errs)}곳")
        for r in errs:
            log(f"  [오류] {r['site']}: {r['error']}")
        if not kept:
            log("  [경고] 통과한 행사가 0건입니다. report 파일을 확인하세요")

        if a.no_calendar or a.dry_run:
            log("캘린더 등록 건너뜀")
        else:
            res = sync_calendar(require_creds(), kept)
            log(f"캘린더: 신규 {res['created']} / 갱신 {res['updated']} / 실패 {len(res['failed'])}")
            for t, e in res["failed"]:
                log(f"  [실패] {t}: {e[:200]}")
            ok &= not res["failed"]

        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        weeks = int((cfg.get("rules") or {}).get("weeks") or 4)
        html = (OUT / f"mail_{stamp}.html").read_text(encoding="utf-8")
        if a.no_mail or a.dry_run:
            log(f"메일 발송 건너뜀 (미리보기: out/mail_{stamp}.html)")
        else:
            creds = require_creds()
            me = user_email(creds)
            settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8")) if (ROOT / "settings.json").exists() else {}
            sid = settings.get("recipients_sheet_id", "")
            to = [p["이메일"] for p in rc.active(rc.load(creds, sid))] if sid else []
            to = to or [me]  # 받는 사람이 없으면 나에게만
            subject = subject_for(len(kept), today, today + timedelta(weeks=weeks) - timedelta(days=1))
            send_mail(creds, build_message(me, to, html, to_ics(kept), subject, f"세미나IR_{stamp}.ics"))
            if sid:
                rc.log_send(creds, sid, me, to, subject)
            log(f"메일 발송: {len(to)}명 / {subject}")
    except Exception:
        ok = False
        log("[중단] " + traceback.format_exc())
    finally:
        with (LOGS / f"run_{stamp}.log").open("a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
