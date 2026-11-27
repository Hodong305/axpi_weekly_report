"""GPT·Gemini 통합 탐색 프롬프트(ALL.txt)를 휴대폰에서 복사·내려받을 수 있는 페이지로 만든다.

  python tools/prompt_page.py --issue-week 2026-W48 [--out out_internal/prompt-page.html]

매주 같은 출력 파일로 만들어 같은 아티팩트에 다시 게시한다(주차·기간·생성 시각이 페이지 머리에 나온다).
페이지는 downloads 기능으로 ALL-{주차}.txt를 내려받게 하고, 복사 버튼과 전체 본문을 함께 보인다.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = r"""<title>MTC AX 탐색 프롬프트</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* 레이아웃: 한 열. 위에 주차 머리 + 세 단계 안내 + 버튼, 아래에 프롬프트 전문(스크롤 상자) */
:root {
  --bg: #f4f6f5; --surface: #ffffff; --fg: #14211d; --muted: #5d6b66; --line: #d9e2df;
  --accent: #00806f; --accent-fg: #ffffff; --code-bg: #f7faf9; --ok: #0a7a3e; --warn: #a15c00;
  --font-body: "Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f1614; --surface: #17211e; --fg: #e6efec; --muted: #9db0aa; --line: #2a3834;
  --accent: #3cc4ae; --accent-fg: #06201b; --code-bg: #121b18; --ok: #5fd48f; --warn: #f0b45a; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #0f1614; --surface: #17211e; --fg: #e6efec; --muted: #9db0aa; --line: #2a3834;
  --accent: #3cc4ae; --accent-fg: #06201b; --code-bg: #121b18; --ok: #5fd48f; --warn: #f0b45a; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--font-body); font-size: 15px; line-height: 1.6;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 20px 32px; }
.wrap { max-width: 760px; margin: 0 auto; display: grid; gap: 16px; }
.head { display: grid; gap: 4px; }
.eyebrow { font-size: 12px; letter-spacing: 0.06em; color: var(--accent); font-weight: 700; }
h1 { font-size: 24px; line-height: 1.3; margin: 0; text-wrap: balance; }
.meta { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; min-width: 0; }
ol { margin: 0; padding-left: 20px; display: grid; gap: 6px; }
ol b { font-weight: 700; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button { font: inherit; font-weight: 700; font-size: 15px; border-radius: 10px; padding: 10px 16px; cursor: pointer; border: 1px solid var(--accent); }
button.primary { background: var(--accent); color: var(--accent-fg); }
button.secondary { background: transparent; color: var(--accent); }
button:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
button[disabled] { opacity: 0.5; cursor: default; }
.status { font-size: 13px; color: var(--muted); min-height: 1.4em; }
.status.ok { color: var(--ok); } .status.warn { color: var(--warn); }
.prompt-head { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
pre { margin: 0; background: var(--code-bg); border: 1px solid var(--line); border-radius: 10px; padding: 14px;
  max-height: 60vh; overflow: auto; white-space: pre-wrap; word-break: break-word; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.65; }
.note { font-size: 13px; color: var(--muted); }
</style>

<div class="wrap">
  <header class="head">
    <div class="eyebrow">GPT · GEMINI 통합 탐색 프롬프트</div>
    <h1 id="title">이번 주 탐색 프롬프트</h1>
    <div class="meta" id="meta"></div>
  </header>

  <section class="card" aria-labelledby="how">
    <strong id="how">쓰는 법</strong>
    <ol>
      <li><b>전체 복사</b> 또는 <b>파일로 받기</b>를 누릅니다. 내용은 고치지 않습니다.</li>
      <li>ChatGPT와 Gemini 앱에 각각 붙여넣습니다. <b>웹 검색을 꼭 켜 주세요.</b></li>
      <li>받은 답을 표시 없이 그대로 Claude 세션에 붙여넣습니다. 답이 잘리면 "잘렸으니 이어서 계속 출력해줘"로 이어서 받습니다.</li>
    </ol>
    <div class="actions">
      <button class="primary" id="copy" type="button">전체 복사</button>
      <button class="secondary" id="save" type="button" hidden>파일로 받기 (.txt)</button>
    </div>
    <div class="status" id="status" role="status" aria-live="polite"></div>
  </section>

  <section class="card" aria-labelledby="body-title">
    <div class="prompt-head"><strong id="body-title" style="color:var(--fg)">프롬프트 전문</strong><span id="size"></span></div>
    <pre id="prompt" tabindex="0"></pre>
    <div class="note">이 페이지는 매주 새 주차의 프롬프트로 다시 게시됩니다. 주차와 기간이 맞는지 위에서 확인하세요.</div>
  </section>
</div>

<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (id) => document.getElementById(id);
$("title").textContent = D.week + " 탐색 프롬프트";
$("meta").textContent = "수집 기간 " + D.period + " · 생성 " + D.generated;
$("prompt").textContent = D.text;
$("size").textContent = D.text.length.toLocaleString("ko-KR") + "자";
const status = (msg, kind) => { const s = $("status"); s.textContent = msg; s.className = "status" + (kind ? " " + kind : ""); };

$("copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(D.text);
    status("복사했습니다. ChatGPT·Gemini에 붙여넣으세요.", "ok");
  } catch (e) {
    const r = document.createRange(); r.selectNodeContents($("prompt"));
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    status("자동 복사가 막혀 있어 본문을 선택해 두었습니다. 길게 눌러 복사하세요.", "warn");
  }
});

let downloads = null;
(async () => {
  try { downloads = await window.claude?.use?.("downloads"); } catch (e) { downloads = null; }
  if (downloads) $("save").hidden = false;
})();
$("save").addEventListener("click", async () => {
  if (!downloads) return;
  try {
    await downloads.save({ filename: "ALL-" + D.week + ".txt", data: D.text });
    status("파일을 저장했습니다.", "ok");
  } catch (e) {
    const code = e && e.code;
    if (code === "declined") status("저장을 취소했습니다.", "");
    else if (code === "rate_limited") status("잠시 뒤 다시 눌러 주세요.", "warn");
    else if (code === "host_unavailable") { $("save").hidden = true; status("이 앱에서는 파일 저장이 안 됩니다. 전체 복사를 쓰거나 브라우저에서 열어 주세요.", "warn"); }
    else { $("save").hidden = true; status("여기서는 파일로 받을 수 없습니다. 전체 복사를 써 주세요.", "warn"); }
  }
});
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-week", required=True)
    ap.add_argument("--out", default=str(ROOT / "out_internal" / "prompt-page.html"))
    a = ap.parse_args()
    src = ROOT / "work" / a.issue_week / "prompts" / "ALL.txt"
    if not src.exists():
        sys.exit(f"{src}가 없습니다. 먼저 python tools/combine_prompts.py combine --issue-week {a.issue_week}")
    text = src.read_text(encoding="utf-8")
    import re
    m = re.search(r"\[기간\] (\S+) 이상 (\S+) 미만", text)
    period = ""
    if m:
        from datetime import date, timedelta
        s, e = date.fromisoformat(m.group(1)[:10]), date.fromisoformat(m.group(2)[:10]) - timedelta(days=1)
        period = f"{s.isoformat()} ~ {e.isoformat()}"
    data = {"week": a.issue_week, "period": period, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text}
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
