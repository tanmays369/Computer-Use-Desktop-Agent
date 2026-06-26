"""ComputerSkill — the five layers above cua-driver (brief §10).

cua-driver gives perception + action. This skill owns the rest:

  Goal decomposition      → which layer to run for this task
  Perception interpretation → AX-tree markdown filtered for the judge LLM
  Action sequencing       → the scan-act-verify loop, re-scan invariant
  Error recovery          → empty-tree precondition, escalate-on-miss
  Vision fallback         → screenshot → V9 /v1/vision → click by (x,y)

The cascade is `extract → deterministic → a11y → vision`, the same shape
as Session 9's Browser skill, with desktop logic at each layer. The
layer that produced the answer is surfaced on `ComputerOutput.path` so
the replay viewer shows it the same way it shows Browser's path.

Recording (§11): when `trajectory_dir` is set on the task, the run is
wrapped in start_recording / stop_recording so every action writes a
turn folder of (tool, args, screenshot, app_state).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path as FsPath

from .client import V9Client
from .driver import Driver, PreconditionError, DriverError
from .prompts import (A11Y_JUDGE_SYSTEM, PLAN_KEYS_SYSTEM, VISION_JUDGE_SYSTEM)
from .schemas import (ACTION_SCHEMA, KEYSTROKE_PLAN_SCHEMA, ComputerOutput)


@dataclass
class ComputerTask:
    goal: str
    app_name: str                       # for AppleScript activate()
    bundle_id: str
    force_path: str | None = None       # deterministic | a11y | vision | electron
    electron_port: int | None = None    # Electron page path
    page_steps: list[dict] | None = None  # CDP steps for the electron path
    open_urls: list[str] | None = None
    launch_args: list[str] | None = None  # extra Electron/Chromium argv
    new_instance: bool = False
    pid_match: str | None = None         # argv substring to recover pid on slow launch
    max_turns: int = 12
    a11y_query: str | None = None       # pre-filter the AX markdown (§10 knob)
    verify_title_contains: str | None = None  # cheap post-condition via window title
    verify_equals: str | None = None    # exact result the run must produce (e.g. "223")
    trajectory_dir: str | None = None
    artifacts_dir: str | None = None
    extra: dict = field(default_factory=dict)


class ComputerSkill:
    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "computer", session: str | None = None,
                 text_provider: str = "cerebras",
                 vision_provider: str = "groq",
                 vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"):
        self.session = session or f"s10-{int(time.time())}"
        self.driver = Driver(session=self.session)
        self.client = V9Client(base_url=gateway_url, agent=agent_tag,
                               session=self.session)
        # Provider routing kept explicit so the skill is independent of
        # agent_routing.yaml: text judgment + keystroke planning go to a
        # healthy text model (Cerebras), vision goes to Groq's free
        # multimodal llama-4-scout (Gemini's free vision tier is
        # quota-exhausted). All calls still flow through the V9 gateway and
        # are tagged agent="computer" in the cost ledger.
        self.text_provider = text_provider
        self.vision_provider = vision_provider
        self.vision_model = vision_model
        self.vision_calls = 0
        self.llm_calls = 0

    # ── public entry ────────────────────────────────────────────────────
    async def run(self, task: ComputerTask) -> ComputerOutput:
        self.vision_calls = 0
        self.llm_calls = 0
        self.driver.ensure_daemon()
        if not self.driver.permissions_ok():
            return self._err(task, "precondition_blocked",
                             "Accessibility / Screen Recording not granted to "
                             "the CuaDriver bundle. Run `cua-driver permissions "
                             "grant`.")

        # The recording daemon resolves relative paths against ITS own cwd,
        # not ours — so turn folders silently land elsewhere. Make every
        # path absolute before handing it to the driver.
        if task.artifacts_dir:
            task.artifacts_dir = str(FsPath(task.artifacts_dir).resolve())
            FsPath(task.artifacts_dir).mkdir(parents=True, exist_ok=True)
        if task.trajectory_dir:
            task.trajectory_dir = str(FsPath(task.trajectory_dir).resolve())

        recording = False
        if task.trajectory_dir:
            FsPath(task.trajectory_dir).mkdir(parents=True, exist_ok=True)
            try:
                self.driver.start_recording(task.trajectory_dir)
                recording = True
            except DriverError:
                recording = False
        try:
            return await self._run_inner(task)
        finally:
            if recording:
                try:
                    self.driver.stop_recording()
                except DriverError:
                    pass

    async def _run_inner(self, task: ComputerTask) -> ComputerOutput:
        # ── launch + activate (handles the §8.2 background-launch trap) ──
        launched = self.driver.launch(
            bundle_id=task.bundle_id,
            electron_debugging_port=task.electron_port,
            urls=task.open_urls,
            additional_arguments=task.launch_args,
            new_instance=task.new_instance,
        )
        pid = launched.get("pid")
        if not pid and task.pid_match:
            # cua-driver's launch_app can time out waiting on NSWorkspace for
            # slow cold-starts (e.g. Chrome with a fresh profile) and return
            # an error instead of a pid, even though the app DID start.
            # Recover the main process pid by argv match.
            pid = self.driver.find_main_pid(task.pid_match)
        if not pid:
            return self._err(task, "no_target",
                             f"launch_app returned no pid for {task.bundle_id}")
        # Electron CDP needs a moment for the debugging port to come up.
        self.driver.activate(task.app_name,
                             settle=1.2 if task.electron_port else 0.6)

        win = self.driver.main_window(pid)
        window_id = win.get("window_id") if win else None

        # ── Electron page path (Layer-2 special case, §9) ───────────────
        if task.force_path == "electron" or task.electron_port:
            return await self._electron_page(task, pid, window_id)

        if not win:
            return self._err(task, "no_target", "no on-screen window after activate")

        # ── Layer 2a deterministic (§6) ─────────────────────────────────
        if task.force_path == "deterministic":
            return await self._deterministic(task, pid, window_id)

        # ── Layer 2b a11y, natural-escalate to Layer 3 vision ───────────
        if task.force_path in (None, "a11y"):
            out, escalate = await self._a11y(task, pid, window_id)
            if not escalate:
                return out
            # fall through to vision

        return await self._vision(task, pid, window_id)

    # ── Layer 2a: deterministic keystrokes ──────────────────────────────
    async def _deterministic(self, task, pid, window_id) -> ComputerOutput:
        """LLM plans the keystroke list once (cheap text call); the
        dispatch loop runs with NO LLM in it — the brief's Layer-2a
        discipline. Zero vision."""
        plan = await self.client.chat(
            f"GOAL: {task.goal}",
            system=PLAN_KEYS_SYSTEM,
            schema=KEYSTROKE_PLAN_SCHEMA, schema_name="plan",
            max_tokens=256, provider=self.text_provider,
        )
        self.llm_calls += 1
        keys = []
        if plan.parsed:
            keys = plan.parsed.get("keys") or []
        if not keys:
            try:
                keys = json.loads(plan.text).get("keys", [])
            except Exception:                              # noqa: BLE001
                keys = []
        if not keys:
            return self._err(task, "no_target",
                             "planner produced no keystroke sequence",
                             path="deterministic")

        # Focus + reset before dispatch. Layer-2a keystrokes only land if the
        # target window is frontmost; without this the first digits can be
        # dropped (focus still settling) and the leftover state corrupts the
        # result. Activate, then press Escape (Calculator's All-Clear) so the
        # run always starts from a known 0. (Fixed a wrong-result regression.)
        self.driver.activate(task.app_name, settle=0.6)
        try:
            self.driver.press_key(pid, "escape", window_id=window_id)
        except DriverError:
            pass
        time.sleep(0.2)

        actions: list[dict] = []
        for i, k in enumerate(keys, start=1):
            mapped = self._map_key(k)
            self.driver.press_key(pid, mapped["key"],
                                  modifiers=mapped.get("modifiers"),
                                  window_id=window_id)
            actions.append({"turn": i, "actions": [{"type": "press_key",
                            "key": mapped["key"]}], "outcome": "ok"})
            time.sleep(0.18)

        # verify (§7) via Layer-1 clipboard extract: copy the result with
        # Cmd+C and read it back. macOS Calculator exposes no result element
        # in its AX tree, so the clipboard is the cheap, reliable
        # post-condition — zero LLM, zero vision.
        time.sleep(0.3)
        self.driver.hotkey(pid, ["cmd", "c"])
        time.sleep(0.25)
        result_text = self.driver.clipboard()
        if not result_text:
            result_text = self._extract_result(pid, window_id, task.a11y_query)
        actions.append({"turn": len(keys) + 1,
                        "actions": [{"type": "hotkey", "keys": ["cmd", "c"]}],
                        "outcome": "copied result to clipboard"})
        # Success means the post-condition holds: when the task declares the
        # expected answer, compare it (digits only, so "4,021" vs "223" can't
        # both pass); otherwise fall back to "produced a numeric result".
        if task.verify_equals is not None:
            norm = lambda s: "".join(ch for ch in (s or "") if ch.isdigit() or ch in ".-")
            success = norm(result_text) == norm(task.verify_equals)
        else:
            success = bool(result_text)
        return ComputerOutput(
            goal=task.goal, app=task.app_name, path="deterministic",
            turns=len(keys), success=success,
            result_text=result_text,
            final_state=f"{len(keys)} keystrokes dispatched; result read "
                        f"from clipboard (Layer-1 extract): {result_text!r}"
                        + (f"; expected {task.verify_equals!r}"
                           if task.verify_equals is not None else ""),
            actions=actions, trajectory_dir=task.trajectory_dir,
            vision_calls=0, llm_calls=self.llm_calls,
            error=None if success else
            (f"result {result_text!r} != expected {task.verify_equals!r}"
             if task.verify_equals is not None else None),
        )

    @staticmethod
    def _map_key(k: str) -> dict:
        """Translate a planner key token to a cua-driver press_key spec.

        cua-driver's press_key vocabulary is named keys + letters + digits;
        most symbols work as literals EXCEPT `*` (no keyname), which is the
        shifted `8`, and `+`, which is the named key `plus`. The rest
        ("-", "/", ".", "=", "c") are accepted literally. (Empirically
        verified against macOS Calculator.)"""
        if k == "*":
            return {"key": "8", "modifiers": ["shift"]}
        if k == "+":
            return {"key": "plus"}
        special = {"return", "escape", "delete", "tab", "space",
                   "up", "down", "left", "right"}
        return {"key": k}  # digits, "-", "/", ".", "=", "c", specials

    def _extract_result(self, pid, window_id, query) -> str:
        """Layer-1 extract used as the verify step: pull the most likely
        result string out of the AX tree (Calculator surfaces it as a
        static text / the window has a value)."""
        try:
            state = self.driver.window_state(pid, window_id, mode="ax",
                                             guard_empty=False)
        except (PreconditionError, DriverError):
            return ""
        best = ""
        for e in state.get("elements", []):
            role = e.get("role", "")
            label = e.get("label")
            val = e.get("value")
            text = val if isinstance(val, str) and val else label
            if role in ("AXStaticText",) and isinstance(text, str):
                # Calculator's result is a numeric static text.
                cleaned = text.replace(",", "").replace(" ", "")
                if cleaned.replace(".", "").replace("-", "").isdigit():
                    best = text
        return best

    # ── Layer 2b: a11y judge loop ───────────────────────────────────────
    async def _a11y(self, task, pid, window_id) -> tuple[ComputerOutput, bool]:
        """Returns (output, should_escalate). Escalation climbs to vision."""
        actions: list[dict] = []
        for turn in range(1, task.max_turns + 1):
            try:
                state = self.driver.window_state(
                    pid, window_id, mode="ax", query=task.a11y_query)
            except PreconditionError as e:
                # empty tree → vision is the only path left
                return (self._err(task, "precondition_blocked", str(e),
                                  path="a11y"), True)
            md = state.get("tree_markdown") or self._render_md(state)
            prompt = (f"GOAL: {task.goal}\n\nAX TREE (markdown):\n{md[:9000]}")
            reply = await self.client.chat(
                prompt, system=A11Y_JUDGE_SYSTEM,
                schema=ACTION_SCHEMA, schema_name="action", max_tokens=400,
                provider=self.text_provider)
            self.llm_calls += 1
            decision = reply.parsed or self._loads(reply.text)
            verdict = (decision or {}).get("verdict", "escalate")
            thinking = (decision or {}).get("thinking", "")

            if verdict == "escalate":
                actions.append({"turn": turn, "actions": [], "outcome":
                                f"escalate: {(decision or {}).get('reason','')}"})
                out = ComputerOutput(
                    goal=task.goal, app=task.app_name, path="a11y",
                    turns=turn, success=False, actions=actions,
                    final_state="a11y escalated to vision",
                    vision_calls=0, llm_calls=self.llm_calls,
                    trajectory_dir=task.trajectory_dir)
                return out, True

            if verdict == "done":
                ok = bool((decision or {}).get("success"))
                ok = ok and self._verify(task, pid, window_id)
                actions.append({"turn": turn, "actions": [], "outcome":
                                f"done success={ok}"})
                return (ComputerOutput(
                    goal=task.goal, app=task.app_name, path="a11y",
                    turns=turn, success=ok,
                    result_text=self._extract_result(pid, window_id, task.a11y_query),
                    final_state=thinking, actions=actions,
                    vision_calls=0, llm_calls=self.llm_calls,
                    trajectory_dir=task.trajectory_dir), False)

            # verdict == act
            act = (decision or {}).get("action") or {}
            outcome = self._dispatch(pid, window_id, act)
            actions.append({"turn": turn, "actions": [act], "outcome": outcome})
            time.sleep(0.4)  # let the UI reflow before the next scan (§7)

        # ran out of turns without 'done'
        return (ComputerOutput(
            goal=task.goal, app=task.app_name, path="a11y",
            turns=task.max_turns,
            success=self._verify(task, pid, window_id),
            result_text=self._extract_result(pid, window_id, task.a11y_query),
            final_state="max turns reached", actions=actions,
            vision_calls=0, llm_calls=self.llm_calls,
            trajectory_dir=task.trajectory_dir), False)

    def _dispatch(self, pid, window_id, act: dict) -> str:
        t = act.get("type")
        try:
            if t == "click" and "element_index" in act:
                self.driver.click(pid, element_index=act["element_index"],
                                  window_id=window_id)
            elif t == "click" and "x" in act:
                self.driver.click(pid, x=act["x"], y=act["y"], window_id=window_id)
            elif t == "type_text":
                self.driver.type_text(pid, act.get("text", ""),
                                      element_index=act.get("element_index"),
                                      window_id=window_id)
            elif t == "press_key":
                self.driver.press_key(pid, act.get("key", "return"),
                                      modifiers=act.get("modifiers"),
                                      window_id=window_id)
            elif t == "hotkey":
                self.driver.hotkey(pid, act.get("keys", []), window_id=window_id)
            else:
                return f"unknown action {t}"
            return "ok"
        except DriverError as e:
            return f"error: {e}"

    # ── Layer 3: vision ─────────────────────────────────────────────────
    async def _vision(self, task, pid, window_id) -> ComputerOutput:
        """Set-of-marks vision: screenshot → overlay a numbered grid →
        the VLM picks the mark sitting on the target → click that mark's
        known pixel center. (Raw-coordinate regression is unreliable on the
        free-tier VLMs; see README failure modes.)"""
        from .marks import draw_grid_marks
        from .prompts import VISION_MARK_SYSTEM
        from .schemas import MARK_PICK_SCHEMA
        actions: list[dict] = []
        adir = FsPath(task.artifacts_dir or "/tmp")
        time.sleep(1.5)  # let the canvas finish painting before first capture
        for turn in range(1, max(2, task.max_turns // 3) + 1):
            shot = str(adir / f"{self.session}_vision_t{turn}.png")
            marked = str(adir / f"{self.session}_marks_t{turn}.png")
            self.driver.screenshot(pid, window_id, shot)
            try:
                centers, (W, H) = draw_grid_marks(shot, marked, cols=5, rows=4)
            except Exception as e:                         # noqa: BLE001
                return self._err(task, "vlm_unavailable",
                                 f"set-of-marks render failed: {e}", path="vision")
            data_url = self._data_url(marked)
            if not data_url:
                return self._err(task, "vlm_unavailable",
                                 "could not capture screenshot for vision",
                                 path="vision")
            try:
                reply = await self.client.vision(
                    data_url, f"GOAL: {task.goal}",
                    system=VISION_MARK_SYSTEM,
                    schema=MARK_PICK_SCHEMA, schema_name="pick", max_tokens=200,
                    provider=self.vision_provider, model=self.vision_model)
            except Exception as e:                         # noqa: BLE001
                return self._err(task, "vlm_unavailable",
                                 f"vision call failed: {e}", path="vision")
            self.vision_calls += 1
            self.llm_calls += 1
            decision = reply.parsed or self._loads(reply.text)
            mark = (decision or {}).get("mark")
            if mark not in centers:
                actions.append({"turn": turn, "actions": [],
                                "outcome": f"VLM returned invalid mark {mark!r}"})
                time.sleep(0.4)
                continue
            mx, my = centers[mark]
            # A pixel click only delivers to web content when the window is
            # frontmost; the VLM round-trip can take ~20s during which focus
            # drifts. Re-activate immediately before clicking (§8.2).
            self.driver.activate(task.app_name, settle=0.3)
            self.driver.click(pid, x=mx, y=my, window_id=window_id)
            actions.append({"turn": turn, "actions": [
                {"type": "click", "mark": mark, "x": mx, "y": my}],
                "outcome": f"clicked mark {mark} @({mx},{my}); "
                           f"{(decision or {}).get('thinking','')[:60]}"})
            time.sleep(0.7)
            if self._verify(task, pid, window_id):
                return ComputerOutput(
                    goal=task.goal, app=task.app_name, path="vision",
                    turns=turn, success=True,
                    final_state=f"post-condition met after clicking mark {mark} "
                                f"(set-of-marks on {W}x{H} screenshot)",
                    actions=actions, vision_calls=self.vision_calls,
                    llm_calls=self.llm_calls, trajectory_dir=task.trajectory_dir)

        return ComputerOutput(
            goal=task.goal, app=task.app_name, path="vision",
            turns=len(actions), success=self._verify(task, pid, window_id),
            final_state="vision turns exhausted without post-condition",
            actions=actions, vision_calls=self.vision_calls,
            llm_calls=self.llm_calls, trajectory_dir=task.trajectory_dir)

    # ── Electron page path (CDP) ────────────────────────────────────────
    async def _electron_page(self, task, pid, window_id) -> ComputerOutput:
        """Drive the Electron app's DOM through the CDP page path.

        cua-driver is the substrate: it launched the app with
        electron_debugging_port and records the run. Each `page_steps`
        entry is a DOM action addressed by CSS selector / JS. For
        `click_element` we resolve the element's on-screen rect via CDP and
        then issue a *visible* cua-driver `click` at those coordinates — so
        the agent-cursor overlay moves and the click lands in the recorded
        trajectory, rather than a silent DOM .click(). Zero vision."""
        actions: list[dict] = []
        result_text = ""
        via = set()
        # DOM rects come back in CSS (logical) pixels; cua-driver click(x,y)
        # expects window-local SCREENSHOT pixels, which are physical (DPR-
        # scaled). Compute the device-pixel ratio once so the visible click
        # lands on the element. (Classic coordinate-space trap.)
        dpr = 1.0
        try:
            sw, _ = self.driver.screenshot_dims(pid, window_id)
            lw = float((self.driver.main_window(pid) or {})
                       .get("bounds", {}).get("width", 0)) or sw
            if sw and lw:
                dpr = round(sw / lw)
                dpr = dpr if dpr >= 1 else 1.0
        except Exception:                                  # noqa: BLE001
            dpr = 2.0
        steps = task.page_steps or []
        for i, step in enumerate(steps, start=1):
            action = step.get("action")
            res = self.driver.page(
                pid, action, window_id=window_id,
                selector=step.get("selector"),
                css_selector=step.get("css_selector"),
                javascript=step.get("javascript"),
                attributes=step.get("attributes"))
            via.add(res.get("_via", "?"))

            if action == "click_element":
                rect = res.get("result")
                if isinstance(rect, dict) and "x" in rect:
                    px, py = rect["x"] * dpr, rect["y"] * dpr
                    self.driver.click(pid, x=px, y=py, window_id=window_id)
                    actions.append({"turn": i, "actions": [
                        {"type": "click", "selector": step.get("selector"),
                         "x": px, "y": py}],
                        "outcome": f"clicked {rect.get('text','')[:40]} "
                                   f"@({px:.0f},{py:.0f}) dpr={dpr}"})
                else:
                    actions.append({"turn": i, "actions": [step],
                                    "outcome": "element not found"})
            else:
                txt = self._page_result(res)
                if action in ("get_text", "execute_javascript", "query_dom"):
                    result_text = txt or result_text
                actions.append({"turn": i, "actions": [step],
                                "outcome": (txt or "ok")[:120]})
            time.sleep(0.5)

        ok = bool(result_text)
        if task.verify_title_contains:
            ok = task.verify_title_contains.lower() in (result_text or "").lower()
        return ComputerOutput(
            goal=task.goal, app=task.app_name, path="deterministic",
            turns=len(steps), success=ok, result_text=result_text,
            final_state=f"electron page path ({'/'.join(sorted(via))}): "
                        f"{len(steps)} DOM steps; result={result_text!r}",
            actions=actions, vision_calls=0, llm_calls=0,
            trajectory_dir=task.trajectory_dir,
            error=None if ok else "page steps produced no verifiable result")

    @staticmethod
    def _page_result(res: dict) -> str:
        for k in ("result", "text", "value", "output", "elements"):
            v = res.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if v is not None and not isinstance(v, str):
                return json.dumps(v)[:400]
        return ""

    # ── verify (§7) ─────────────────────────────────────────────────────
    def _verify(self, task: ComputerTask, pid: int, window_id: int) -> bool:
        """Cheap post-condition. When the task declares a title token, the
        target page/app sets its window title on success and we read it via
        list_windows — no extra LLM/vision call."""
        if not task.verify_title_contains:
            return True
        for w in self.driver.list_windows(pid):
            if task.verify_title_contains.lower() in (w.get("title") or "").lower():
                return True
        return False

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _data_url(png_path: str) -> str:
        import base64
        p = FsPath(png_path)
        if not p.exists() or p.stat().st_size == 0:
            return ""
        b = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/png;base64,{b}"

    @staticmethod
    def _loads(text: str) -> dict:
        try:
            return json.loads(text)
        except Exception:                                  # noqa: BLE001
            s, e = text.find("{"), text.rfind("}")
            if s >= 0 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except Exception:                          # noqa: BLE001
                    return {}
            return {}

    @staticmethod
    def _render_md(state: dict) -> str:
        lines = []
        for e in state.get("elements", []):
            lines.append(f"[element_index {e.get('element_index')}] "
                         f"{e.get('role')} {e.get('label')!r}")
        return "\n".join(lines)

    def _err(self, task, code, msg, *, path="blocked") -> ComputerOutput:
        return ComputerOutput(
            goal=task.goal, app=task.app_name, path=path, turns=0,
            success=False, error_code=code, error=msg,
            vision_calls=self.vision_calls, llm_calls=self.llm_calls,
            trajectory_dir=task.trajectory_dir)
