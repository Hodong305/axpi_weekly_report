"""config.yaml, sources.yaml, queries.yaml 로딩과 경로 규칙."""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "MTC-AX-collection-v4.5.schema.json"
SCHEMA_VERSION = "4.5"
TOPIC_CODES = ["T1_CHIPMAKER", "T2_EQUIPMENT", "T3_SOLUTION", "T4_PHYSICAL_AI"]
TOPIC_SHORT = {"T1": "T1_CHIPMAKER", "T2": "T2_EQUIPMENT", "T3": "T3_SOLUTION", "T4": "T4_PHYSICAL_AI"}
PROMPT_TOPICS = ["T1", "T2", "T3", "T4", "B"]
LLM_AGENTS = ["claude", "gpt", "gemini"]
WEEK_RE = re.compile(r"^[0-9]{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Settings:
    config: dict
    sources: list
    source_meta: dict
    queries: dict
    root: Path

    @property
    def c(self):
        return self.config

    def path(self, key):
        return self.root / self.config["paths"][key]

    def work_dir(self, issue_week):
        return self.path("work_dir") / issue_week

    def out_dir(self, issue_week):
        return self.path("out_dir") / issue_week

    def candidates_dir(self, issue_week):
        return self.work_dir(issue_week) / "candidates"

    def manual_dir(self, issue_week):
        return self.work_dir(issue_week) / "manual"

    def source_by_id(self, sid):
        for s in self.sources:
            if s["id"] == sid:
                return s
        return None

    def source_for_url(self, url):
        """url_prefixes(가장 긴 일치) → domains 순으로 발행처를 찾는다."""
        best, blen = None, 0
        for s in self.sources:
            for pre in s.get("url_prefixes", []):
                if url.startswith(pre) and len(pre) > blen:
                    best, blen = s, len(pre)
        if best:
            return best
        from urllib.parse import urlsplit
        return self.source_for_host(urlsplit(url).hostname)

    def source_for_host(self, host):
        host = (host or "").lower()
        if host.startswith("www."):
            host = host[4:]
        for s in self.sources:
            for d in s.get("domains", []):
                d = d.lower()
                if host == d or host.endswith("." + d):
                    return s
        return None

    def blocked(self, host):
        host = (host or "").lower()
        for d in self.config.get("blocked_domains", []) or []:
            d = d.lower()
            if host == d or host.endswith("." + d):
                return True
        return False


def load_settings(root=None, config=None, sources=None, queries=None):
    root = Path(root) if root else ROOT
    cfg = load_yaml(config or root / "config.yaml")
    src = load_yaml(sources or root / "sources.yaml")
    qry = load_yaml(queries or root / "queries.yaml")
    ids = [s["id"] for s in src.get("sources", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("sources.yaml: id 중복")
    for s in src.get("sources", []):
        bad = [t for t in s.get("topics", []) if t not in TOPIC_CODES]
        if bad:
            raise ValueError(f"sources.yaml {s['id']}: 알 수 없는 분야 코드 {bad}")
    return Settings(cfg, src.get("sources", []), {k: v for k, v in src.items() if k != "sources"}, qry, root)


def file_name(issue_week, kind, revision, ext="json"):
    return f"mtc-ax-{issue_week.replace('-', '')}-{kind}-r{revision}.{ext}"
