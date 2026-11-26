"""경쟁 칩메이커 이름·별칭 대조(설계서 1장, 4.5, 9.5). 사외 queries.yaml과 사내 config.yaml이 같은 형식(name, aliases)을 쓴다."""
import re
from urllib.parse import urlsplit


def _alias_re(alias):
    a = alias.strip()
    if a.isascii():     # 영문 별칭은 단어 경계를 지킨다(예: UMC가 다른 단어 안에 든 경우 제외)
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(a), re.I)


def company_matcher(company):
    names = [company["name"]] + list(company.get("aliases") or [])
    pats = [_alias_re(n) for n in names if n and n.strip()]
    return lambda text: any(p.search(text or "") for p in pats)


def find_company(companies, *texts):
    """texts를 앞에서부터 차례로 보고, 처음 일치하는 회사 이름을 돌려준다. 없으면 None."""
    for text in texts:
        if not text:
            continue
        for c in companies or []:
            if company_matcher(c)(text):
                return c["name"]
    return None


def record_company(companies, rec):
    """수집 레코드의 발행처·제목·URL 호스트로 회사를 찾는다."""
    host = urlsplit(rec.get("source_url") or "").hostname or ""
    return find_company(companies, rec.get("publisher"), rec.get("title"), host)


def complete_counts(companies, records):
    """회사별 complete 레코드 수."""
    out = {c["name"]: 0 for c in companies or []}
    for r in records:
        if r.get("status") != "complete":
            continue
        n = record_company(companies, r)
        if n:
            out[n] += 1
    return out
