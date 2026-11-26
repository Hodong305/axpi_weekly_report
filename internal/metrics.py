"""시범 지표(설계서 11장) → log/pilot.csv. 검토 요청 기록과 반입 JSON에서 계산할 수 있는 열만 둔다."""
import csv

AGENTS = ["claude", "gpt", "gemini", "script", "manual"]

COLUMNS = (["issue_week", "issue_type", "config_version", "collected", "intake_complete", "intake_partial", "intake_drop",
            "intake_paywall", "resurvey_run", "manual_attempts", "pass_docs", "events", "published", "core", "brief",
            "core_eligible", "r1_approved_as_is", "r1_changed", "r1_change_ratio", "r1_lifted", "r1_lowered",
            "r1_ledger_changes", "r1_invalid_responses", "r2_A_custom", "r2_B_custom", "r2_C_revisions", "r2_d_changed",
            "r2_d_total", "r2_title_replace_rate", "warnings_total", "warnings_fix_chosen", "review1_sec", "review2_sec",
            "review3_sec", "review_sec_total"]
           + [f"agent_{a}_{k}" for a in AGENTS for k in ("complete", "unique")])


def discovery_stats(doc):
    out = {}
    for a in AGENTS:
        agents = [{d["agent"] for d in r["discovery"]} for r in doc["records"] if r["status"] == "complete"]
        out[f"agent_{a}_complete"] = sum(a in s for s in agents)
        out[f"agent_{a}_unique"] = sum(s == {a} for s in agents)
    return out


def build_row(state, doc, log, config_version):
    m = state.get("metrics", {})
    r1, r2 = m.get("r1", {}), m.get("r2", {})
    s = doc["summary"]
    pub = [e for e in state["events"] if e["slot"] in ("core", "brief")]
    t1, t2, t3 = log.elapsed("R1"), log.elapsed("R2"), log.elapsed("R3")
    invalid = sum(1 for r in log.read() if r["kind"] == "response" and r.get("valid") is False and r.get("review", "").startswith("R1"))
    manual = sum(1 for r in doc["records"] for a in r["attempts"] if a["agent"] == "manual")
    row = {"issue_week": state["issue_week"], "issue_type": state["issue_type"], "config_version": config_version,
           "collected": s["records"], "intake_complete": s["complete"], "intake_partial": s["partial"],
           "intake_drop": s["drop"], "intake_paywall": s["paywall"], "resurvey_run": int("resurvey" in doc),
           "manual_attempts": manual, "pass_docs": state["counts"]["pass_docs"], "events": state["counts"]["events"],
           "published": len(pub), "core": sum(e["slot"] == "core" for e in pub), "brief": sum(e["slot"] == "brief" for e in pub),
           "core_eligible": state["counts"]["core_eligible"],
           "r1_approved_as_is": r1.get("approved_as_is", ""), "r1_changed": r1.get("changed", ""),
           "r1_change_ratio": (round(r1["changed"] / max(1, len(pub)), 3) if "changed" in r1 else ""),
           "r1_lifted": r1.get("lifted", ""), "r1_lowered": r1.get("lowered", ""),
           "r1_ledger_changes": r1.get("ledger_changes", ""), "r1_invalid_responses": invalid,
           "r2_A_custom": int(r2.get("A_custom", False)), "r2_B_custom": int(r2.get("B_custom", False)),
           "r2_C_revisions": r2.get("c_revisions", 0), "r2_d_changed": r2.get("d_changed", 0),
           "r2_d_total": r2.get("d_total", 0),
           "r2_title_replace_rate": round(r2.get("d_changed", 0) / r2["d_total"], 3) if r2.get("d_total") else 0,
           "warnings_total": r2.get("e_total", 0), "warnings_fix_chosen": r2.get("e_fix", 0),
           "review1_sec": round(t1, 1), "review2_sec": round(t2, 1), "review3_sec": round(t3, 1),
           "review_sec_total": round(t1 + t2 + t3, 1)}
    row.update(discovery_stats(doc))
    return row


def upsert_row(path, row):
    """같은 issue_week 행이 있으면 바꾸고 없으면 덧붙인다(S8 재실행에도 중복되지 않는다)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            rows = [{k: r.get(k, "") for k in COLUMNS} for r in csv.DictReader(f) if r["issue_week"] != row["issue_week"]]
    rows.append({k: row.get(k, "") for k in COLUMNS})
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
