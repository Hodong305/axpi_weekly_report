"""메일 발송 시험(네트워크 없음): smtplib.SMTP·SMTP_SSL을 가짜로 바꾼다. 실행: python -m pytest -q tests"""
import gzip
import json
import smtplib
from pathlib import Path

import pytest
import yaml

from collector import cli, mailer
from collector.settings import file_name

WEEK = "2026-W49"
SECRET = "s3cret-app-password"


class Stub:
    def __init__(self, root):
        self.root = Path(root)

    def out_dir(self, week):
        return self.root / "out" / week


class FakeSMTP:
    instances = []
    fail = False

    def __init__(self, host, port, **kw):
        self.host, self.port, self.kw = host, port, kw
        self.started, self.logged_in, self.sent = False, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        self.started = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise smtplib.SMTPAuthenticationError(535, ("계정 " + SECRET).encode())
        self.sent.append(msg)


class FakeSSL(FakeSMTP):
    pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setenv("MTCAX_SMTP_PASSWORD", SECRET)
    out = tmp_path / "out" / WEEK
    pkg = out / "package"
    pkg.mkdir(parents=True)
    (out / file_name(WEEK, "merged", 1)).write_text("{}")
    raw = json.dumps({"x": "가" * 50}).encode()
    base = file_name(WEEK, "intake", 1)
    (pkg / base).write_bytes(raw)
    (pkg / (base + ".gz")).write_bytes(gzip.compress(raw))
    (pkg / file_name(WEEK, "report", 1, "md")).write_text("# 리포트\n", encoding="utf-8")
    manifest = {"schema_version": "4.5", "issue_week": WEEK, "revision": 1, "window_weeks": 1,
                "summary": {"records": 7, "complete": 6, "partial": 1, "drop": 0, "paywall": 0},
                "files": [], "warnings": ["K10 complete 6건 < intake_min 10"]}
    (pkg / file_name(WEEK, "manifest", 1)).write_text(json.dumps(manifest), encoding="utf-8")
    cfg = {"smtp_host": "smtp.example.com", "smtp_port": 587, "security": "starttls", "username": "you@example.com",
           "from_addr": "you@example.com", "to_addrs": ["inbox@company.example", "b@company.example"], "max_total_mb": 20}
    cfgp = tmp_path / "mail.local.yaml"
    cfgp.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return Stub(tmp_path), cfgp, pkg, cfg


def test_send_builds_four_attachments_and_sent_file(env, capsys):
    st, cfgp, pkg, cfg = env
    rec = mailer.send(st, WEEK, cfgp)
    smtp = FakeSMTP.instances[0]
    assert smtp.started and smtp.logged_in == ("you@example.com", SECRET) and (smtp.host, smtp.port) == ("smtp.example.com", 587)
    msg = smtp.sent[0]
    assert msg["Subject"] == "[MTC-AX] 2026-W49 사외 수집 r1 · complete 6건"
    atts = list(msg.iter_attachments())
    assert [a.get_filename() for a in atts] == [
        "mtc-ax-2026W49-intake-r1.json.gz", "mtc-ax-2026W49-intake-r1.json", "mtc-ax-2026W49-report-r1.md", "mtc-ax-2026W49-manifest-r1.json"]
    assert [a.get_content_type() for a in atts] == ["application/gzip", "application/json", "text/markdown", "application/json"]
    body = msg.get_body().get_content()
    assert "2026-11-22 ~ 2026-11-29 00:00" in body and "complete 6" in body and "K10" in body and "python -m internal run --package" in body
    assert "mtc-ax-2026W49-intake-r1.json.gz" in body and len(__import__("re").findall(r"[0-9a-f]{64}", body)) == 4
    sent = json.loads((pkg / "sent-r1.json").read_text(encoding="utf-8"))
    assert sent["recipients"] == 2 and len(sent["attachments"]) == 4 and sent["message_id"].startswith("<")
    assert set(sent) == {"sent_at", "recipients", "message_id", "attachments"}
    text = (pkg / "sent-r1.json").read_text(encoding="utf-8") + capsys.readouterr().out
    assert SECRET not in text and "company.example" not in text and "you@example.com" not in text


def test_ssl_security_uses_smtp_ssl(env):
    st, cfgp, pkg, cfg = env
    cfgp.write_text(yaml.safe_dump(dict(cfg, security="ssl", smtp_port=465)), encoding="utf-8")
    mailer.send(st, WEEK, cfgp)
    assert isinstance(FakeSMTP.instances[0], FakeSSL) and not FakeSMTP.instances[0].started


