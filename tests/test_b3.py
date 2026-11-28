"""B3 레이아웃(민트 블록 머리띠, 꼭지 번호 배지, 카드 맨 위 이미지) 시험."""
import copy
import email
import email.policy
import io
import re

import pytest
from PIL import Image

from internal import render, settings as settings_mod

from test_layout import full_draft, render_ctx


def png_bytes(w=800, h=450):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 200)).save(buf, "JPEG")
    return buf.getvalue()


def b3_ctx():
    ctx = render_ctx(theme="B3")
    ctx.update(newsletter_name="MTC AX Tech Brief", newsletter_tagline="메모리 제조기술센터 AX/PI팀이 매주 정리하는 팹 AI 브리프",
               newsletter_team="삼성전자 DS부문 메모리 제조기술센터(MTC) AX/PI팀", newsletter_from="")
    return ctx


def draft_b3(image=False):
    d = full_draft()
    d["articles"][0]["event_id"], d["articles"][1]["event_id"] = "e1", "e2"
    d["articles"][0]["image"] = {"media_ref": "url1_x#M1", "caption": "삼성 에이전트 구성도를 새로 설명한 문장"} if image else None
    d["articles"][1]["image"] = None
    d["more"].append({"ref": "r3", "topic": "B", "line": "기반기술 항목 요약 (논문)", "credit": "출처 3", "evidence_ids": []})
    d["articles"][2]["figure"] = {"value": "350개", "label": "공정 파라미터"}
    return d


def render_b3(image=False):
    ctx = b3_ctx()
    if image:
        ctx["image_loader"] = lambda ref: (png_bytes(), "삼성반도체")
    return render.build_all(draft_b3(image), ctx)


