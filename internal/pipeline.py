"""사내 파이프라인 상태 기계(설계서 9.1). 단계마다 review/{issue_week}-state.json에 저장하므로 중단 후 이어서 실행할 수 있다.

단계: S1 → S2 → S3 → S4 → S5 → R1(검토 요청 ①) → S6 → S7 → R2(검토 요청 ②) → R3(발송 확인) → S8 → DONE
검토 지점에서는 사용자가 요청하지 않아도 9.2 형식의 검토 요청을 먼저 출력하고 응답을 기다린다.
"""
import copy
import json
import sys
from datetime import datetime

from . import checks, draft as draftmod, fingerprint, glossary as glossmod, events as ev_mod, fit as fit_mod, metrics, render, review as rv, score as sc_mod
from . import select as sel
from .common import add_flag
from .draft import SUMMARY_SCHEMA, load_draft, save_draft
from .intake import IntakeError, load_package
from .llm import AutoJudgeFailed, Pending, ResponseCache, is_truncated
from .review import ReviewError
from .reviewlog import ReviewLog, now_iso
from .s7 import run_s7
from .settings import TOPICS

STAGES = ["S1", "S2", "S3", "S4", "S5", "R1", "S6", "S7", "R2", "R3", "S8", "DONE"]
STEP_NAME = {"S1": "S1 반입 검증", "S2": "S2 1단계 적합도", "S3": "S3 사건 묶음·대장 대조", "S4": "S4 2단계 중요도",
             "S5": "S5 자동 편성", "S6": "S6 원고·머리부 초안", "S7": "S7 원고 자동 검사", "S8": "S8 발송·대장 갱신"}
TOPIC_NAME = {"T1_CHIPMAKER": "칩메이커", "T2_EQUIPMENT": "반도체 장비사", "T3_SOLUTION": "장비사 외 제조 솔루션 공급사",
              "T4_PHYSICAL_AI": "Physical AI"}


class Interrupted(Exception):
    """사용자가 중단했거나 입력이 끊김. 상태는 저장돼 있다."""


class Restart(Exception):
    """사용자가 '처음부터'를 입력함."""


class NeedAnswer(Exception):
    """검토 요청에 답할 사람이 없는 환경(표준입력이 터미널이 아니고 --answer도 없음)."""


class ConsoleIO:
    """answers: --answer로 받은 응답(앞에서부터 한 번씩 쓴다). interactive가 거짓이면 입력을 기다리지 않고 NeedAnswer를 낸다."""

    def __init__(self, answers=None, interactive=None):
        self.answers = list(answers or [])
        self.interactive = sys.stdin.isatty() if interactive is None else interactive

    def write(self, text=""):
        print(text, flush=True)

    def read(self):
        if self.answers:
            return self.answers.pop(0)
        if not self.interactive:
            raise NeedAnswer()
        try:
            return input()
        except EOFError:
            raise Interrupted()


