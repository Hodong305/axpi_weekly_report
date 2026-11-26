"""공식 발표 이미지(설계서 4.4·4.7·9.8): 수집(후보 추출·필터·축소), 패키지(zip·manifest·K12), 메일 첨부 순서, 사내 이미지 선택·[I]·.eml."""
import copy
import email
import email.policy
import gzip
import hashlib
import io
import json
import re
import threading
import zipfile
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from PIL import Image

from collector import mailer, media as media_mod
from collector.pipeline import package, run
from collector.settings import ROOT, file_name, load_settings
from collector.validate import check, dir_lookup, zip_lookup
from internal import checks, draft as draftmod, intake, render, review as rv, settings as settings_mod
from internal.llm import Judge
from internal.pipeline import Pipeline

import test_collector as tc
from test_internal import CFG, FakeClient, FakeIO, WEEK, fill, make_package, make_record, sample_records

HITS = []


class CountingHandler(tc.Handler):
    def do_GET(self):
        HITS.append(self.path.split("?")[0])
        return super().do_GET()


def png(w, h, color=(30, 90, 200), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, fmt)
    return buf.getvalue()


def page(base, extra=""):
    return tc.article("Official fleet analytics news", "2026-11-24T09:00:00-08:00", tc.paras(tc.LONG_EN) + extra, canonical=None,
                      extra_head=f'<meta property="og:image" content="/img/hero.png"/>')


FIGS = ('<figure><img src="/img/diagram.png" alt="diagram alt"><figcaption>System diagram of the analytics pipeline</figcaption></figure>'
        '<img src="/img/logo.png" alt="company logo"><img src="/img/pixel.png" alt="px" width="1" height="1">'
        '<img src="/img/tiny.png" alt="tiny icon image"><img src="/img/photo.jpg" alt="Engineers at the fab">'
        '<img src="/img/no-alt.png"><img src="/img/vector.svg" alt="vector">')


