# Session 10 — Computer-Use Agent

A Computer-Use skill that drives real macOS desktop applications through a
five-layer cascade on top of `cua-driver`, and three recorded task runs.
Drops into the Session-9 catalogue alongside `Browser`; the S9 orchestrator
(`flow.py`) is unchanged. All LLM and vision calls go through the V9 gateway
from Session 9. No paid APIs, no third-party agentic frameworks.

## Result

| Task | Target | Layer used | Vision calls | LLM calls | Success | Est. cost |
|---|---|---|---|---|---|---|
| A — arithmetic | Calculator (native) | `deterministic` (2a) | 0 | 1 (plan) | yes (`223`) | $0.00028 |
| B — open Search view | VS Code (Electron) | Electron page path | 0 | 0 | yes (`Search`) | $0.00000 |
| C — click red circle | Chrome canvas (pixels) | `vision` (3) | 1 | 1 | yes (`HIT-RED`) | $0.00043 |

Constraints: **≥1 vision** (C), **≥1 Electron page path** (B), **≥1 zero-vision**
(A and B). Reports: [`reports/INDEX.md`](reports/INDEX.md). Trajectories:
[`trajectories/`](trajectories/).

## The five layers

`cua-driver` provides perception (AX tree, screenshots) and action (clicks,
keystrokes, recording). Everything above it is in `computer/`:

| Layer | What it does | Where | Cost |
|---|---|---|---|
| Precondition | TCC Accessibility + Screen Recording; AppleScript activate | `driver.permissions_ok`, `driver.activate` | — |
| 1 — extract | read AX text / **clipboard** / file directly | `skill._extract_result`, `driver.clipboard` | $0 |
| 2a — deterministic | LLM plans a keystroke list ONCE, loop dispatches with no LLM | `skill._deterministic` | ~$0 |
| 2b — a11y | cheap text LLM reads AX markdown, emits one element-indexed action/turn | `skill._a11y` | cents |
| 3 — vision | screenshot → **set-of-marks** → vision LLM picks a mark → click by coord | `skill._vision`, `marks.py` | dollars |

The cascade is `extract → deterministic → a11y → vision`, the same shape as
Session 9's Browser skill. The layer that produced the answer is surfaced on
`ComputerOutput.path`, so the replay viewer shows it the way it shows
Browser's `path`. The a11y layer can return `escalate`, which climbs the
cascade to vision (`skill._run_inner`).

### scan → act → verify

Every element-indexed turn runs `get_window_state` (scan) → action (act) →
`get_window_state` (verify), re-scanning after every state change because
`element_index` is a turn-scoped token (brief §7). The empty-tree
precondition (`element_count == 0`) is raised as a typed `PreconditionError`
listing the four causes (permissions / background launch / Qt env / Electron),
so the same surface symptom is not mistaken for four different bugs (§8).

## The three tasks

### A. Calculator — Layer 2a deterministic (zero vision)
`Compute 12 × 18 + 7`. A text LLM converts the goal to a keystroke list
once (`["1","2","*","1","8","+","7","="]`); the dispatch loop sends them with
no LLM in the loop. Verify is a **Layer-1 clipboard extract**: `Cmd+C` then
`pbpaste` → `223` (Calculator exposes no result element in its AX tree, so the
clipboard is the cheap, reliable post-condition).

### B. VS Code — Electron page path (zero vision)
Launch an **isolated** VS Code instance (separate `--user-data-dir`, scratch
workspace, own debug port) so the user's real editor is never touched (§13).
Drive its DOM through the CDP page path: confirm `.monaco-workbench`, read
`document.title`, click the Search activity-bar item, and confirm via CDP that
the Search view became active. The click is issued by `cua-driver` at the
element's on-screen rect, so the agent-cursor overlay moves and the click
lands in the recorded trajectory (not a silent DOM `.click()`).

### C. Chrome canvas — Layer 3 vision (set-of-marks)
A local HTML page paints four shapes on a single `<canvas>`; nothing is in the
AX tree, so "click the red circle" forces vision. The skill screenshots the
window, overlays a numbered grid (set-of-marks), asks the V9 vision endpoint
which mark sits on the red circle, and clicks that mark's known pixel center.
On a correct click the page sets `document.title = HIT-RED`, which the skill
verifies via the window title — no extra vision call.

## Cascade decisions

- **Task A is forced to 2a, not 2b.** Arithmetic is a known fixed sequence;
  paying per-turn LLM cost to click number buttons would be the exact "escalate
  too far" mistake the brief warns about.
- **Task B uses the Electron page path, not the AX tree.** VS Code is one
  opaque `AXWebArea`; the AX tree has no addressable workbench elements. The
  CDP/CSS path is the intended escape hatch (§9).
