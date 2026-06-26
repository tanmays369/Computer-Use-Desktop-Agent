"""Replay report for the Session-10 Computer-Use runs.

Reads each task's persisted `result.json` (written by run_all.py) plus its
recorded trajectory, and emits an 8-section markdown report per task — the
desktop analogue of the Session-9 browser replay viewer. The cascade layer
that produced the answer is surfaced exactly like Browser's `path`.

Usage:
    python report.py            # report on every task with a result.json
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
TRAJ = ROOT / "trajectories"

LAYER_RATIONALE = {
    "extract": "Read content straight from the AX tree / clipboard / file. "
               "No click, no LLM, $0.",
    "deterministic": "Goal maps to a fixed, known input sequence; an LLM "
                     "plans it ONCE, then the loop dispatches with no LLM in "
                     "it. Zero vision.",
    "a11y": "A cheap text LLM reads the AX-tree markdown and emits one "
            "element-indexed action per turn (scan-act-verify).",
    "vision": "The target is canvas/pixels with no actionable AX node, so "
              "the cascade escalates to a screenshot + set-of-marks + vision "
              "LLM, clicking by coordinate.",
    "blocked": "A precondition failed (permissions / no window).",
}


def _turns(key: str) -> int:
    d = TRAJ / key
    return len(list(d.glob("turn-*"))) if d.exists() else 0


def _last_screenshot(key: str) -> Path | None:
    d = TRAJ / key
    if not d.exists():
        return None
    turns = sorted(d.glob("turn-*"))
    for t in reversed(turns):
        p = t / "screenshot.png"
        if p.exists():
            return p
    return None


def _cost_lines(cost: dict) -> list[str]:
    rows = []
    for agent, entries in (cost or {}).items():
        for e in entries:
            if not e.get("calls"):
                continue
            rows.append(
                f"| {e.get('provider')} | {e.get('calls')} | "
                f"{e.get('in_tok',0)} | {e.get('out_tok',0)} | "
                f"{e.get('ok',0)}/{e.get('calls',0)} | "
                f"${e.get('dollars',0):.5f} |")
    return rows


def _fmt_action(a: dict) -> str:
    kind = a.get("type") or a.get("action") or "?"
    arg = (a.get("selector") or a.get("key") or a.get("text")
           or a.get("mark") or (f"({a.get('x')},{a.get('y')})"
                                 if "x" in a else "") or "")
    if a.get("keys"):
        arg = "+".join(a["keys"])
    return f"{kind}({arg})"


def render(key: str, r: dict) -> str:
    L = []
    L.append(f"# Replay — {r.get('label', key)}\n")

    L.append("## 1. Task\n")
    L.append(f"- **Goal:** {r['goal']}")
    L.append(f"- **App:** {r['app']}")
    L.append(f"- **Session:** `{r.get('session','')}`\n")

    L.append("## 2. Cascade path\n")
    L.append(f"- **Layer used:** `{r['path']}`")
    L.append(f"- **Vision calls:** {r['vision_calls']}  |  "
             f"**LLM calls:** {r['llm_calls']}\n")

    L.append("## 3. Why this layer\n")
    L.append(LAYER_RATIONALE.get(r["path"], "") + "\n")

    L.append("## 4. Actions (scan → act → verify)\n")
    L.append("| turn | action(s) | outcome |")
    L.append("|---|---|---|")
    for a in r.get("actions", []):
        acts = ", ".join(_fmt_action(x) for x in a.get("actions", [])) or "—"
        L.append(f"| {a.get('turn','')} | {acts} | {a.get('outcome','')[:70]} |")
    L.append("")

    L.append("## 5. Screenshot (last recorded turn)\n")
    shot = _last_screenshot(key)
    if shot:
        rel = shot.relative_to(REPORTS.parent)
        L.append(f"![last turn](../../{rel})\n")
    else:
        L.append("_no screenshot recorded_\n")

    L.append("## 6. Verification\n")
    L.append(f"- **Success:** {r['success']}")
    L.append(f"- **Result / final state:** {r.get('result_text') or r.get('final_state','')}")
    if r.get("error"):
        L.append(f"- **Error:** {r['error']}")
    L.append("")

    L.append("## 7. Cost (V9 ledger, agent=computer)\n")
    rows = _cost_lines(r.get("cost", {}))
    if rows:
        L.append("| provider | calls | in_tok | out_tok | ok | est $ |")
        L.append("|---|---|---|---|---|---|")
        L.extend(rows)
    else:
        L.append("_no LLM calls (pure deterministic / CDP path)_")
    L.append("")

    L.append("## 8. Trajectory evidence\n")
    L.append(f"- **Turns recorded:** {_turns(key)}")
    L.append(f"- **Trajectory dir:** `trajectories/{key}/` "
             f"(per-turn `action.json` + `screenshot.png` + `app_state.json`)")
    L.append(f"- Replay with: "
             f"`cua-driver call replay_trajectory "
             f"'{{\"trajectory_dir\":\"{(TRAJ / key)}\"}}'`\n")
    return "\n".join(L)


def main() -> None:
    index = ["# Session 10 — Computer-Use replay reports\n"]
    for key in ("calc", "vscode", "canvas"):
        rj = REPORTS / key / "result.json"
        if not rj.exists():
            continue
        r = json.loads(rj.read_text())
        md = render(key, r)
        (REPORTS / key / "report.md").write_text(md)
        index.append(f"- **{key}** — `{r['path']}` — success={r['success']} "
                     f"— [report](./{key}/report.md)")
        print(f"wrote reports/{key}/report.md")
    (REPORTS / "INDEX.md").write_text("\n".join(index) + "\n")
    print("wrote reports/INDEX.md")


if __name__ == "__main__":
    main()
