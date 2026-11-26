"""사내 파이프라인 설정과 경로. 설정값은 config.yaml의 internal·thresholds·extraction만 읽는다."""
from dataclasses import dataclass
from pathlib import Path

from collector.settings import ROOT, TOPIC_CODES, load_yaml

TOPICS = TOPIC_CODES


@dataclass
class Settings:
    config: dict
    data_root: Path        # review/, ledger/, out_internal/, log/ 가 놓이는 곳(기본: 저장소 루트)

    @property
    def internal(self):
        return self.config["internal"]

    @property
    def llm_cfg(self):
        return self.config["internal"]["llm"]

    @property
    def lead_min_chars(self):
        return self.config["extraction"]["lead_min_chars"]

    @property
    def intake_min(self):
        return self.config["thresholds"]["intake_min"]

    @property
    def prompts_dir(self):
        return ROOT / "prompts"

    @property
    def review_dir(self):
        return self.data_root / "review"

    @property
    def ledger_path(self):
        return self.data_root / "ledger" / "events.jsonl"

    @property
    def pilot_csv(self):
        return self.data_root / "log" / "pilot.csv"

    def out_dir(self, week):
        return self.data_root / "out_internal" / week

    def review_path(self, week, suffix):
        return self.review_dir / f"{week}-{suffix}"

    def state_path(self, week):
        return self.review_path(week, "state.json")

    def selection_path(self, week):
        return self.review_path(week, "selection.csv")

    def draft_path(self, week):
        return self.review_path(week, "draft.yaml")

    def review_log_path(self, week):
        return self.review_path(week, "review-log.jsonl")


def load(data_root=None, config_path=None):
    cfg = load_yaml(config_path or ROOT / "config.yaml")
    return Settings(cfg, Path(data_root) if data_root else ROOT)
