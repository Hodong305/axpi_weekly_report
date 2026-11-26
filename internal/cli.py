"""python -m internal run|resume|status"""
import argparse
import sys

from . import settings as settings_mod
from .llm import Judge, LLMClient
from .pipeline import ConsoleIO, Pipeline


def build_parser():
    p = argparse.ArgumentParser(prog="python -m internal", description="사내 소식지 파이프라인(설계서 v4.5 9장)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-root", default=argparse.SUPPRESS, help="review/·ledger/·out_internal/·log/ 를 둘 폴더(기본: 저장소 루트)")
    p.add_argument("--data-root", default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common], help="패키지를 반입해 처음부터 진행")
    r.add_argument("--package", required=True, help="사외 package/ 폴더(intake·report·manifest)")
    r.add_argument("--issue-week", help="예: 2026-W49 (생략하면 manifest 값)")
    r.add_argument("--restart", action="store_true", help="저장된 진행 상태를 버리고 처음부터(모델 응답 저장은 유지)")
    r.add_argument("--llm-backend", choices=["http", "file"], help="모델 백엔드(기본: config.yaml의 internal.llm.backend)")
    r.add_argument("--fresh", action="store_true", help="저장된 모델 응답을 쓰지 않고 새로 호출")
    rs = sub.add_parser("resume", parents=[common], help="중단된 진행을 이어서 실행")
    rs.add_argument("--issue-week")
    rs.add_argument("--llm-backend", choices=["http", "file"], help="모델 백엔드(기본: config.yaml의 internal.llm.backend)")
    rs.add_argument("--answer", action="append", help="대기 중인 검토 요청에 줄 응답(표준입력이 터미널이 아닐 때 쓴다). 여러 번 쓰면 질문 순서대로 쓴다(상한 확인 등 이어지는 질문)")
    rs.add_argument("--fresh", action="store_true", help="저장된 모델 응답을 쓰지 않고 새로 호출")
    st = sub.add_parser("status", parents=[common], help="진행 상태 보기")
    st.add_argument("--issue-week")
    return p


def main(argv=None, judge=None, io=None):
    args = build_parser().parse_args(argv)
    s = settings_mod.load(data_root=args.data_root)
    if args.cmd == "status":
        rows = Pipeline(s, None, io or ConsoleIO()).statuses()
        rows = [r for r in rows if not args.issue_week or r[0] == args.issue_week]
        if not rows:
            print("진행 상태가 없습니다.")
        for week, stage, itype, at in rows:
            print(f"{week}  단계 {stage}  {itype or '-'}  {at}")
        return 0
    backend = getattr(args, "llm_backend", None) or s.llm_cfg.get("backend", "http")
    if judge is None:
        if backend == "file":
            from .filellm import FileClient
            judge = Judge(FileClient())
        else:
            judge = Judge(LLMClient.from_config(s.llm_cfg))
    answer = getattr(args, "answer", None)
    pipe = Pipeline(s, judge, io or ConsoleIO(answer), fresh=args.fresh)
    if args.cmd == "run":
        return pipe.start(args.package, args.issue_week, args.restart)
    return pipe.resume(args.issue_week)


if __name__ == "__main__":
    sys.exit(main())
