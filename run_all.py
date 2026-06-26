"""Run the Session-10 task catalogue end to end.

For each task: spin a fresh session id (clean cost ledger), pre-clean any
stale app instance, run the ComputerSkill cascade (which wraps the run in
start_recording / stop_recording), then persist the result JSON and the
per-session cost rollup from the V9 gateway.

Usage:
    python run_all.py            # run all three tasks
    python run_all.py calc       # run one task by catalogue key
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from computer import ComputerSkill
from tasks import CATALOGUE

ROOT = Path(__file__).resolve().parent


def _clean(key: str) -> None:
    """Kill stale instances so each run starts from a known UI state (§13)."""
    if key == "calc":
        subprocess.run(["pkill", "-x", "Calculator"], capture_output=True)
    elif key == "vscode":
        subprocess.run(["pkill", "-f", "user-data-dir /tmp/s10_userdata"],
                       capture_output=True)
    elif key == "canvas":
        subprocess.run(["pkill", "-f", "user-data-dir=/tmp/s10_chrome"],
                       capture_output=True)
    time.sleep(1.5)


async def run_one(key: str) -> dict:
    label, factory = CATALOGUE[key]
    print(f"\n=== {key}: {label} ===")
    _clean(key)
    session = f"s10-{key}-{int(time.time())}"
    skill = ComputerSkill(session=session)
    task = factory()
    out = await skill.run(task)

    cost = await skill.client.cost_by_agent(session=session)
    result = out.to_dict()
    result["session"] = session
    result["label"] = label
    result["cost"] = cost

    outdir = Path(task.artifacts_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "result.json").write_text(json.dumps(result, indent=2))

    print(f"  path={out.path}  success={out.success}  turns={out.turns}  "
          f"vision_calls={out.vision_calls}  llm_calls={out.llm_calls}")
    print(f"  result_text={out.result_text!r}")
    if out.error:
        print(f"  error={out.error}")
    print(f"  trajectory={out.trajectory_dir}")
    return result


async def main() -> None:
    keys = sys.argv[1:] or list(CATALOGUE.keys())
    summary = []
    for key in keys:
        if key not in CATALOGUE:
            print(f"unknown task {key!r}; choices: {list(CATALOGUE)}")
            continue
        summary.append(await run_one(key))

    print("\n===== SUMMARY =====")
    for r in summary:
        print(f"{r['goal'][:42]:44} layer={r['path']:13} "
              f"ok={str(r['success']):5} vision={r['vision_calls']} "
              f"llm={r['llm_calls']}")
    (ROOT / "reports" / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
