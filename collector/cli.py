"""명령행: python -m collector <명령> (설계서 4.3, AGENTS.md)."""
import argparse
import sys
from datetime import datetime, timedelta

from .candidates import candidate_filename, render_prompt
from .ref import KST, weekly_window
from .settings import PROMPT_TOPICS, WEEK_RE, load_settings


def default_issue_week(now=None):
    """가장 최근에 끝난 수집 기간(일 00:00 KST 종료)에 대응하는 발송 주차."""
    now = now or datetime.now(KST)
    d = now.date()
    last_sunday = d - timedelta(days=(d.weekday() + 1) % 7)
    y, w, _ = (last_sunday + timedelta(days=1)).isocalendar()
    return f"{y}-W{w:02d}"


def _week(s):
    if not WEEK_RE.match(s):
        raise argparse.ArgumentTypeError("YYYY-Www 형식 (예: 2026-W49)")
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m collector", description="MTC AX/PI 위클리 사외 수집기")
    ap.add_argument("--root", help="저장소 루트(기본: 이 패키지의 상위 폴더)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def week_args(p):
        p.add_argument("--issue-week", type=_week, default=None, help="발송 주차. 기본: 가장 최근에 끝난 기간")
        p.add_argument("--weeks", type=int, choices=[1, 2], default=1,
                       help="2는 사용자가 요청한 경우에만: 지난주 발행을 건너뛰었을 때 14일 수집")

    p = sub.add_parser("window", help="수집 기간 확인"); week_args(p)
    p = sub.add_parser("prompt", help="LLM 후보 탐색 프롬프트 생성"); week_args(p)
    p.add_argument("--topic", choices=PROMPT_TOPICS + ["all"], default="all")
    p.add_argument("--resurvey", action="store_true", help="재조사용 프롬프트")
    p = sub.add_parser("run", help="수집 실행(pass1→pass2→병합→검증→리포트)"); week_args(p)
    p.add_argument("--force", action="store_true", help="기간 종료 전 실행(시험용)")
    p.add_argument("--skip-registry", action="store_true", help="레지스트리 순회 생략(시험용)")
    p = sub.add_parser("package", help="사내 발송 파일 생성"); week_args(p)
    p = sub.add_parser("validate", help="JSON 파일 검증(K1~K11)")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("check-sources", help="sources.yaml 발행처 접속 점검")
    p.add_argument("--id", action="append", help="특정 id만")

    a = ap.parse_args(argv)
    st = load_settings(a.root)
    week = getattr(a, "issue_week", None) or default_issue_week()

    if a.cmd == "window":
        s, e = weekly_window(week, a.weeks)
        print(f"issue_week {week}  window {s.isoformat()} ~ {e.isoformat()} ({a.weeks}주)")
        return 0

    if a.cmd == "prompt":
        s, e = weekly_window(week, a.weeks)
        d = st.work_dir(week) / "prompts"
        (st.candidates_dir(week)).mkdir(parents=True, exist_ok=True)
        d.mkdir(parents=True, exist_ok=True)
        topics = PROMPT_TOPICS if a.topic == "all" else [a.topic]
        if a.resurvey:
            topics = [t for t in topics if t != "B"]
        for t in topics:
            text = render_prompt(st, t, s, e, a.resurvey)
            name = f"{'resurvey-' if a.resurvey else ''}{t}.txt"
            (d / name).write_text(text + "\n", encoding="utf-8")
            print(f"{d / name}")
            print(f"   → 결과 저장: {st.candidates_dir(week) / candidate_filename('claude', t, a.resurvey)} "
                  f"(gpt·gemini는 파일명 앞부분만 바꿈)")
        return 0

    if a.cmd == "run":
        from .pipeline import run
        docs, errors = run(st, week, a.weeks, a.force, skip_registry=a.skip_registry)
        return 1 if any(v[0] for v in errors.values()) else 0

    if a.cmd == "package":
        from .pipeline import package
        files, warns = package(st, week)
        for f in files:
            print(f)
        for w in warns:
            print("⚠", w)
        return 0

    if a.cmd == "validate":
        from .validate import check_file
        rc = 0
        for f in a.files:
            errs, warns = check_file(f, st.c["extraction"]["lead_min_chars"], st.c["thresholds"]["intake_min"])
            print(f"{f}: {'통과' if not errs else '실패'}")
            for x in errs:
                print("  ❌", x)
            for x in warns:
                print("  ⚠", x)
            rc |= bool(errs)
        return rc

    if a.cmd == "check-sources":
        from .fetch import Fetcher
        from .registry import crawl_source
        f = Fetcher(st.c)
        s, e = weekly_window(default_issue_week(), 1)
        print("| id | 방식 | HTTP | 항목 | 최근 기간 채택 | 비고 |")
        print("|---|---|---|---|---|---|")
        for src in st.sources:
            if a.id and src["id"] not in a.id:
                continue
            _, log = crawl_source(src, f, s, e, 30, 1000)
            print(f"| {src['id']} | {src['method']} | {log['status']} | {log['items']} | {log['kept']} | {log['note']} |")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

