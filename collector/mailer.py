"""사내 메일 발송(설계서 4.7). 표준 라이브러리만 쓴다. 비밀번호는 환경변수로만 읽고 출력·기록하지 않는다."""
import hashlib
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

import yaml

from .fetch import now_kst
from .pipeline import load_json, next_revision
from .ref import weekly_window
from .settings import file_name

REQUIRED = ["smtp_host", "smtp_port", "security", "username", "from_addr", "to_addrs"]
DEFAULT_PASSWORD_ENV = "MTCAX_SMTP_PASSWORD"
MIME = {".gz": ("application", "gzip"), ".json": ("application", "json"), ".md": ("text", "markdown")}


class MailError(Exception):
    """발송 설정·크기·중복 등으로 보낼 수 없음. 메시지는 사용자에게 그대로 보여 준다."""


def load_config(path):
    p = Path(path)
    if not p.exists():
        raise MailError(f"메일 설정 파일이 없습니다: {p} — mail.local.example.yaml을 복사해 채우세요")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED if cfg.get(k) in (None, "", [])]
    if missing:
        raise MailError(f"메일 설정에 필요한 항목이 없습니다: {', '.join(missing)}")
    if cfg["security"] not in ("starttls", "ssl"):
        raise MailError("security는 starttls 또는 ssl이어야 합니다")
    if not isinstance(cfg["to_addrs"], list) or not all(isinstance(x, str) and "@" in x for x in cfg["to_addrs"]):
        raise MailError("to_addrs는 이메일 주소 목록이어야 합니다")
    try:
        cfg["smtp_port"] = int(cfg["smtp_port"])
        cfg["max_total_mb"] = float(cfg.get("max_total_mb", 18))
    except (TypeError, ValueError):
        raise MailError("smtp_port와 max_total_mb는 숫자여야 합니다")
    cfg["password_env"] = cfg.get("password_env") or DEFAULT_PASSWORD_ENV
    return cfg


def get_password(cfg, environ=None):
    import os
    pw = (environ if environ is not None else os.environ).get(cfg["password_env"])
    if not pw:
        raise MailError(f"비밀번호가 없습니다. 환경변수 {cfg['password_env']}에 SMTP 비밀번호(Gmail은 앱 비밀번호)를 넣고 다시 실행하세요")
    return pw


def package_paths(settings, issue_week):
    """최신 revision의 package/ 파일 네 개: (rev, [gz, 원본, 리포트, manifest], package 폴더)"""
    out = settings.out_dir(issue_week)
    rev = next_revision(out, issue_week) - 1
    pkg = out / "package"
    if rev < 1 or not pkg.exists():
        raise MailError("보낼 패키지가 없습니다. 먼저 run과 package를 실행하세요")
    base = file_name(issue_week, "intake", rev)
    paths = [pkg / (base + ".gz"), pkg / base, pkg / file_name(issue_week, "report", rev, "md"),
             pkg / file_name(issue_week, "manifest", rev)]
    miss = [p.name for p in paths if not p.exists()]
    if miss:
        raise MailError(f"package/에 파일이 없습니다: {', '.join(miss)} — package를 다시 실행하세요")
    return rev, paths, pkg


def subject_for(issue_week, rev, manifest):
    return f"[MTC-AX] {issue_week} 사외 수집 r{rev} · complete {manifest['summary']['complete']}건"


def build_body(issue_week, rev, manifest, paths, dropped):
    s, e = weekly_window(issue_week, manifest["window_weeks"])
    sm = manifest["summary"]
    lines = [f"{issue_week} 사외 수집 결과 r{rev}",
             f"기간: {s.date()} ~ {(e.date()).isoformat()} 00:00 (KST, 종료 시각 미포함) · {manifest['window_weeks']}주",
             f"summary: records {sm['records']} / complete {sm['complete']} / partial {sm['partial']} / drop {sm['drop']} / paywall {sm['paywall']}",
             "경고:"]
    lines += [f"  - {w}" for w in manifest.get("warnings", [])] or ["  (없음)"]
    lines.append("첨부 파일(바이트 수, SHA-256):")
    for p in paths:
        b = p.read_bytes()
        lines.append(f"  - {p.name}  {len(b):,} B  {hashlib.sha256(b).hexdigest()}")
    if dropped:
        lines.append(f"※ 메일 크기 한도 때문에 원본 intake({dropped.name})는 첨부하지 않았습니다. 압축 intake로 처리하세요.")
    lines += ["사내 처리: 첨부 파일을 한 폴더에 저장한 뒤", f"  python -m internal run --package <그 폴더> --issue-week {issue_week}"]
    return "\n".join(lines) + "\n"