def test_oversize_drops_original_then_refuses(env, tmp_path):
    st, cfgp, pkg, cfg = env
    big = pkg / "mtc-ax-2026W49-intake-r1.json"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    cfgp.write_text(yaml.safe_dump(dict(cfg, max_total_mb=1)), encoding="utf-8")
    mailer.send(st, WEEK, cfgp)
    msg = FakeSMTP.instances[0].sent[0]
    assert len(list(msg.iter_attachments())) == 3 and "원본 intake" in msg.get_body().get_content()
    assert json.loads((pkg / "sent-r1.json").read_text(encoding="utf-8"))["attachments"].count("mtc-ax-2026W49-intake-r1.json") == 0
    (pkg / "sent-r1.json").unlink()
    FakeSMTP.instances.clear()
    (pkg / "mtc-ax-2026W49-report-r1.md").write_bytes(b"y" * (2 * 1024 * 1024))     # 원본을 빼도 초과
    with pytest.raises(mailer.MailError, match="보내지 않습니다"):
        mailer.send(st, WEEK, cfgp)
    assert FakeSMTP.instances == [] and not (pkg / "sent-r1.json").exists()


def test_missing_password_env(env, monkeypatch):
    st, cfgp, pkg, cfg = env
    monkeypatch.delenv("MTCAX_SMTP_PASSWORD")
    with pytest.raises(mailer.MailError, match="MTCAX_SMTP_PASSWORD"):
        mailer.send(st, WEEK, cfgp)
    assert FakeSMTP.instances == []
    cfgp.write_text(yaml.safe_dump(dict(cfg, password_env="MY_PW")), encoding="utf-8")
    monkeypatch.setenv("MY_PW", "x")
    mailer.send(st, WEEK, cfgp)
    assert FakeSMTP.instances[0].logged_in[1] == "x"


def test_config_validation(env):
    st, cfgp, pkg, cfg = env
    for bad in (dict(cfg, security="plain"), {k: v for k, v in cfg.items() if k != "to_addrs"}, dict(cfg, to_addrs=["nope"])):
        cfgp.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(mailer.MailError):
            mailer.load_config(cfgp)
    with pytest.raises(mailer.MailError, match="없습니다"):
        mailer.load_config(cfgp.parent / "none.yaml")


def test_resend_guard(env):
    st, cfgp, pkg, cfg = env
    mailer.send(st, WEEK, cfgp)
    with pytest.raises(mailer.MailError, match="--resend"):
        mailer.send(st, WEEK, cfgp)
    assert len(FakeSMTP.instances) == 1
    mailer.send(st, WEEK, cfgp, resend=True)
    assert len(FakeSMTP.instances) == 2


def test_dry_run_sends_nothing(env, capsys, monkeypatch):
    st, cfgp, pkg, cfg = env
    monkeypatch.delenv("MTCAX_SMTP_PASSWORD")      # dry-run은 비밀번호가 필요 없다
    assert mailer.send(st, WEEK, cfgp, dry_run=True) is None
    out = capsys.readouterr().out
    assert FakeSMTP.instances == [] and not (pkg / "sent-r1.json").exists()
    assert "[dry-run] 제목: [MTC-AX] 2026-W49 사외 수집 r1 · complete 6건" in out and "수신자 2명" in out and "합계" in out
    assert out.count("첨부 ") == 4 and "company.example" not in out


def test_send_failure_hides_server_text_and_writes_no_sent_file(env):
    st, cfgp, pkg, cfg = env
    FakeSMTP.fail = True
    with pytest.raises(mailer.MailError) as e:
        mailer.send(st, WEEK, cfgp)
    assert SECRET not in str(e.value) and not (pkg / "sent-r1.json").exists()


def test_cli_send_and_package_send(env, monkeypatch, capsys):
    st, cfgp, pkg, cfg = env
    monkeypatch.setattr(cli, "load_settings", lambda root=None: st)
    assert cli.main(["send", "--issue-week", WEEK, "--mail-config", str(cfgp), "--dry-run"]) == 0
    assert cli.main(["send", "--issue-week", WEEK, "--mail-config", str(cfgp)]) == 0
    assert cli.main(["send", "--issue-week", WEEK, "--mail-config", str(cfgp)]) == 1       # 중복 발송 거부
    assert "이미 보냈습니다" in capsys.readouterr().out
    # package --send: 패키징은 성공하고 발송 실패 시 종료 코드만 1
    import collector.pipeline as pl
    monkeypatch.setattr(pl, "package", lambda s, w: ([pkg / "a"], []))
    FakeSMTP.fail = True
    (pkg / "sent-r1.json").unlink()
    assert cli.main(["package", "--issue-week", WEEK, "--send", "--mail-config", str(cfgp)]) == 1
    out = capsys.readouterr().out
    assert "발송 실패" in out and SECRET not in out
    FakeSMTP.fail = False
    assert cli.main(["package", "--issue-week", WEEK, "--send", "--mail-config", str(cfgp)]) == 0