def strip_data(html_doc):
    return re.sub(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+", "data:", html_doc)


def test_b3_is_default_theme_and_config():
    cfg = settings_mod.load().internal
    assert cfg["newsletter_theme"] == "S6" and cfg["newsletter_name"] == "MTC AX Tech Brief"     # v4.7: 기본은 B4, B3는 그대로 고를 수 있다
    assert render.load_themes()["B3"]["template"] == "newsletter-b3.html"
    assert {"A", "B", "C", "B3", "B4", "B5", "B6", "S4"} <= set(render.load_themes())


def test_b3_layout_elements_without_image():
    out = render_b3(image=False)
    h = out["html_preview"]
    low = strip_data(h).lower()
    assert "http" not in low and "<a " not in low and "<script" not in low and "<img" not in low and "[대표 이미지 자리]" not in h and "e9eef0" not in h
    th = render.load_themes()["B3"]
    # 머리띠: 민트 블록 + 흰 글씨 + 이름 강조 + 꼭지 번호
    assert f'background:{th["hero"]};border-radius:18px 18px 0 0' in h and "linear-gradient" not in h
    assert 'MTC AX <span style="color:#0b2b3c;">Tech Brief</span>' in h and "메모리 제조기술센터 AX/PI팀이 매주 정리하는 팹 AI 브리프" in h
    assert "01 칩메이커 &nbsp;·&nbsp; 02 장비사 &nbsp;·&nbsp; 03 제조 솔루션 &nbsp;·&nbsp; 04 Physical AI" in h and "제3호 · 2026-W49" in h and "11.15 ~ 11.21" in h
    # 헤드라인 · 총론(왼쪽 강조선) · 어두운 한 방 상자
    assert "이번 주 헤드라인" in h and f"border-left:3px solid {th['accent']};padding-left:12px" in h
    assert f'background:{th["punch_bg"]};border-radius:14px' in h and "이번 주 가장 놀라운 숫자" in h and f"color:{th['punch_big']}" in h
    # 핵심 카드: 이미지가 없으면 회색 자리 없이 위쪽 여백 16px로 시작한다
    assert h.count("padding:16px 18px;") >= 2 and "padding:10px 18px 16px 18px" not in h and "border-radius:15px 15px 0 0" not in h
    # 꼭지 번호 배지: 핵심·단신·더 볼 거리 모두, 기반기술은 +
    for n, name in (("01", "칩메이커"), ("04", "Physical AI"), ("03", "제조 솔루션"), ("+", "기반기술")):
        assert re.search(rf">{re.escape(n)}</span>&nbsp;<span[^>]*>{name}</span>", h), (n, name)
    fg, bg = th["topic"]["T1"]
    assert f"background:{fg};color:#ffffff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:4px" in h
    assert f"background:{bg};border-radius:10px;padding:10px 4px;text-align:center" in h and f"color:{fg};\">99%" in h            # 수치 카드는 꼭지 색으로
    assert "MTC AX Insight" in h and "출처 · 발행처, 「원제」" in h and ">350개<" in h and "공정 파라미터" in h and "이번 호 용어 사전" in h
    assert "(논문)" in h and "(보도)" in h and "더 볼 거리" in h and ">단신<" in h and "이번 주 핵심" in h
    # 약어 풀이는 첫 등장에만, 제목에는 없다
    gl = '<span style="color:#7a8597;font-size:12.5px;">(웨이퍼를 자동으로 실어 나르는 팹 물류 시스템)</span>'
    assert h.count(gl) == 1 and "AMHS 제목에는 풀이가 붙지 않는다" in h
    # 꼬리말: 이름·팀, 이미지 문장은 이미지가 있을 때만
    assert "MTC AX Tech Brief는 삼성전자 DS부문 메모리 제조기술센터(MTC) AX/PI팀이 매주 화요일 보내는 제조 AI 동향 소식지입니다." in h
    assert "공개 자료 39건을 검토해 3건을 골랐고" in h and "이미지는 각 발행처의 공개 자료이며 저작권은 발행처에 있습니다." not in h
    assert "이미지는 각 발행처" not in out["text"] and out["images"] == []


def test_b3_image_first_in_card_with_credit_and_footer_sentence():
    out = render_b3(image=True)
    h, e = out["html_preview"], out["html_eml"]
    assert len(out["images"]) == 1
    for doc in (h, e):
        card = doc[doc.index("이번 주 핵심"):]
        img = card.index("<img")
        assert img < card.index(">01</span>") and img < card.index("AMHS 제목에는") and img < card.index("MTC AX Insight")      # 이미지가 카드 맨 위
        assert 'width="624"' in doc and "max-width:100%" in doc and "border-radius:15px 15px 0 0" in doc and "padding:10px 18px 16px 18px" in doc
        assert "삼성 에이전트 구성도를 새로 설명한 문장" in doc and "이미지 · 삼성반도체" in doc
        assert "이미지는 각 발행처의 공개 자료이며 저작권은 발행처에 있습니다." in doc
        assert "<a " not in strip_data(doc).lower() and "http" not in strip_data(doc).lower()
    assert re.search(r'<img src="data:image/jpeg;base64,', h) and "cid:" not in h
    cid = out["images"][0][0]
    assert f'<img src="cid:{cid}"' in e and "data:image" not in e
    assert "[이미지] 삼성 에이전트 구성도를 새로 설명한 문장 · 이미지 · 삼성반도체" in out["text"]
    assert "이미지는 각 발행처의 공개 자료이며 저작권은 발행처에 있습니다." in out["text"]
    # 이미지가 있는 카드가 없는 카드보다 위쪽 여백이 작다(이미지 아래 10px, 없으면 16px)
    assert h.count("padding:10px 18px 16px 18px") == 1


def test_b3_eml_uses_same_layout_and_structure():
    ctx = b3_ctx()
    ctx["image_loader"] = lambda ref: (png_bytes(), "삼성반도체")
    d = draft_b3(True)
    built = render.build_all(d, ctx)
    msg = render.build_eml(d, ctx, built)
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert parsed["Subject"] == f"MTC AX Tech Brief {d['issue_title']}" and parsed["To"] is None and parsed["From"] is None
    parts = list(parsed.iter_parts())
    html_part = next(parts[1].iter_parts())
    assert "01 칩메이커" in html_part.get_content() and "Tech Brief" in html_part.get_content()
    assert re.findall(r'<img src="cid:([^"]+)"', html_part.get_content()) == [built["images"][0][0]]
    assert "MTC AX Tech Brief는" in parts[0].get_content() and "01 칩메이커 · 02 장비사" in parts[0].get_content()
    ctx["newsletter_from"] = "brief@company.example"
    assert render.build_eml(d, ctx, built)["From"] == "brief@company.example"


@pytest.mark.parametrize("theme", ["A", "B", "C"])
def test_other_themes_still_render_with_new_footer(theme):
    ctx = render_ctx(theme=theme)
    ctx["image_loader"] = lambda ref: (png_bytes(), "삼성반도체")
    d = draft_b3(True)
    out = render.build_all(copy.deepcopy(d), ctx)
    assert "FAB" in out["html_preview"] and "이미지는 각 발행처의 공개 자료이며 저작권은 발행처에 있습니다." in out["html_preview"]
    assert "삼성전자 DS · 메모리 MTC AX/PI이 매주 화요일" in out["html_preview"]
    out2 = render.build_all(copy.deepcopy(draft_b3(False)), render_ctx(theme=theme))
    assert "이미지는 각 발행처" not in out2["html_preview"]


def test_name_html_variants():
    th = render.load_themes()
    assert render.name_html("MTC AX Tech Brief", "#00b3a4", th["B3"]) == 'MTC AX <span style="color:#0b2b3c;">Tech Brief</span>'
    assert render.name_html("브리프", "#00b3a4", th["B3"]) == "브리프"                      # 단어가 모자라면 꾸미지 않는다
    assert render.name_html("FAB4 위클리", "#00b3a4", th["A"]) == 'FAB<span style="color:#00b3a4;">4</span> 위클리'