- **Task C uses vision, not a11y.** A `<canvas>` has no per-shape AX nodes;
  the a11y layer would correctly `escalate`, so the task pins vision directly.
- **set-of-marks over raw coordinates.** The only free vision model available
  (see failure modes) cannot regress pixel coordinates; it returned generic
  `(250,250)` guesses. Numbered-mark classification is reliable where
  coordinate regression is not.

## Failure modes encountered

1. **Gemini free-tier vision is quota-exhausted (HTTP 429).** It was the only
   vision-capable provider wired in the V9 gateway. Fix: added `llama-4` to the
   gateway's vision-model hints and one guard so an explicitly pinned,
   vision-capable provider+model is not dropped by the static capability filter
   (`providers.py`, `main.py`). Vision now routes to Groq's free multimodal
   `llama-4-scout`.
2. **Free VLM cannot regress coordinates.** `llama-4-scout` returned round,
   generic `(x,y)` guesses. Fix: set-of-marks (`marks.py`) — the model picks a
   number, we own the arithmetic.
3. **cua-driver `page` RPC hangs on this Chromium build.** Raw CDP works, but
   the websocket handshake hits Chromium's `--remote-allow-origins` gate
   (added in recent Chromium) and cua-driver 0.6.8's `page` tool blocks
   indefinitely. Fixes: launch Electron with `--remote-allow-origins=*`, and
   `driver.page()` falls back to a thin direct-CDP call (same action, same
   port) after an 8 s timeout. cua-driver still owns the substrate (launch +
   debug port + recording + the visible click).
4. **macOS background-launch trap (§8.2).** `launch_app` does not steal focus,
   so the first AX walk sees only the menu bar. Fix: AppleScript `activate` +
   settle before scanning (`driver.activate`).
5. **`launch_app` NSWorkspace timeout on cold Chrome.** Chrome with a fresh
   profile takes >30 s; `launch_app` returns an error without a pid even though
   the app started. Fix: recover the main pid by argv match
   (`driver.find_main_pid`).
6. **press_key has no `*` keyname; Calculator has no AX result element.**
   `*` is sent as Shift+8, `+` as `plus`; the result is read from the clipboard.
7. **Pixel clicks need the window frontmost.** A vision click ~20 s after launch
   silently no-ops if focus drifted. Fix: re-activate immediately before the
   click.
8. **Recording paths must be absolute** — the daemon resolves relative paths
   against its own cwd, so turn folders silently land elsewhere. Fix: resolve
   to absolute before `start_recording`.

## Recording

Every run is wrapped in `start_recording` / `stop_recording`
(`skill.run`). Each action writes a turn folder under `trajectories/<task>/`
with `action.json`, `screenshot.png`, `app_state.json`. Replay:

```bash
cua-driver call replay_trajectory '{"trajectory_dir":"<abs>/trajectories/calc"}'
```

## Catalogue integration (S9)

`integration.py` shows the two pieces (catalogue entry + the one `if
skill.name == "computer"` dispatch branch) and the `metadata → ComputerTask`
mapper. The S9 `flow.py` is unchanged; the V9 gateway handles every LLM and
vision call; the cost ledger tags calls under `agent: computer`.

## Layout

```
computer/        the skill: driver wrapper, V9 client, cascade, set-of-marks
  driver.py      thin wrapper over `cua-driver call …` + CDP fallback
  skill.py       the five layers (ComputerSkill / ComputerTask / ComputerOutput)
  marks.py       set-of-marks overlay for Layer 3
  client.py      framework-free V9 gateway client (reused from S9)
  prompts.py     planner / a11y-judge / vision system prompts
  schemas.py     ComputerOutput + action schemas
assets/          canvas_target.html (Layer-3 target)
tasks.py         the three tasks as catalogue entries
run_all.py       run the catalogue, record, persist results + cost
report.py        8-section replay report per task
reports/         result.json + report.md per task; INDEX.md
trajectories/    recorded turn folders (submission evidence)
integration.py   how it drops into the S9 catalogue
```

## Run it

```bash
# 1. cua-driver daemon + permissions (one-time)
cua-driver status            # or: cua-driver serve
cua-driver permissions grant # Accessibility + Screen Recording

# 2. V9 gateway from Session 9 on :8109 must be running.

# 3. deps
pip install -r requirements.txt

# 4. run all three tasks (records trajectories, writes reports)
python run_all.py            # or: python run_all.py calc
python report.py
```

Requires macOS (primary OS). The Electron and vision tasks use isolated
app instances / a scratch profile so a run never touches the user's real
files (§13).

## YouTube demo

Not recorded.
