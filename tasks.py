"""The three Session-10 tasks, expressed as catalogue entries.

Each task is a `ComputerTask` the `ComputerSkill` cascade can execute.
Together they satisfy the assignment's constraints:

  Task A — Calculator      Layer 2a deterministic   zero vision
  Task B — VS Code         Electron page path       zero vision
  Task C — canvas target   Layer 3 vision (SoM)     uses vision

So: ≥1 vision (C), ≥1 Electron page path (B), ≥1 zero-vision (A and B).
"""
from __future__ import annotations

from pathlib import Path

from computer import ComputerTask

ROOT = Path(__file__).resolve().parent
TRAJ = ROOT / "trajectories"
ART = ROOT / "reports"
CANVAS_URL = "file://" + str(ROOT / "assets" / "canvas_target.html")

# Stable VS Code activity-bar selector for the Search view.
_VSCODE_SEARCH = ".activitybar .action-item a.action-label[aria-label^='Search']"
_VSCODE_ACTIVE = ("(function(){var e=document.querySelector('.activitybar "
                  ".action-item.checked a.action-label');return e?"
                  "e.getAttribute('aria-label'):'none';})()")


def task_calculator() -> ComputerTask:
    """A — arithmetic via deterministic hotkeys (Layer 2a, zero vision)."""
    return ComputerTask(
        goal="Compute 12 multiplied by 18, then add 7, using the keys.",
        app_name="Calculator",
        bundle_id="com.apple.calculator",
        force_path="deterministic",
        trajectory_dir=str(TRAJ / "calc"),
        artifacts_dir=str(ART / "calc"),
    )


def task_vscode() -> ComputerTask:
    """B — drive VS Code's DOM through the Electron page path (CDP)."""
    return ComputerTask(
        goal="In an isolated VS Code window, confirm the workbench loaded, "
             "open the Search view from the activity bar, and report which "
             "view is active.",
        app_name="Visual Studio Code",
        bundle_id="com.microsoft.VSCode",
        force_path="electron",
        electron_port=9223,
        new_instance=True,
        launch_args=["--user-data-dir", "/tmp/s10_userdata",
                     "--extensions-dir", "/tmp/s10_ext",
                     "--remote-allow-origins=*"],
        open_urls=["/tmp/s10_ws"],
        page_steps=[
            {"action": "query_dom", "css_selector": ".monaco-workbench"},
            {"action": "execute_javascript", "javascript": "document.title"},
            {"action": "click_element", "selector": _VSCODE_SEARCH},
            {"action": "execute_javascript", "javascript": _VSCODE_ACTIVE},
        ],
        verify_title_contains="Search",
        trajectory_dir=str(TRAJ / "vscode"),
        artifacts_dir=str(ART / "vscode"),
    )


def task_canvas() -> ComputerTask:
    """C — click a canvas-rendered shape; forces Layer 3 vision."""
    return ComputerTask(
        goal="Click the center of the red circle.",
        app_name="Google Chrome",
        bundle_id="com.google.Chrome",
        force_path="vision",
        new_instance=True,
        launch_args=["--user-data-dir=/tmp/s10_chrome", "--no-first-run",
                     "--no-default-browser-check", "--new-window"],
        open_urls=[CANVAS_URL],
        pid_match="user-data-dir=/tmp/s10_chrome",
        verify_title_contains="HIT-RED",
        max_turns=6,
        trajectory_dir=str(TRAJ / "canvas"),
        artifacts_dir=str(ART / "canvas"),
    )


CATALOGUE = {
    "calc": ("Calculator — Layer 2a deterministic (zero vision)", task_calculator),
    "vscode": ("VS Code — Electron page path (zero vision)", task_vscode),
    "canvas": ("Canvas target — Layer 3 vision / set-of-marks", task_canvas),
}