@pytest.fixture()
def msite(tmp_path):
    HITS.clear()
    www = tmp_path / "www"
    (www / "news").mkdir(parents=True)
    (www / "press").mkdir()
    (www / "img").mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(CountingHandler, directory=str(www)))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    (www / "img" / "hero.png").write_bytes(png(800, 450))
    (www / "img" / "diagram.png").write_bytes(png(640, 400, (250, 250, 250)))
    (www / "img" / "logo.png").write_bytes(png(600, 600))
    (www / "img" / "pixel.png").write_bytes(png(1, 1))
    (www / "img" / "tiny.png").write_bytes(png(120, 120))
    (www / "img" / "photo.jpg").write_bytes(png(2400, 1600, (10, 120, 60), "JPEG"))
    (www / "img" / "no-alt.png").write_bytes(png(700, 500))
    (www / "img" / "vector.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (www / "news" / "official").write_text(page(base, FIGS), encoding="utf-8")
    (www / "press" / "story").write_text(page(base, FIGS), encoding="utf-8")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield base, tmp_path
    server.shutdown()


def media_settings(tmp, base):
    st = tc.make_settings(tmp, base)
    st.sources.append({"id": "press-src", "publisher": "Example Press", "domains": ["127.0.0.1"], "url_prefixes": [f"{base}/press/"],
                       "topics": [], "track": "A", "language": "en", "source_type": "press", "source_tier": "named_secondary", "method": "llm_only"})
    d = st.candidates_dir(tc.WEEK)
    d.mkdir(parents=True, exist_ok=True)
    (d / "claude-T2.json").write_text(json.dumps({"candidates": [{"url": f"{base}/news/official", "topic_hints": ["T2"]},
                                                                {"url": f"{base}/press/story", "topic_hints": ["T2"]}]}), encoding="utf-8")
    return st


def run_media(tmp, base, **kw):
    st = media_settings(tmp, base)
    docs, errors = run(st, tc.WEEK, force=True, log=lambda *a: None, skip_registry=True)
    return st, docs, errors


def test_media_extracted_filtered_and_resized(msite):
    base, tmp = msite
    st, docs, errors = run_media(tmp, base)
    assert not any(v[0] for v in errors.values()), errors
    r = tc.recs(docs["merged"])
    off, press = r["news/official"], r["press/story"]
    assert off["status"] == press["status"] == "complete" and off["source_type"] == "company_official" and press["source_type"] == "press"
    assert press["media"] == []                                                  # 공식 발표가 아니면 이미지 후보를 만들지 않는다
    m = off["media"]
    assert [x["id"] for x in m] == ["M1", "M2", "M3"]                            # og, 도식, 사진만(로고·픽셀·작은 것·alt 없는 것·SVG 제외)
    assert [x["kind"] for x in m] == ["og_image", "figure", "figure"]
    assert m[1]["caption"] == "System diagram of the analytics pipeline" and m[1]["alt"] == "diagram alt"
    assert m[2]["caption"] is None and m[2]["alt"] == "Engineers at the fab"
    assert m[0]["url"].endswith("/img/hero.png") and m[0]["caption"] is None
    out = st.out_dir(tc.WEEK)
    for x in m:
        assert x["file"] == f"media/{off['doc_id'][:12]}-{x['id']}.{'png' if x['mime'] == 'image/png' else 'jpg'}"
        data = (out / x["file"]).read_bytes()
        assert len(data) == x["bytes"] and hashlib.sha256(data).hexdigest() == x["sha256"]
        assert Image.open(io.BytesIO(data)).size == (x["width"], x["height"])
    assert m[0]["mime"] == "image/png" and m[1]["mime"] == "image/png" and m[2]["mime"] == "image/jpeg"       # PNG 도식은 PNG 유지, 사진은 JPEG
    assert (m[2]["width"], m[2]["height"]) == (1200, 800)                         # 긴 변 1200px로 줄임
    assert (m[0]["width"], m[0]["height"]) == (800, 450)
    assert "/img/vector.svg" not in HITS and "/img/no-alt.png" not in HITS and "/img/logo.png" not in HITS
    assert "/img/tiny.png" in HITS and any("이미지 제외: 너무 작음" in a["observation"] for a in off["attempts"])        # 태그에 크기가 없어 받았지만 작아서 제외
    errs, _ = check(docs["intake"], 200, 10, dir_lookup(out))
    assert errs == []


def test_rerun_does_not_download_media_again(msite):
    base, tmp = msite
    st, docs, _ = run_media(tmp, base)
    n_img = sum(1 for h in HITS if h.startswith("/img/"))
    assert n_img > 0
    docs2, errors2 = run(st, tc.WEEK, force=True, log=lambda *a: None, skip_registry=True)
    assert not any(v[0] for v in errors2.values()), errors2
    assert docs2["merged"]["revision"] == 2
    assert sum(1 for h in HITS if h.startswith("/img/")) == n_img                    # 같은 본문이라 이미지를 다시 받지 않는다
    off = tc.recs(docs2["merged"])["news/official"]
    assert [x["id"] for x in off["media"]] == ["M1", "M2", "M3"]
    assert off["media"] == tc.recs(docs["merged"])["news/official"]["media"]


def test_package_zip_manifest_and_k12(msite):
    base, tmp = msite
    st, docs, _ = run_media(tmp, base)
    files, warns = package(st, tc.WEEK)
    names = [f.name for f in files]
    zname = file_name(tc.WEEK, "media", 1, "zip")
    assert names == [file_name(tc.WEEK, "intake", 1) + ".gz", file_name(tc.WEEK, "intake", 1), file_name(tc.WEEK, "report", 1, "md"),
                     file_name(tc.WEEK, "manifest", 1), zname]
    pkg = st.out_dir(tc.WEEK) / "package"
    manifest = json.loads((pkg / file_name(tc.WEEK, "manifest", 1)).read_text(encoding="utf-8"))
    listed = {f["name"]: f for f in manifest["files"]}
    assert manifest["schema_version"] == "4.6" and set(listed) == set(names) - {file_name(tc.WEEK, "manifest", 1)}
    zb = (pkg / zname).read_bytes()
    assert listed[zname]["bytes"] == len(zb) and listed[zname]["sha256"] == hashlib.sha256(zb).hexdigest()
    with zipfile.ZipFile(pkg / zname) as z:
        assert sorted(z.namelist()) == sorted(m["file"] for r in docs["intake"]["records"] for m in r["media"])
    intake_doc = json.loads(gzip.open(files[0], "rt", encoding="utf-8").read())
    assert check(intake_doc, 200, 10, zip_lookup(pkg / zname))[0] == []
    # K12: 파일 없음·해시 불일치·ID 순번
    other = copy.deepcopy(intake_doc)
    next(r for r in other["records"] if r["media"])["media"][0]["sha256"] = "0" * 64
    assert any("K12" in e and "해시" in e for e in check(other, 200, 10, zip_lookup(pkg / zname))[0])
    other = copy.deepcopy(intake_doc)
    next(r for r in other["records"] if r["media"])["media"][1]["file"] = next(r for r in other["records"] if r["media"])["media"][1]["file"].replace("-M2", "-M9")
    errs = check(other, 200, 10, zip_lookup(pkg / zname))[0]
    assert any("K12" in e and "없음" in e for e in errs)
    other = copy.deepcopy(intake_doc)
    next(r for r in other["records"] if r["media"])["media"][1]["id"] = "M3"
    assert any("K12" in e and "순번" in e for e in check(other, 200, 10)[0])
    other = copy.deepcopy(intake_doc)
    del next(r for r in other["records"] if r["media"])["media"]
    assert any("K1" in e for e in check(other, 200, 10)[0])                          # 스키마 4.6은 media 필수
    other = copy.deepcopy(intake_doc)
    other["schema_version"] = "4.5"
    assert any("K1" in e for e in check(other, 200, 10)[0])
    # 이미지가 없으면 묶음을 만들지 않고 이전 묶음도 지운다
    for r in docs["intake"]["records"]:
        r["media"] = []
    (st.out_dir(tc.WEEK) / file_name(tc.WEEK, "intake", 1)).write_text(json.dumps(docs["intake"], ensure_ascii=False), encoding="utf-8")
    files2, _ = package(st, tc.WEEK)
    assert not (pkg / zname).exists() and zname not in [f.name for f in files2]


# 메일 ----------------------------------------------------------------------------

def mail_cfg(mb):
    return {"max_total_mb": mb}


def test_mail_attaches_zip_and_drops_original_then_zip(tmp_path):
    def mk(name, n):
        p = tmp_path / name
        p.write_bytes(b"x" * n)
        return p
    mb = 1024 * 1024
    paths = [mk("a.json.gz", mb // 10), mk("intake.json", mb), mk("r.md", 100), mk("m.json", 100), mk("media.zip", mb)]
    assert mailer.plan_attachments(mail_cfg(5), paths) == ([], sum(p.stat().st_size for p in paths))
    dropped, total = mailer.plan_attachments(mail_cfg(1.5), paths)          # 원본 JSON을 먼저 뺀다
    assert [d.name for d in dropped] == ["intake.json"]
    dropped, total = mailer.plan_attachments(mail_cfg(0.5), paths)          # 그래도 넘으면 이미지 묶음도 뺀다
    assert [d.name for d in dropped] == ["intake.json", "media.zip"] and total == mb // 10 + 200
    with pytest.raises(mailer.MailError, match="보내지 않습니다"):
        mailer.plan_attachments(mail_cfg(0.05), paths)
    # 이미지 묶음이 없으면 기존 동작(원본만 뺀다)
    assert [d.name for d in mailer.plan_attachments(mail_cfg(0.5), paths[:4])[0]] == ["intake.json"]


def test_mail_message_has_zip_attachment_and_notes_drop(msite):
    base, tmp = msite
    st, docs, _ = run_media(tmp, base)
    package(st, tc.WEEK)
    cfgp = tmp / "mail.local.yaml"
    cfgp.write_text(yaml.safe_dump({"smtp_host": "smtp.example.com", "smtp_port": 587, "security": "starttls", "username": "u@example.com",
                                    "from_addr": "u@example.com", "to_addrs": ["to@company.example"], "max_total_mb": 18}), encoding="utf-8")
    cfg = mailer.load_config(cfgp)
    rev, paths, pkg = mailer.package_paths(st, tc.WEEK)
    assert [p.suffix for p in paths] == [".gz", ".json", ".md", ".json", ".zip"]
    manifest = json.loads(paths[3].read_text(encoding="utf-8"))
    msg, keep = mailer.build_message(cfg, tc.WEEK, rev, paths, manifest)
    atts = list(msg.iter_attachments())
    assert len(atts) == 5 and atts[4].get_content_type() == "application/zip" and atts[4].get_filename() == file_name(tc.WEEK, "media", 1, "zip")
    msg, keep = mailer.build_message(cfg, tc.WEEK, rev, paths, manifest, [paths[1], paths[4]])
    body = msg.get_body().get_content()
    assert len(list(msg.iter_attachments())) == 3 and "이미지 묶음" in body and "사내 발송본은 이미지 없이" in body and "원본 intake" in body


# 마이그레이션 ----------------------------------------------------------------------

def test_migration_emits_empty_media():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import migrate_v42 as mg
    from test_migrate_v42 import old_rec
    old = {"schema_version": "4.2", "phase": "intake", "records": [old_rec("https://a.example.org/x", "문서", "2026-11-18")]}
    merged, intake_doc, rows, aborted, extra = mg.migrate(old, "2026-W48", load_settings(), now="2026-11-26T10:00:00+09:00")
    assert merged["schema_version"] == "4.6" and all(r["media"] == [] for r in merged["records"])
    assert check(merged, 40, 10)[0] == [] and check(intake_doc, 40, 10)[0] == []


# 사내: 이미지 선택·[I]·.eml -------------------------------------------------------------

def media_records():
    """Lam 사건(공식 자료 2건 중 한쪽에 이미지 2장, 언론 자료에도 이미지 1장)이 있는 입력과 이미지 묶음 파일."""
    recs = sample_records()
    files = {}
    for r in recs:
        if r["publisher"] == "Lam Research":
            ref = r
            r["media"] = []
            for n, (kind, cap, alt, w, h) in enumerate([("og_image", None, None, 1200, 630), ("figure", "Fleet analytics architecture diagram", "architecture", 900, 500)], 1):
                name = f"media/{r['doc_id'][:12]}-M{n}.jpg"
                data = png(w, h, (20 * n, 90, 160), "JPEG")
                files[name] = data
                r["media"].append({"id": f"M{n}", "kind": kind, "url": f"https://lam.example.org/img/{n}.jpg", "caption": cap, "alt": alt, "file": name,
                                   "mime": "image/jpeg", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "width": w, "height": h})
        elif r["source_type"] == "press" and "Lam" in r["title"]:
            name = f"media/{r['doc_id'][:12]}-M1.jpg"
            data = png(800, 400, (200, 10, 10), "JPEG")
            files[name] = data
            r["media"] = [{"id": "M1", "kind": "og_image", "url": "https://news.example.com/x.jpg", "caption": None, "alt": None, "file": name,
                           "mime": "image/jpeg", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "width": 800, "height": 400}]
    return recs, files


def run_internal(tmp, inputs, client=None, with_zip=True, cfg_edit=None):
    recs, files = media_records()
    st = settings_mod.load(data_root=tmp)
    if cfg_edit:
        cfg_edit(st.config)
    pkg = make_package(tmp, recs, media_files=files if with_zip else None)
    io_ = FakeIO(inputs)
    client = client or FakeClient()
    code = Pipeline(st, Judge(client), io_).start(pkg)
    return code, client, io_, recs, files


def lam_doc(recs):
    return next(r for r in recs if r["publisher"] == "Lam Research")


def test_s1_loads_media_zip_and_verifies(tmp_path):
    recs, files = media_records()
    pkg = make_package(tmp_path, recs, media_files=files)
    p = intake.load_package(pkg, 40, 10)
    assert p.media_zip is not None and p.media_note == ""
    name = next(iter(files))
    assert p.image_bytes(name) == files[name] and p.image_bytes("media/none.jpg") is None
    # 이미지 묶음을 건드리면 manifest 해시 불일치
    z = next(pkg.glob("*-media-*.zip"))
    z.write_bytes(z.read_bytes() + b"x")
    with pytest.raises(intake.IntakeError, match="이미지 묶음"):
        intake.load_package(pkg, 40, 10)
    # 묶음이 없으면 이미지 없이 진행한다
    pkg2 = make_package(tmp_path / "b", recs)
    p2 = intake.load_package(pkg2, 40, 10)
    assert p2.media_zip is None and "이미지 없이" in p2.media_note


def test_core_image_selected_validated_and_rendered(tmp_path):
    code, client, io_, recs, files = run_internal(tmp_path, ["1", "승인", "1"])
    assert code == 0
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    core = [a for a in d["articles"] if a["slot"] == "core"]
    lam = lam_doc(recs)
    imgs = [a["image"] for a in core if a.get("image")]
    assert imgs == [{"media_ref": f"{lam['doc_id']}#M1", "caption": "기사의 핵심 장면을 보여 주는 도식"}]
    assert all("image" not in a for a in d["articles"] if a["slot"] == "brief") and all("image" in a for a in core)
    # 후보 목록은 캡션·alt·크기만 담고 이미지 내용은 없다. 언론 자료의 이미지는 후보가 아니다
    user = next(u for n, u in client.calls if n == "s6_article" and "[이미지 후보]" in u)
    cands = json.loads(re.search(r"\[이미지 후보\]\n(\[.*\])", user).group(1))
    assert [c["media_ref"] for c in cands] == [f"{lam['doc_id']}#M1", f"{lam['doc_id']}#M2"] and cands[1]["size"] == "900x500"
    assert set(cands[0]) == {"media_ref", "kind", "caption", "alt", "size"} and "base64" not in user
    # 산출물: eml(cid) + 미리 보기 html(data URI) + txt
    out = tmp_path / "out_internal" / WEEK
    eml = email.message_from_bytes((out / "newsletter.eml").read_bytes(), policy=email.policy.default)
    assert eml["Subject"].startswith("FAB4 위클리 ") and eml["To"] is None and eml["From"] is None and eml["X-Unsent"] == "1"
    assert eml.get_content_type() == "multipart/alternative"
    parts = list(eml.iter_parts())
    assert parts[0].get_content_type() == "text/plain" and parts[1].get_content_type() == "multipart/related"
    html_part = next(parts[1].iter_parts())
    html_doc = html_part.get_content()
    cids = re.findall(r'<img src="cid:([^"]+)"', html_doc)
    related = [p for p in parts[1].iter_parts() if p.get_content_maintype() == "image"]
    assert len(cids) == len(related) == 1 and related[0]["Content-ID"] == f"<{cids[0]}>"
    assert len(related[0].get_content()) <= 300 * 1024
    assert "http" not in html_doc.lower() and "<a " not in html_doc.lower() and "<script" not in html_doc.lower()
    assert 'width="624"' in html_doc and "max-width:100%" in html_doc and "이미지 · Lam Research" in html_doc
    assert html_doc.index("<img") > html_doc.index("핵심") and "기사의 핵심 장면을 보여 주는 도식" in html_doc
    preview = (out / "newsletter.html").read_text(encoding="utf-8")
    assert re.search(r'<img src="data:image/jpeg;base64,[A-Za-z0-9+/=]+"', preview) and "cid:" not in preview
    assert "<a " not in preview.lower() and "http" not in re.sub(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+", "", preview).lower()
    text = (out / "newsletter.txt").read_text(encoding="utf-8")
    assert "[이미지] 기사의 핵심 장면을 보여 주는 도식 · 이미지 · Lam Research" in text
    assert "[이미지]" in parts[0].get_content()
    assert "[I 대표 이미지]   I1 핵심1 · M1 기사의 핵심 장면을 보여 주는 도식 (후보 M1~M2)" in io_.text


def test_no_images_without_zip_or_for_non_policy_sources(tmp_path):
    code, client, io_, recs, files = run_internal(tmp_path, ["1", "승인", "1"], with_zip=False)
    assert code == 0 and any("이미지 묶음이 없어" in l for l in io_.out)
    assert not any("[이미지 후보]" in u for n, u in client.calls)
    out = tmp_path / "out_internal" / WEEK
    eml = email.message_from_bytes((out / "newsletter.eml").read_bytes(), policy=email.policy.default)
    assert not [p for p in eml.walk() if p.get_content_maintype() == "image"] and "<img" not in (out / "newsletter.html").read_text(encoding="utf-8")
    assert "[I 대표 이미지]   (없음)" in io_.text


def test_image_not_in_candidates_is_rejected_and_dropped(tmp_path):
    recs, _ = media_records()
    press = next(r for r in recs if r["source_type"] == "press" and r["media"])
    bad = {"media_ref": f"{press['doc_id']}#M1", "caption": "언론 자료의 이미지"}       # 정책 대상이 아닌 자료의 이미지
    base = FakeClient()

    class C(FakeClient):
        def r_s6_article(self, user):
            out = base.r_s6_article(user)
            if '"slot": "core"' in user and "[이미지 후보]" in user:
                out["image"] = bad
            return out
    code, client, io_, _, _ = run_internal(tmp_path, ["1", "승인", "1"], client=C())
    assert code == 0
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    assert all(a.get("image") is None for a in d["articles"] if a["slot"] == "core")
    assert "<img" not in (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")


def test_caption_checks_length_url_and_verbatim():
    recs, _ = media_records()
    recs = {r["doc_id"]: r for r in recs}
    lam = next(r for r in recs.values() if r["publisher"] == "Lam Research")
    from test_internal import lam_event
    ev, _, _ = lam_event()
    ev["doc_ids"] = [lam["doc_id"]]
    ev.update(cap="core", slot="core")
    orig = lam["media"][1]["caption"]
    art = {"event_id": ev["event_id"], "slot": "core", "title": "t", "title_alternatives": [], "body": fill("본문", 280), "watch": "w", "key_figures": [],
           "evidence_ids": [f"{lam['doc_id']}/P1"], "credits": [],
           "image": {"media_ref": f"{lam['doc_id']}#M2", "caption": "가" * 41}}
    draft = {"issue_week": WEEK, "issue_title": "제목", "summary": fill("총론", 130), "punch": None, "punch_candidates": [], "glossary": [],
             "issue_title_candidates": ["제목"], "summary_candidates": [fill("총론", 130)], "articles": [art], "more": [], "excluded_count": 0, "review_log": []}
    by_id = {ev["event_id"]: ev}
    assert any("이미지 설명 41자" in m for _, m in checks.length_violations(draft, CFG, "정규호"))
    art["image"]["caption"] = "자세히는 https://x.example.org/a 참조"
    checks.autofix(draft, recs)
    assert "http" not in art["image"]["caption"]
    art["image"]["caption"] = orig + " 입니다 그리고 더"                               # 원문 캡션을 그대로 옮김(30자 이상 연속 일치)
    kinds = [k for _, k, _ in checks.hard_violations(draft, by_id, recs, CFG, "정규호")]
    assert "원문 연속 일치" in kinds
    art["image"]["caption"] = "파이프라인 구성을 한눈에 보여 주는 도식"
    assert not [v for v in checks.hard_violations(draft, by_id, recs, CFG, "정규호") if v[1] in ("이미지", "원문 연속 일치")]
    art["image"]["media_ref"] = "url1_" + "0" * 64 + "#M1"
    assert any(k == "이미지" for _, k, _ in checks.hard_violations(draft, by_id, recs, CFG, "정규호"))


def test_review2_I_grammar_render_and_apply():
    assert rv.parse_review2("승인 I1=빼기")["I"] == {1: ("none",)}
    assert rv.parse_review2("승인 I2=M3, I1=빼기 F1=빼기")["I"] == {2: ("swap", 3), 1: ("none",)}
    assert rv.parse_review2("승인")["I"] == {}
    for bad in ("승인 I1=3", "승인 I=빼기", "승인 I1=M"):
        with pytest.raises(rv.ReviewError):
            rv.parse_review2(bad)
    from test_internal import sample_draft
    d = sample_draft()
    cands = lambda n: [{"label": f"M{k}", "media_ref": f"url1_x#M{k}", "caption": f"원문 캡션 {k}", "alt": None, "kind": "figure", "width": 800, "height": 400} for k in range(1, n + 1)]
    d["articles"][0]["image"] = {"media_ref": "url1_x#M2", "caption": "새로 쓴 설명"}
    d["articles"][1]["image"] = None
    img = {0: cands(3), 1: cands(2), 2: []}
    lines = rv.render_review2(WEEK, d, [], CFG, img).splitlines()
    assert "[I 대표 이미지]   I1 핵심1 · M2 새로 쓴 설명 (후보 M1~M3) / I2 핵심2 · 없음 (후보 M1~M2)" in lines
    assert lines.index("[F 더 볼 거리]   (분야별 한 줄 요약 · 빼려면 F3=빼기)") < lines.index("[I 대표 이미지]   I1 핵심1 · M2 새로 쓴 설명 (후보 M1~M3) / I2 핵심2 · 없음 (후보 M1~M2)") < lines.index("[G 새 용어 풀이]")
    assert " - I: 바꿀 이미지만. 예: I1=빼기, I2=M3. 생략하면 모델 선택 유지" in lines
    for bad, msg in (("승인 I3=빼기", "I3"), ("승인 I1=M4", "M1~M3"), ("승인 I2=M3", "M1~M2")):
        with pytest.raises(rv.ReviewError, match=msg):
            rv.validate_review2(rv.parse_review2(bad), d, [], CFG, img)
    rv.validate_review2(rv.parse_review2("승인 I1=M3 I2=M1"), d, [], CFG, img)
    st = rv.apply_review2(d, rv.parse_review2("승인 I1=M3 I2=M1"), [], img)
    assert d["articles"][0]["image"]["media_ref"] == "url1_x#M3" and d["articles"][1]["image"]["media_ref"] == "url1_x#M1"
    assert [(i, c["label"]) for i, c in st["image_swaps"]] == [(0, "M3"), (1, "M1")] and st["i_changed"] == 2
    st = rv.apply_review2(d, rv.parse_review2("승인 I1=빼기"), [], img)
    assert d["articles"][0]["image"] is None and st["i_changed"] == 1
    for a in d["articles"]:
        a["image"] = None
    assert "[I 대표 이미지]   (없음)" in rv.render_review2(WEEK, d, [], CFG, None)


def test_pipeline_review_swaps_and_removes_images(tmp_path):
    code, client, io_, recs, files = run_internal(tmp_path, ["1", "승인 I1=M2", "1"])
    assert code == 0
    lam = lam_doc(recs)
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    img = next(a["image"] for a in d["articles"] if a.get("image"))
    assert img == {"media_ref": f"{lam['doc_id']}#M2", "caption": "바꾼 이미지를 새로 설명한 문장"} and client.steps().count("s6_image_caption") == 1
    html_doc = (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")
    assert "바꾼 이미지를 새로 설명한 문장" in html_doc and html_doc.count("<img") == 1
    tmp2 = tmp_path / "second"
    code, client, io_, recs, files = run_internal(tmp2, ["1", "승인 I1=빼기", "1"])
    assert code == 0 and "<img" not in (tmp2 / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")


def test_prepare_image_respects_width_and_size():
    rng = __import__("random").Random(1)
    noisy = Image.new("RGB", (1200, 800))
    noisy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1200 * 800)])
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    data, mime, w, h = render.prepare_image(buf.getvalue(), 100)
    assert mime == "image/jpeg" and len(data) <= 100 * 1024 and w <= 624 and abs(w / h - 1.5) < 0.02
    small = png(300, 200)
    assert render.prepare_image(small, 300)[2:] == (300, 200)                      # 작은 이미지는 키우지 않는다


def test_verify_static_allows_only_embedded_images():
    render.verify_static('<img src="cid:a@b" width="624">')
    render.verify_static('<img src="data:image/jpeg;base64,aHR0cA==">' + "x")
    for bad in ('<img src="https://x/y.jpg">', '<img src="/local.jpg">', '<a href="x">', "http://x"):
        with pytest.raises(ValueError):
            render.verify_static(bad)
