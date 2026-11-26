"""파일 모드 모델 백엔드와 비대화형 검토 응답(--answer) 시험."""
import json

from internal import settings as settings_mod
from internal.cli import build_parser, main
from internal.filellm import FileClient
from internal.llm import Judge
from internal.pipeline import ConsoleIO, Pipeline

from test_internal import FakeClient, FakeIO, WEEK, make_package, sample_records


def requests_dir(tmp):
    return tmp / "review" / f"{WEEK}-llm"


def pending_requests(tmp):
    d = requests_dir(tmp)
    return sorted(p for p in d.glob("*.request.json") if not p.with_name(p.name.replace(".request.", ".response.")).exists())


def answer(tmp, fake, only=None, extra=None):
    """대기 중인 요청에 가짜 모델의 답을 응답 파일로 쓴다(사람·에이전트가 하는 일의 대역)."""
    n = 0
    for req in pending_requests(tmp):
        if only and only not in req.name:
            continue
        data = json.loads(req.read_text(encoding="utf-8"))
        obj = json.loads(fake.chat(data["messages"], data["json_schema"], data["step"]))
        if extra:
            obj.update(extra)
        req.with_name(req.name.replace(".request.", ".response.")).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        n += 1
    return n


def file_pipeline(tmp, io):
    return Pipeline(settings_mod.load(data_root=tmp), Judge(FileClient()), io)


def test_file_mode_pending_answer_and_continue(tmp_path):
    pkg = make_package(tmp_path, sample_records())
    fake = FakeClient()
    io = FakeIO(["1", "승인", "1"])
    assert file_pipeline(tmp_path, io).start(pkg) == 3
    waiting = [l for l in io.out if l.startswith("모델 응답 대기: ")]
    assert len(waiting) == 6                                  # 문서 6건의 1단계 요청을 한꺼번에 내보낸다
    req = json.loads(pending_requests(tmp_path)[0].read_text(encoding="utf-8"))
    assert set(req) == {"step", "system", "messages", "json_schema", "cache_key"} and req["step"] == "s2_fit"
    assert req["messages"][0]["role"] == "system" and req["messages"][-1]["content"].startswith("<source doc_id=")
    assert pending_requests(tmp_path)[0].name.startswith("0001-s2_fit.")
    state = json.loads((tmp_path / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "S2"
    codes, rounds = [3], 0
    while codes[-1] == 3 and rounds < 20:
        assert answer(tmp_path, fake) > 0
        codes.append(file_pipeline(tmp_path, io).resume())
        rounds += 1
    assert codes[-1] == 0 and codes[0] == 3
    assert (tmp_path / "out_internal" / WEEK / "newsletter.html").exists()
    state = json.loads((tmp_path / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))
    assert state["fingerprint"]["model"] == "file"
    cache = (tmp_path / "review" / f"{WEEK}-llm-cache.jsonl").read_text(encoding="utf-8")
    assert '"model": "file"' in cache                         # 응답은 같은 응답 저장소에 들어간다


def test_file_mode_model_field_and_replay(tmp_path):
    pkg = make_package(tmp_path, sample_records())
    fake = FakeClient()
    io = FakeIO([])
    file_pipeline(tmp_path, io).start(pkg)
    answer(tmp_path, fake, extra={"model": "hand-written"})
    seqs_before = json.loads((requests_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    file_pipeline(tmp_path, FakeIO([])).resume()               # 다음 요청이 나올 때까지 진행
    idx = json.loads((requests_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert all(idx[k] == v for k, v in seqs_before.items())     # 같은 요청은 같은 번호를 쓴다
    cache = (tmp_path / "review" / f"{WEEK}-llm-cache.jsonl").read_text(encoding="utf-8")
    assert '"model": "file:hand-written"' in cache and '"model"' in cache


def test_file_mode_invalid_response_writes_retry_request(tmp_path):
    pkg = make_package(tmp_path, sample_records())
    fake = FakeClient()
    io = FakeIO([])
    file_pipeline(tmp_path, io).start(pkg)
    first = pending_requests(tmp_path)[0]
    answer(tmp_path, fake)
    resp = first.with_name(first.name.replace(".request.", ".response."))
    resp.write_text(json.dumps({"엉뚱한": 1}), encoding="utf-8")        # 스키마에 맞지 않는 응답
    io2 = FakeIO([])
    assert file_pipeline(tmp_path, io2).resume() == 3
    retry = [p for p in pending_requests(tmp_path) if p != first]
    assert any("이전 응답의 오류" in json.loads(p.read_text(encoding="utf-8"))["messages"][-1]["content"] for p in retry)
    assert all(not p.name.startswith(first.name[:4]) for p in retry) and any(l.startswith("모델 응답 대기: ") for l in io2.out)
    # 재시도 요청에도 틀린 답을 주면 '자동 판정 실패'로 처리하고 진행한다
    for p in retry:
        p.with_name(p.name.replace(".request.", ".response.")).write_text(json.dumps({"또": 2}), encoding="utf-8")
    code = file_pipeline(tmp_path, FakeIO([])).resume()
    state = json.loads((tmp_path / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))
    assert code == 3 and state["stage"] in ("S2", "S3")


def test_cli_backend_flag_and_answer_option():
    a = build_parser().parse_args(["run", "--package", "x", "--llm-backend", "file"])
    assert a.llm_backend == "file"
    a = build_parser().parse_args(["resume", "--llm-backend", "file", "--answer", "1"])
    assert a.llm_backend == "file" and a.answer == ["1"]
    assert settings_mod.load().llm_cfg["backend"] == "http"


def noninteractive(tmp, client, answers=()):
    return Pipeline(settings_mod.load(data_root=tmp), Judge(client), ConsoleIO(list(answers), interactive=False))


def test_answer_flow_exit_codes(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    assert noninteractive(tmp_path, client).start(pkg) == 4                   # 검토 요청 ①에서 답이 없어 멈춤
    out = capsys.readouterr().out
    assert "━━ 검토 요청 ① 선정" in out and "--answer" in out
    st = json.loads((tmp_path / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))
    assert st["stage"] == "R1"
    assert noninteractive(tmp_path, client, ["엉뚱"]).resume() == 4           # 해석 불가 → 오류와 같은 요청, 종료 코드 4
    out = capsys.readouterr().out
    assert "오류: " in out and out.count("━━ 검토 요청 ① 선정") == 2
    assert noninteractive(tmp_path, client, ["1"]).resume() == 4              # ① 적용 → ②에서 멈춤
    assert "━━ 검토 요청 ② 머리부·제목" in capsys.readouterr().out
    assert noninteractive(tmp_path, client, ["승인"]).resume() == 4
    assert "━━ 발송 확인" in capsys.readouterr().out
    assert noninteractive(tmp_path, client, ["1"]).resume() == 0
    assert (tmp_path / "out_internal" / WEEK / "newsletter.html").exists()


def test_main_resume_with_answer(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    assert main(["--data-root", str(tmp_path), "run", "--package", str(pkg)], judge=Judge(client)) == 4
    assert main(["--data-root", str(tmp_path), "resume", "--answer", "1"], judge=Judge(client)) == 4
    out = capsys.readouterr().out
    assert "[적용 결과] 자동 편성안 그대로 승인" in out and "━━ 검토 요청 ② 머리부·제목" in out