def build_message(cfg, issue_week, rev, paths, manifest, dropped=None):
    keep = [p for p in paths if p != dropped]
    msg = EmailMessage()
    msg["Subject"] = subject_for(issue_week, rev, manifest)
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(cfg["to_addrs"])
    msg["Message-ID"] = make_msgid(domain="mtc-ax.local")
    msg.set_content(build_body(issue_week, rev, manifest, keep, dropped))
    for p in keep:
        main, sub = MIME[p.suffix]
        msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub, filename=p.name)
    return msg, keep


def plan_attachments(cfg, paths):
    """합계가 max_total_mb를 넘으면 원본 intake만 뺀다. 그래도 넘으면 MailError. 반환: (뺀 파일 또는 None, 합계 바이트)"""
    limit = cfg["max_total_mb"] * 1024 * 1024
    total = sum(p.stat().st_size for p in paths)
    if total <= limit:
        return None, total
    dropped = paths[1]
    total -= dropped.stat().st_size
    if total > limit:
        raise MailError(f"첨부 합계 {total / 1024 / 1024:.1f} MB가 한도 {cfg['max_total_mb']:g} MB를 넘어 보내지 않습니다"
                        "(원본 intake를 빼도 초과). max_total_mb를 올리거나 후보 수를 줄이세요")
    return dropped, total


def _connect(cfg):
    if cfg["security"] == "ssl":
        return smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ssl.create_default_context(), timeout=60)
    smtp = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=60)
    smtp.starttls(context=ssl.create_default_context())
    return smtp


def send(settings, issue_week, config_path=None, dry_run=False, resend=False, out=print, environ=None):
    """package/의 네 파일을 메일로 보낸다. 반환: 발송 기록(dict, dry-run이면 None)."""
    cfg = load_config(config_path or Path(settings.root) / "mail.local.yaml")
    rev, paths, pkg = package_paths(settings, issue_week)
    sent_file = pkg / f"sent-r{rev}.json"
    if sent_file.exists() and not resend and not dry_run:
        raise MailError(f"{issue_week} r{rev}은(는) 이미 보냈습니다({sent_file.name}). 다시 보내려면 --resend를 붙이세요")
    manifest = load_json(paths[3])
    dropped, total = plan_attachments(cfg, paths)
    msg, keep = build_message(cfg, issue_week, rev, paths, manifest, dropped)
    if dry_run:
        out(f"[dry-run] 제목: {msg['Subject']}")
        for p in keep:
            out(f"  첨부 {p.name}  {p.stat().st_size:,} B")
        if dropped:
            out(f"  (한도 초과로 원본 intake 제외: {dropped.name})")
        out(f"  합계 {total:,} B (한도 {cfg['max_total_mb']:g} MB) · 수신자 {len(cfg['to_addrs'])}명 · 보내지 않았습니다")
        return None
    password = get_password(cfg, environ)
    try:
        with _connect(cfg) as smtp:
            smtp.login(cfg["username"], password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"메일 발송 실패: {type(e).__name__}")      # 서버 응답에 계정 정보가 섞일 수 있어 종류만 보인다
    record = {"sent_at": now_kst(), "recipients": len(cfg["to_addrs"]), "message_id": msg["Message-ID"],
              "attachments": [p.name for p in keep]}
    sent_file.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    out(f"발송 완료: {msg['Subject']} (수신자 {record['recipients']}명, 첨부 {len(keep)}개)")
    return record
