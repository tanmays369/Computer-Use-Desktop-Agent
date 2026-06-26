"""How the Computer skill drops into the Session-9 catalogue (brief §12).

The S9 orchestrator (`flow.py`) does not change. Integration is two small
pieces, mirroring exactly how Browser is wired:

1. A catalogue entry (same shape as Browser's in `agent_config.yaml`):

       computer:
         prompt: prompts/computer.md
         temperature: 0.0
         max_tokens: 1024
         description: |
           Drives desktop apps through a four-layer cascade
           (extract, deterministic, a11y, vision) on cua-driver.
           Inputs in metadata: goal, app_name, bundle_id (required);
           force_path / electron_port / page_steps (optional).
           Returns ComputerOutput with the chosen layer as output.path.

2. ONE dispatch branch in `skills.py` (the Browser branch already there is
   the template) — this is the "one line" the brief refers to:

       if skill.name == "computer":
           from computer.skill import ComputerSkill
           from integration import nodespec_to_task
           sk = ComputerSkill(session=session_id)
           task = nodespec_to_task(graph_nodes[node_id], query)
           return await sk.run(task), rendered

The V9 gateway handles every LLM and vision call (no new gateway); the
replay viewer surfaces `output.path` like Browser's; the cost ledger tags
calls under `agent: computer`.

`nodespec_to_task` below performs the metadata→ComputerTask mapping the
dispatch branch needs.
"""
from __future__ import annotations

from computer import ComputerTask

# The catalogue entry as a Python dict (load into agent_config.yaml or merge
# at registry-build time).
CATALOGUE_ENTRY = {
    "computer": {
        "prompt": "prompts/computer.md",
        "temperature": 0.0,
        "max_tokens": 1024,
        "description": (
            "Drives desktop apps through a four-layer cascade (extract, "
            "deterministic, a11y, vision) on cua-driver. Inputs in metadata: "
            "goal, app_name, bundle_id (required); force_path / electron_port "
            "/ page_steps (optional). Returns ComputerOutput with the chosen "
            "layer surfaced as output.path."),
    }
}


def nodespec_to_task(node: dict, query: str) -> ComputerTask:
    """Map an S9 graph node's `metadata` to a ComputerTask. Mirrors how the
    Browser branch builds its NodeSpec from `metadata` (url + goal)."""
    md = (node.get("metadata") or {}) if isinstance(node, dict) else {}
    return ComputerTask(
        goal=md.get("goal") or query,
        app_name=md.get("app_name", ""),
        bundle_id=md.get("bundle_id", ""),
        force_path=md.get("force_path"),
        electron_port=md.get("electron_port"),
        page_steps=md.get("page_steps"),
        open_urls=md.get("open_urls"),
        launch_args=md.get("launch_args"),
        new_instance=bool(md.get("new_instance", False)),
        pid_match=md.get("pid_match"),
        a11y_query=md.get("a11y_query"),
        verify_title_contains=md.get("verify_title_contains"),
        max_turns=int(md.get("max_turns", 12)),
        trajectory_dir=md.get("trajectory_dir"),
        artifacts_dir=md.get("artifacts_dir"),
    )