class Pipeline:
    def __init__(self, settings, judge, io=None, clock=None, fresh=False):
        self.fresh = fresh
        self.s = settings
        self.cfg = {**settings.internal, "media_source_types": settings.config["collection"]["media_source_types"]}
        self.judge = judge
        if judge is not None and settings.llm_cfg.get("max_input_chars"):
            judge.max_input_chars = settings.llm_cfg["max_input_chars"]
        self.io = io or ConsoleIO()
        self.clock = clock or now_iso
        self.state = None
        self.week = None
        self.pkg = None
        self.log = None

    # 진입점 -------------------------------------------------------------------

    def start(self, package_dir, issue_week=None, restart=False):
        """새로 시작한다. 반환: 종료 코드(0 정상, 1 반입 실패·기존 상태 충돌)."""
        try:
            pkg = load_package(package_dir, self.s.lead_min_chars, self.s.intake_min, issue_week)
        except IntakeError as e:
            self.io.write(f"S1 반입 검증 …")
            self.io.write(str(e))
            return 1
        week = pkg.issue_week
        path = self.s.state_path(week)
        if path.exists() and not restart:
            old = json.loads(path.read_text(encoding="utf-8"))
            self.io.write(f"{week}의 진행 상태가 이미 있습니다(단계 {old['stage']}). "
                          "이어서 하려면 `python -m internal resume`, 처음부터 다시 하려면 `--restart`를 붙이세요.")
            return 1
        self.week, self.pkg = week, pkg
        self.log = ReviewLog(self.s.review_log_path(week), self.clock)
        self.attach_cache()
        if restart and path.exists():
            self.log.write(kind="restart", review="-")
        self.state = {"version": 1, "issue_week": week, "package_dir": str(pkg.directory), "stage": "S1",
                      "created_at": self.clock(), "updated_at": self.clock(), "issue_type": None, "fit": {}, "events": [],
                      "counts": {"pass_docs": 0, "events": 0, "candidates": 0, "core_eligible": 0}, "warnings": [],
                      "failed_gen": [], "metrics": {}, "review_log": [], "intake_warnings": pkg.warnings,
                      "fingerprint": {}, "fp_changes": []}
        return self._run()

    def resume(self, issue_week=None):
        week = issue_week or self.latest_unfinished()
        if not week:
            self.io.write("이어서 할 진행 상태가 없습니다. `python -m internal run --package DIR`로 시작하세요.")
            return 1
        path = self.s.state_path(week)
        if not path.exists():
            self.io.write(f"{week}의 진행 상태가 없습니다.")
            return 1
        self.state = json.loads(path.read_text(encoding="utf-8"))
        self.week = week
        if self.state["stage"] == "DONE":
            self.io.write(f"{week}은(는) 이미 발송본 생성까지 끝났습니다.")
            return 0
        try:
            self.pkg = load_package(self.state["package_dir"], self.s.lead_min_chars, self.s.intake_min, week)
        except IntakeError as e:
            self.io.write(str(e))
            return 1
        self.log = ReviewLog(self.s.review_log_path(week), self.clock)
        self.attach_cache()
        self.refresh_fingerprint()
        return self._run()

    def attach_cache(self):
        """모델 응답 저장(9.11). --fresh이면 저장된 응답을 읽지 않는다. --restart는 저장을 지우지 않는다."""
        if self.judge is not None and hasattr(self.judge.client, "set_dir"):
            self.judge.client.set_dir(self.s.review_path(self.week, "llm"))
        if self.judge is not None:
            self.judge.cache = ResponseCache(self.s.review_path(self.week, "llm-cache.jsonl"), read=not self.fresh)

    def refresh_fingerprint(self):
        """실행 지문을 갱신하고 직전 호와 비교한다. 모델 이름은 첫 호출 뒤에야 알 수 있어 S5에서 다시 비교한다."""
        old = self.state.get("fingerprint") or {}
        fp = fingerprint.current(self.s, old.get("model"))
        self.state["fingerprint"] = fp
        self.state["fp_changes"] = fingerprint.changed(fingerprint.previous(self.s.pilot_csv, self.week), fp)

    def latest_unfinished(self):
        best = None
        for p in sorted(self.s.review_dir.glob("*-state.json")) if self.s.review_dir.exists() else []:
            st = json.loads(p.read_text(encoding="utf-8"))
            if st["stage"] != "DONE" and (best is None or st["updated_at"] > best[0]):
                best = (st["updated_at"], st["issue_week"])
        return best[1] if best else None

    def statuses(self):
        out = []
        for p in sorted(self.s.review_dir.glob("*-state.json")) if self.s.review_dir.exists() else []:
            st = json.loads(p.read_text(encoding="utf-8"))
            out.append((st["issue_week"], st["stage"], st.get("issue_type"), st["updated_at"]))
        return out

    # 상태 기계 ------------------------------------------------------------------

    @property
    def records(self):
        """반입 자료. 이미지 묶음이 없으면 이미지 후보를 쓰지 않도록 media를 비운 사본을 돌려준다."""
        recs = self.pkg.records
        if self.pkg.media_zip is None and any(r.get("media") for r in recs.values()):
            return {d: dict(r, media=[]) for d, r in recs.items()}
        return recs

    def save(self):
        self.state["updated_at"] = self.clock()
        p = self.s.state_path(self.week)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def goto(self, stage):
        self.state["stage"] = stage
        self.save()

    def _run(self):
        while True:
            try:
                while self.state["stage"] != "DONE":
                    getattr(self, "step_" + self.state["stage"])()
                return 0
            except Pending as p:
                self.save()
                for path in p.paths:
                    self.io.write(f"모델 응답 대기: {path}")
                self.io.write("각 요청 파일 옆에 같은 번호의 .response.json(모델이 돌려줄 JSON 객체)을 쓴 뒤 "
                              "`python -m internal resume --llm-backend file`로 이어서 진행하세요.")
                return 3
            except NeedAnswer:
                self.save()
                self.io.write("검토 요청에 답이 필요합니다. 위 요청을 확인하고 "
                              "`python -m internal resume --answer \"응답\"`으로 이어서 진행하세요.")
                return 4
            except Interrupted:
                self.save()
                self.io.write("중단했습니다. `python -m internal resume`으로 이어서 진행할 수 있습니다.")
                return 2
            except Restart:
                self.log.write(kind="restart", review="-")
                self.state.update(stage="S1", fit={}, events=[], warnings=[], metrics={}, review_log=[], failed_gen=[],
                                  issue_type=None, pending_review=None)
                self.save()

    def step_header(self, stage, note=""):
        self.io.write(f"{STEP_NAME[stage]} …{(' ' + note) if note else ''}")

    # S1~S5 -----------------------------------------------------------------------

    def step_S1(self):
        self.step_header("S1", self.pkg.media_note if self.pkg.media_zip is None and "없어" in self.pkg.media_note else "")
        self.refresh_fingerprint()
        if self.state["fp_changes"]:
            self.io.write(fingerprint.notice(self.state["fp_changes"]))
        self.goto("S2")

    def step_S2(self):
        if not self.records:
            self.step_header("S2", "생략(반입 자료 0건)")
            self.state["issue_type"] = "현황호"
            self.goto("S3")
            return
        self.step_header("S2")
        self.state["fit"] = fit_mod.run_fit(self.judge, self.records)
        self.goto("S3")

    def step_S3(self):
        if not self.records:
            self.step_header("S3", "생략")
            self.goto("S4")
            return
        self.step_header("S3")
        events = ev_mod.build_events(self.judge, self.records, self.state["fit"], self.week, self.cfg["article_title_max"])
        ledger = ev_mod.load_ledger(self.s.ledger_path)
        ev_mod.match_ledger(self.judge, events, self.records, ledger, self.week, self.cfg["ledger_lookback_issues"])
        self.state["events"] = events
        self.goto("S4")

    def step_S4(self):
        if not self.records:
            self.step_header("S4", "생략")
            self.goto("S5")
            return
        self.step_header("S4")
        sc_mod.score_events(self.judge, self.state["events"], self.records, self.state["fit"], self.cfg)
        for e in self.state["events"]:
            if e["status"] == "PENDING":      # 방어: 채점되지 않은 사건은 보류로 둔다
                e.update(status="HOLD", note="채점 누락")
        self.goto("S5")

    def refresh_fingerprint_model(self):
        model = self.judge.served_model if self.judge is not None else None
        if model:
            self.state["fingerprint"]["model"] = model
            prev = fingerprint.previous(self.s.pilot_csv, self.week)
            self.state["fp_changes"] = fingerprint.changed(prev, self.state["fingerprint"])

    def counts(self):
        evs = self.state["events"]
        return {"pass_docs": sum(1 for f in self.state["fit"].values() if f["decision"] == "ADVANCE"),
                "events": sum(1 for e in evs if e["kind"] == "event"),
                "candidates": sum(1 for e in evs if e["slot"] in ("core", "brief")),
                "core_eligible": len(sel.core_eligible(evs, self.cfg))}

    def step_S5(self):
        self.step_header("S5")
        evs = self.state["events"]
        self.refresh_fingerprint_model()
        limit = self.s.llm_cfg.get("max_input_chars")
        for e in evs:     # 입력이 잘린 문서가 있는 사건은 표시한다(9.11)
            if any(is_truncated(self.records[d], limit) for d in e["doc_ids"]):
                add_flag(e, "본문 일부만 판정")
        if evs:
            sel.select(evs, self.cfg, self.records)
        for e in evs:
            e["auto_slot"], e["auto_rank"] = e["slot"], e["rank"]
        self.state["counts"] = self.counts()
        sel.write_csv(self.s.selection_path(self.week), evs, self.records)
        self.goto("R1")

    # 사용자 입력 ----------------------------------------------------------------------

    def ask(self):
        text = self.io.read()
        t = (text or "").strip()
        if t in ("중단", "stop"):
            raise Interrupted()
        if t == "처음부터":
            raise Restart()
        return text

    def request(self, review, text):
        self.io.write(text)
        self.log.write(kind="request", review=review, text=text)

    def respond(self, review, resp, valid=True, error=None):
        self.log.write(kind="response", review=review, text=resp, valid=valid, error=error)

    def loop_review(self, review, render_fn, handle):
        """요청 출력 → 응답 → 해석. 해석할 수 없으면 오류 한 줄과 같은 요청을 다시 출력한다. handle(text)이 결과를 돌려주면 끝."""
        while True:
            self.request(review, render_fn())
            text = self.ask()
            try:
                result = handle(text)
            except ReviewError as e:
                self.respond(review, text, False, str(e))
                self.io.write(f"오류: {e}")
                continue
            self.respond(review, text, True)
            return result

    # 검토 요청 ① -----------------------------------------------------------------------

    def step_R1(self):
        evs = self.state["events"]
        if not evs:
            self.log.write(kind="skipped", review="R1", text="사건 없음")
            self.finish_r1(None)
            return

        def render_fn():
            return rv.render_review1(self.week, self.state["counts"], self.state["events"],
                                     fingerprint.notice(self.state["fp_changes"]), self.cfg)

        def handle(text):
            parsed = rv.parse_review1(text)
            if parsed["approve"]:
                return {"events": self.state["events"], "stats": {"changed": 0, "lifted": 0, "lowered": 0, "ledger_changes": 0},
                        "approved": True, "text": text}
            return self.r1_apply(text, {})

        pend = self.state.get("pending_review")
        if pend and pend["review"] == "R1":      # 상한 확인에서 끊긴 검토 요청 ①: 처음부터 묻지 않고 그 질문에 이어서 답한다
            result = self.r1_apply(pend["text"], pend["answers"])
        else:
            result = self.loop_review("R1", render_fn, handle)
        self.state["pending_review"] = None
        self.finish_r1(result)

    def r1_apply(self, text, answers):
        """검토 요청 ①의 '바꿔서 승인' 응답을 적용한다. 상한 확인이 필요하면 진행 상황을 state에 남기고 한 건씩 묻는다."""
        parsed = rv.parse_review1(text)
        while True:
            new, violations, stats = rv.apply_review1(self.state["events"], parsed["ops"], answers)
            if not violations:
                break
            v = violations[0]
            self.state["pending_review"] = {"review": "R1", "text": text, "answers": dict(answers), "asking": v["event_id"]}
            self.request("R1-cap", rv.render_cap_check(v["label"], v["reason"]))
            while True:
                ans = self.ask()
                try:
                    answers[v["event_id"]] = {1: "keep", 2: "override"}[rv.parse_choice(ans)]
                    self.respond("R1-cap", ans, True)
                    break
                except ReviewError as e:
                    self.respond("R1-cap", ans, False, str(e))
                    self.io.write(f"오류: {e}")
                    self.request("R1-cap", rv.render_cap_check(v["label"], v["reason"]))
        return {"events": new, "stats": stats, "approved": False, "text": text}

    def finish_r1(self, result):
        if result is not None:
            self.state["events"] = result["events"]
            st = result["stats"]
            self.state["metrics"]["r1"] = {"approved_as_is": int(result["approved"]), "changed": st["changed"],
                                           "lifted": st["lifted"], "lowered": st["lowered"],
                                           "ledger_changes": st["ledger_changes"]}
            self.state["review_log"].append({"review": "①", "at": self.clock(), "response": result["text"].strip(),
                                             "applied": f"변경 {st['changed']}건, 올림 {st['lifted']}건, 내림 {st['lowered']}건"})
            self.log.write(kind="applied", review="R1", stats=self.state["metrics"]["r1"])
            self.io.write("[적용 결과] " + ("자동 편성안 그대로 승인" if result["approved"] else "편성을 바꿔 승인"))
            self.io.write(rv.render_table(self.state["events"]))
            sel.write_csv(self.s.selection_path(self.week), self.state["events"], self.records)
        self.state["counts"] = self.counts()
        self.state["issue_type"] = "현황호" if not self.records else sel.issue_type(self.state["events"], self.cfg)
        self.goto("S6")

    # S6·S7 ----------------------------------------------------------------------------

    def header_counts(self):
        c = self.state["counts"]
        return {"통과_문서": c["pass_docs"], "게재_사건": len([e for e in self.state["events"] if e["slot"] in ("core", "brief")])}

    def step_S6(self):
        it = self.state["issue_type"]
        evs = self.state["events"]
        excluded = sum(len(e["doc_ids"]) for e in evs if e["status"] == "EXCLUDE")   # 꼬리말의 '그 밖에 검토한 자료'
        if it == "현황호":
            self.step_header("S6", "생략(게재 사건 없음, 현황호)")
            d = draftmod.status_draft(self.week, {"collected": len(self.records), "pass_docs": self.state["counts"]["pass_docs"],
                                                  "events": self.state["counts"]["events"]}, excluded)
            self.state["failed_gen"] = []
        else:
            self.step_header("S6")
            more, overflow = sel.more_candidates(evs, self.state["fit"], self.records, self.cfg, self.judge)
            self.state["more_overflow"] = overflow
            d, failed = draftmod.generate_draft(self.judge, evs, self.records, self.week, self.cfg, it, self.header_counts(),
                                                more, excluded)
            self.state["failed_gen"] = failed
        d["review_log"] = list(self.state["review_log"])
        save_draft(self.s.draft_path(self.week), d)
        self.goto("S7")

    def step_S7(self):
        d = load_draft(self.s.draft_path(self.week))
        if self.state["issue_type"] == "현황호":
            self.step_header("S7", "생략")
            self.state["warnings"] = []
        else:
            self.step_header("S7")
            d, warns = run_s7(self.judge, d, self.state["events"], self.records, self.cfg, self.state["issue_type"],
                              self.header_counts(), self.state["failed_gen"], self.glossary_data())
            self.state["warnings"] = warns
            save_draft(self.s.draft_path(self.week), d)
        self.goto("R2")

    def glossary_data(self):
        """(glossary.yaml의 용어, 공통어). 저장소 루트의 glossary.yaml을 쓰고, data_root가 다르면 복사본을 쓴다."""
        return glossmod.load(self.s.ensure_glossary())

    # 검토 요청 ② -----------------------------------------------------------------------

    def image_map(self, d):
        """핵심 기사별 대표 이미지 후보 {articles 인덱스: 후보 목록}. 이미지 묶음이 없으면 후보가 없다."""
        by_id = {e["event_id"]: e for e in self.state["events"]}
        return {i: draftmod.image_candidates(by_id[a["event_id"]], self.records, self.cfg)
                for i, a in enumerate(d["articles"]) if a["slot"] == "core"}

    def fix_swapped_images(self, d, swaps):
        """검토 요청 ②에서 바꾼 이미지의 설명을 모델로 새로 쓴다. 실패하면 후보의 대체 텍스트를 줄여 쓰고 알린다."""
        for i, cand in swaps:
            cap = draftmod.gen_image_caption(self.judge, d["articles"][i], cand, self.cfg)
            if not cap:
                cap = checks.strip_urls((cand["alt"] or cand["caption"] or "이미지")[: self.cfg["image_caption_chars"]])[0]
                self.io.write(f"오류: {i + 1}번째 기사의 이미지 설명 자동 작성에 실패해 원문 설명을 줄여 넣었습니다. 발송 전에 확인하세요.")
            d["articles"][i]["image"]["caption"] = cap

    def final_warnings(self, d):
        """② 적용 뒤 원고를 다시 검사한다(모델 호출 없음)."""
        if self.state["issue_type"] == "현황호":
            return []
        by_id = {e["event_id"]: e for e in self.state["events"]}
        warns = checks.autofix(d, self.records)
        terms, common = self.glossary_data()       # F로 항목을 빼면 쓰지 않게 된 용어가 있을 수 있어 목록을 다시 만든다(모델 호출 없음)
        d["glossary"] = glossmod.build_glossary(d, terms, [g for g in d.get("glossary") or [] if g["source"] != "glossary"])
        for tgt, kind, msg in checks.hard_violations(d, by_id, self.records, self.cfg, self.state["issue_type"]):
            warns.append({"scope": "머리부" if tgt[0] == "header" else "기사", "quote": "", "kind": kind, "message": msg, "fix": None})
        warns += checks.soft_warnings(d, by_id, self.records, self.cfg, self.state["issue_type"])
        return checks.number_ids(warns)

    def rewrite_summary(self, d, instruction):
        user = ("[현재 총론]\n" + d["summary"] + "\n[편집자 입력]\n" + instruction + "\n[글자 수 범위]\n"
                + json.dumps(self.cfg["summary_chars"]) + "\n"
                + "\n".join(f"[기사] {a['title']}: {a['body']}" for a in d["articles"])
                + "\n" + "\n".join(f"[더 볼 거리] {m['line']}" for m in d.get("more") or []))
        try:
            r = self.judge.ask("s6_summary", user, SUMMARY_SCHEMA)
        except AutoJudgeFailed:
            self.io.write("오류: 총론 자동 수정에 실패해 기존 초안을 유지합니다.")
            return d["summary"]
        return checks.strip_urls(r["summary"])[0].strip()

    def step_R2(self):
        d = load_draft(self.s.draft_path(self.week))
        warns = self.state["warnings"]

        def render_fn():
            return rv.render_review2(self.week, d, warns, self.cfg, self.image_map(d))

        def handle(text):
            res = rv.parse_review2(text)
            rv.validate_review2(res, d, warns, self.cfg, self.image_map(d))
            return res, text

        pend = self.state.get("pending_review")
        if pend and pend["review"] == "R2":      # B 재수정 확인에서 끊김: ② 응답은 이미 적용돼 있으므로 그 질문에 이어서 답한다
            text, stats, instruction, b_rev = pend["text"], pend["stats"], pend["instruction"], pend["c_rev"]
        else:
            res, text = self.loop_review("R2", render_fn, handle)
            stats = rv.apply_review2(d, res, warns, self.image_map(d))
            self.fix_swapped_images(d, stats.pop("image_swaps"))
            instruction, b_rev = stats["b_revise"], 0
            save_draft(self.s.draft_path(self.week), d)
        while instruction is not None:
            self.state["pending_review"] = {"review": "R2", "text": text, "stats": stats, "instruction": instruction, "c_rev": b_rev}
            new = self.rewrite_summary(d, instruction)
            self.request("R2-B", rv.render_b_confirm(new))
            while True:
                ans = self.ask()
                try:
                    kind, val = rv.parse_b_confirm(ans)
                    self.respond("R2-B", ans, True)
                    break
                except ReviewError as e:
                    self.respond("R2-B", ans, False, str(e))
                    self.io.write(f"오류: {e}")
                    self.request("R2-B", rv.render_b_confirm(new))
            b_rev += 1
            d["summary"] = new
            d["summary_candidates"] = [new] + [c for c in d["summary_candidates"] if c != new]
            instruction = None if kind == "approve" else val
            self.state["pending_review"] = None if instruction is None else {
                "review": "R2", "text": text, "stats": stats, "instruction": instruction, "c_rev": b_rev}
            save_draft(self.s.draft_path(self.week), d)
        self.state["pending_review"] = None
        self.state["warnings"] = self.final_warnings(d)
        prev = self.state["metrics"].get("r2")
        merged = {**stats, "b_revisions": b_rev}
        if prev:    # 발송 확인에서 ②로 돌아온 경우: 지표를 누적한다(제목 변경은 기사 단위로 한 번만 센다)
            ids = sorted(set(prev["d_ids"]) | set(stats["d_ids"]))
            merged.update(A_custom=prev["A_custom"] or stats["A_custom"], B_custom=prev["B_custom"] or stats["B_custom"],
                          b_revisions=prev["b_revisions"] + b_rev, d_ids=ids, d_changed=len(ids), e_total=prev["e_total"],
                          e_fix=prev["e_fix"] + stats["e_fix"], e_keep=prev["e_keep"] + stats["e_keep"],
                          f_removed=prev.get("f_removed", 0) + stats["f_removed"],
                          g_edited=prev.get("g_edited", 0) + stats["g_edited"])
        self.state["metrics"]["r2"] = merged
        d["review_log"].append({"review": "②", "at": self.clock(), "response": text.strip(),
                                "applied": f"A·B 확정, B {'수정 ' + str(b_rev) + '회' if b_rev else '승인'}, "
                                           f"제목 변경 {stats['d_changed']}건, 경고 수정 {stats['e_fix']}건·유지 {stats['e_keep']}건"})
        save_draft(self.s.draft_path(self.week), d)
        self.log.write(kind="applied", review="R2", stats=self.state["metrics"]["r2"])
        self.io.write(f"[적용 결과] 호 타이틀: {d['issue_title']}")
        self.io.write(f" 한 줄 요약(총론): {d['summary']}")
        self.io.write(f" 이번 주 한 방: {d['punch']['big'] if d.get('punch') else '없음'} / 새 용어 풀이 {len(rv.new_terms(d))}건 / 제목 변경 {stats['d_changed']}건 / "
                      f"경고 수정 {stats['e_fix']}건 · 유지 {stats['e_keep']}건")
        self.goto("R3")

    # 발송 확인 -------------------------------------------------------------------------

    def step_R3(self):
        d = load_draft(self.s.draft_path(self.week))
        sources = len({c for a in d["articles"] for c in a["credits"]})
        placeholders = checks.placeholder_hits(d)

        def render_fn():
            return rv.render_review3(self.week, {
                "title": d["issue_title"], "summary": d["summary"], "summary_len": len(d["summary"]),
                "core": sum(a["slot"] == "core" for a in d["articles"]), "brief": sum(a["slot"] == "brief" for a in d["articles"]),
                "body_len": checks.body_total(d), "body_max": self.cfg["issue_body_max"], "sources": sources,
                "warnings": len(self.state["warnings"]), "placeholders": placeholders})

        choice = self.loop_review("R3", render_fn, lambda text: rv.parse_review3(text, placeholders))
        d["review_log"].append({"review": "발송 확인", "at": self.clock(), "response": str(choice),
                                "applied": "발송본 생성·확정" if choice == 1 else "검토 ②로 돌아감"})
        save_draft(self.s.draft_path(self.week), d)
        self.goto("S8" if choice == 1 else "R2")

    # S8 ---------------------------------------------------------------------------------

    def status_rows(self):
        fit = self.state["fit"]
        rows = []
        for t in TOPICS:
            direct = sum(1 for f in fit.values() if f["decision"] == "ADVANCE" and (f.get("stage2") or f["stage1"])["fit"][t] == "2")
            rows.append((TOPIC_NAME[t], f"직접 해당 {direct}건"))
        return rows

    def reason_note(self):
        if not self.records:
            return "이번 주 사외 수집에서 완결된 자료가 없었습니다."
        evs = self.state["events"]
        n = lambda f: sum(1 for e in evs if f(e))
        parts = [f"수집 {len(self.records)}건"]
        for label, cond in (("판단 보류", lambda e: e["slot"] == "hold"), ("1단계 제외", lambda e: e["kind"] == "excluded_doc"),
                            ("재전재·중복", lambda e: e["ledger"]["verdict"] == "SAME_NO_CHANGE"),
                            ("점수 미달·편성 한도 밖", lambda e: e["kind"] == "event" and e["slot"] == "out"
                             and e["ledger"]["verdict"] != "SAME_NO_CHANGE" and e["status"] == "PASS")):
            if n(cond):
                parts.append(f"{label} {n(cond)}건")
        return ", ".join(parts) + "."

    def load_image(self, media_ref):
        """발송본에 넣을 이미지: (원본 바이트, 발행처). 이미지 묶음이 없거나 항목이 없으면 None."""
        doc_id, _, mid = media_ref.partition("#")
        rec = self.pkg.records.get(doc_id)
        m = next((x for x in (rec or {}).get("media") or [] if x["id"] == mid), None)
        data = self.pkg.image_bytes(m["file"]) if m else None
        return (data, rec["publisher"]) if data else None

    def corrections(self):
        p = self.s.review_path(self.week, "corrections.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def step_S8(self):
        self.step_header("S8")
        d = load_draft(self.s.draft_path(self.week))
        it = self.state["issue_type"]
        pub = len([e for e in self.state["events"] if e["slot"] in ("core", "brief")])
        note = ""
        if it == "축약호":
            note = "게재 가능 사건이 3건 미만이거나 핵심 자격 사건이 없어 축약해 발행합니다."
        elif it == "현황호":
            note = self.reason_note()
        corrections = self.corrections()
        ctx = {"issue_week": self.week, "period": render.period_text(self.pkg.doc["window_start"], self.pkg.doc["window_end_exclusive"]),
               "issue_type": it, "counts": {"pass_docs": self.state["counts"]["pass_docs"], "published": pub},
               "events": self.state["events"], "status_rows": self.status_rows() if it == "현황호" else [],
               "corrections": corrections, "note": note, "newsletter_name": self.cfg["newsletter_name"],
               "newsletter_tagline": self.cfg["newsletter_tagline"], "newsletter_team": self.cfg["newsletter_team"],
               "theme": self.cfg.get("newsletter_theme", "A"),
               "period_short": render.period_short(self.pkg.doc["window_start"], self.pkg.doc["window_end_exclusive"]),
               "issue_no": metrics.issue_number(self.s.pilot_csv, self.week), "n_docs": len(self.records)}
        ctx["competitors"] = [c["name"] for c in self.cfg.get("competitors") or []]     # B7 레이더: 이번 주 소식 없는 경쟁사
        ctx.update(image_loader=self.load_image, image_max_kb=self.cfg["image_max_kb"], newsletter_from=self.cfg.get("newsletter_from", ""),
                   read_chars_per_min=self.cfg.get("read_chars_per_min", 500))
        built = render.build_all(d, ctx)
        left = checks.placeholder_hits(d, built["text"])     # 마지막 차단: 완성된 발송본에도 가상·견본 흔적이 없어야 한다
        if left:
            raise RuntimeError("발송본에 가상·견본 데이터 흔적이 있어 만들지 않았습니다: "
                               + ", ".join(f"{h['scope']} '{h['quote']}'" for h in left[:10]))
        out = self.s.out_dir(self.week)
        out.mkdir(parents=True, exist_ok=True)
        (out / "newsletter.html").write_text(built["html_preview"], encoding="utf-8")       # 미리 보기: 이미지를 data URI로 내장
        (out / "newsletter.txt").write_text(built["text"], encoding="utf-8")
        (out / "newsletter.eml").write_bytes(render.build_eml(d, ctx, built).as_bytes())     # 기본 발송 파일: 편집자가 열어 수신자를 넣고 보낸다
        # 발송 확정: 이번 호에서 확정한 새 용어(모델 제안·편집자 입력)를 glossary.yaml에 덧붙인다(주석·순서 보존)
        new_terms = [(g["term"], g["meaning"]) for g in d.get("glossary") or [] if g["source"] != "glossary" and g.get("meaning")]
        if new_terms:
            glossmod.append_terms(self.s.ensure_glossary(), new_terms, self.week)
        entries = ev_mod.load_ledger(self.s.ledger_path)
        ev_mod.save_ledger(self.s.ledger_path, ev_mod.update_ledger(entries, self.state["events"], self.week, corrections))
        row = metrics.build_row(self.state, self.pkg.doc, self.log, str(self.s.config.get("config_version", "")))
        row.update({"fp_" + k: v for k, v in self.state["fingerprint"].items()})
        metrics.upsert_row(self.s.pilot_csv, row)
        self.goto("DONE")
        self.io.write(f"발송본: {out / 'newsletter.eml'} (미리 보기 {out / 'newsletter.html'})")
