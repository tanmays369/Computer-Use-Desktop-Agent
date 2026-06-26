"""Thin, framework-free wrapper over the `cua-driver` CLI.

Every call shells out to `cua-driver call <tool> <json>` and parses the
JSON back. No SDK, no agentic framework — cua-driver is the substrate,
exactly as the Session-10 brief requires.

The wrapper adds only ergonomics the brief calls out as mandatory:

  - `ensure_daemon()`   — start `cua-driver serve` once per session (§4).
  - `activate()`        — the AppleScript foreground workaround for the
                          macOS background-launch trap (§8.2).
  - `main_window()`     — pick the real app window out of the menu-bar
                          windows `list_windows` also returns.
  - `window_state()`    — the scan half of scan-act-verify (§7), with the
                          empty-tree precondition guard (§8) raised as a
                          typed `PreconditionError`.

Nothing here interprets perception or plans actions — that is the
skill's job (the five layers above the driver, §10).
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any


class PreconditionError(RuntimeError):
    """Raised when the AX tree comes back empty — the four-causes trap
    from §8 (permissions / background launch / Qt env / Electron). The
    message lists every cause so a linear debugger doesn't mistake one
    for another."""


class DriverError(RuntimeError):
    """A cua-driver call returned a non-zero exit or an error payload."""


class Driver:
    def __init__(self, binary: str = "cua-driver", *, session: str | None = None):
        self.binary = binary
        self.session = session

    # ── core JSON transport ─────────────────────────────────────────────
    def call(self, tool: str, args: dict[str, Any] | None = None,
             *, timeout: float = 60.0) -> dict:
        payload = dict(args or {})
        # Thread the session id so the agent-cursor overlay + per-session
        # state follow the run across apps/windows (brief §11, §13).
        if self.session and tool not in ("status", "serve", "stop") \
                and "session" not in payload:
            payload["session"] = self.session
        proc = subprocess.run(
            [self.binary, "call", tool, json.dumps(payload)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise DriverError(
                f"{tool} exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[:500]}"
            )
        out = (proc.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            # Some tools (get_window_state, list_windows) return JSON; the
            # action tools (press_key, click, type_text, hotkey) return a
            # human "✅ Pressed 1 on pid …" line instead. Grab an embedded
            # JSON block if present, else wrap the human line so callers get
            # a uniform dict with an `ok` flag rather than an exception.
            start, end = out.find("{"), out.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(out[start:end + 1])
                except json.JSONDecodeError:
                    pass
            return {"_raw": out, "ok": out.startswith("✅") or "error" not in out.lower()}

    # ── daemon lifecycle (§4) ───────────────────────────────────────────
    def ensure_daemon(self) -> None:
        status = subprocess.run([self.binary, "status"],
                                capture_output=True, text=True)
        if status.returncode != 0:
            subprocess.Popen([self.binary, "serve"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            time.sleep(1.0)

    def permissions_ok(self) -> bool:
        try:
            p = subprocess.run([self.binary, "permissions", "status", "--json"],
                               capture_output=True, text=True, timeout=15)
            d = json.loads(p.stdout or "{}")
            return bool(d.get("accessibility")) and bool(d.get("screen_recording"))
        except Exception:                                  # noqa: BLE001
            return False

    # ── app lifecycle ───────────────────────────────────────────────────
    def launch(self, *, bundle_id: str | None = None, name: str | None = None,
               electron_debugging_port: int | None = None,
               urls: list[str] | None = None,
               additional_arguments: list[str] | None = None,
               new_instance: bool = False) -> dict:
        args: dict[str, Any] = {}
        if bundle_id:
            args["bundle_id"] = bundle_id
        if name:
            args["name"] = name
        if electron_debugging_port:
            args["electron_debugging_port"] = electron_debugging_port
        if urls:
            args["urls"] = urls
        if additional_arguments:
            args["additional_arguments"] = additional_arguments
        if new_instance:
            args["creates_new_application_instance"] = True
        return self.call("launch_app", args, timeout=40)

    def find_main_pid(self, match: str, *, timeout: float = 25.0) -> int | None:
        """Find the MAIN process pid whose argv contains `match` but not
        `--type=` (so we skip Chromium helper/renderer subprocesses). Used
        to recover from cua-driver's launch_app NSWorkspace timeout on slow
        cold-starts (Chrome with a fresh profile)."""
        import re
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ps = subprocess.run(["pgrep", "-f", match],
                                    capture_output=True, text=True, timeout=5)
                for spid in ps.stdout.split():
                    c = subprocess.run(["ps", "-o", "command=", "-p", spid],
                                       capture_output=True, text=True, timeout=5)
                    if match in c.stdout and "--type=" not in c.stdout:
                        return int(spid)
            except Exception:                              # noqa: BLE001
                pass
            time.sleep(1.0)
        return None

    def activate(self, app_name: str, *, settle: float = 0.6) -> None:
        """The §8.2 workaround: launch_app does not steal focus, so the
        first AX walk sees only the menu bar. AppleScript `activate`
        realises the main window; sleep lets the AX hierarchy build."""
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            check=False, capture_output=True,
        )
        time.sleep(settle)

    def list_windows(self, pid: int) -> list[dict]:
        d = self.call("list_windows", {"pid": pid})
        return d.get("windows", []) if isinstance(d, dict) else []

    def main_window(self, pid: int) -> dict | None:
        """Pick the real application window. `list_windows` also returns
        the full-width, 39-px menu-bar windows; the main window is the
        on-screen one with the largest area."""
        wins = self.list_windows(pid)
        onscreen = [w for w in wins if w.get("is_on_screen")]
        pool = onscreen or wins
        if not pool:
            return None
        def area(w: dict) -> float:
            b = w.get("bounds", {}) or {}
            return float(b.get("width", 0)) * float(b.get("height", 0))
        # Exclude the menu-bar strips (height ≈ 39, full screen width).
        candidates = [w for w in pool
                      if (w.get("bounds", {}) or {}).get("height", 0) > 60] or pool
        return max(candidates, key=area)

    # ── scan (§7) with precondition guard (§8) ──────────────────────────
    def window_state(self, pid: int, window_id: int, *,
                     mode: str = "ax", query: str | None = None,
                     max_elements: int | None = None,
                     guard_empty: bool = True) -> dict:
        args: dict[str, Any] = {"pid": pid, "window_id": window_id,
                                "capture_mode": mode}
        if query:
            args["query"] = query
        if max_elements:
            args["max_elements"] = max_elements
        state = self.call("get_window_state", args)
        if guard_empty and mode != "vision" and state.get("element_count", 0) == 0:
            raise PreconditionError(
                "cua-driver returned an empty AX tree (element_count=0). "
                "Check: (1) Accessibility + Screen Recording granted to the "
                "CuaDriver bundle, (2) app activated via AppleScript, "
                "(3) QT_ACCESSIBILITY=1 if Linux/Qt, (4) electron_debugging_port "
                "if this is an Electron app."
            )
        return state

    # ── act (§7) ────────────────────────────────────────────────────────
    def press_key(self, pid: int, key: str, *, modifiers: list[str] | None = None,
                  window_id: int | None = None) -> dict:
        args: dict[str, Any] = {"pid": pid, "key": key}
        if modifiers:
            args["modifiers"] = modifiers
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("press_key", args)

    def hotkey(self, pid: int, keys: list[str], *,
               window_id: int | None = None) -> dict:
        args: dict[str, Any] = {"pid": pid, "keys": keys}
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("hotkey", args)

    def type_text(self, pid: int, text: str, *,
                  element_index: int | None = None,
                  window_id: int | None = None) -> dict:
        args: dict[str, Any] = {"pid": pid, "text": text}
        if element_index is not None:
            args["element_index"] = element_index
        if window_id is not None:
            args["window_id"] = window_id
        return self.call("type_text", args)

    def click(self, pid: int, *, element_index: int | None = None,
              window_id: int | None = None,
              x: float | None = None, y: float | None = None) -> dict:
        args: dict[str, Any] = {"pid": pid}
        if element_index is not None:
            args["element_index"] = element_index
        if window_id is not None:
            args["window_id"] = window_id
        if x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self.call("click", args)

    def page(self, pid: int, action: str, *, window_id: int | None = None,
             selector: str | None = None, css_selector: str | None = None,
             javascript: str | None = None,
             attributes: list[str] | None = None,
             cua_timeout: float = 8.0) -> dict:
        """Drive an Electron/Chromium DOM via the CDP "page path".

        Tries cua-driver's own `page` tool first. On cua-driver 0.6.8 +
        recent Chromium builds that RPC stalls indefinitely (the websocket
        handshake hits Chromium's --remote-allow-origins gate and the tool
        does not time out), so after `cua_timeout` we fall back to a thin
        direct-CDP call that performs the SAME action over the SAME debug
        port. cua-driver still owns the substrate: it launched the app with
        electron_debugging_port and it records the run. The path actually
        used is returned in `_via`."""
        args: dict[str, Any] = {"pid": pid, "action": action}
        if window_id is not None:
            args["window_id"] = window_id
        if selector:
            args["selector"] = selector
        if css_selector:
            args["css_selector"] = css_selector
        if javascript:
            args["javascript"] = javascript
        if attributes:
            args["attributes"] = attributes
        try:
            res = self.call("page", args, timeout=cua_timeout)
            res["_via"] = "cua-driver:page"
            return res
        except subprocess.TimeoutExpired:
            pass
        except DriverError:
            pass
        out = self._cdp_page(pid, action, selector=selector,
                             css_selector=css_selector, javascript=javascript)
        out["_via"] = "cdp-fallback"
        return out

    # ── direct-CDP fallback for the page path ───────────────────────────
    def _cdp_port(self, pid: int) -> int | None:
        try:
            ps = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
            argv = ps.stdout
        except Exception:                                  # noqa: BLE001
            argv = ""
        import re
        m = re.search(r"--remote-debugging-port=(\d+)", argv)
        if m:
            return int(m.group(1))
        # The launched Electron parent may not carry the flag; scan siblings.
        try:
            ps = subprocess.run(["pgrep", "-f", "remote-debugging-port"],
                                capture_output=True, text=True, timeout=5)
            for spid in ps.stdout.split():
                c = subprocess.run(["ps", "-o", "command=", "-p", spid],
                                   capture_output=True, text=True, timeout=5)
                m = re.search(r"--remote-debugging-port=(\d+)", c.stdout)
                if m:
                    return int(m.group(1))
        except Exception:                                  # noqa: BLE001
            pass
        return None

    def _cdp_page(self, pid: int, action: str, *, selector=None,
                  css_selector=None, javascript=None) -> dict:
        import urllib.request
        port = self._cdp_port(pid)
        if not port:
            return {"error": "no remote-debugging-port found for pid", "ok": False}
        try:
            targets = json.loads(urllib.request.urlopen(
                f"http://localhost:{port}/json/list", timeout=5).read())
        except Exception as e:                             # noqa: BLE001
            return {"error": f"CDP list failed: {e}", "ok": False}
        pages = [t for t in targets if t.get("type") == "page"
                 and t.get("webSocketDebuggerUrl")]
        if not pages:
            return {"error": "no CDP page target", "ok": False}
        ws_url = pages[0]["webSocketDebuggerUrl"]
        if action == "execute_javascript":
            expr = javascript or "document.title"
        elif action == "get_text":
            expr = "document.body && document.body.innerText"
        elif action == "query_dom":
            sel = (css_selector or selector or "*").replace("'", "\\'")
            expr = (f"Array.from(document.querySelectorAll('{sel}'))"
                    f".slice(0,20).map(e=>({{tag:e.tagName,"
                    f"text:(e.innerText||'').slice(0,80),"
                    f"aria:e.getAttribute('aria-label')}}))")
        elif action == "click_element":
            sel = (selector or css_selector or "").replace("'", "\\'")
            # Return the element's on-screen rect so the caller can issue a
            # *visible* cua-driver click at those coordinates (recorded turn
            # + agent-cursor overlay), instead of a silent DOM .click().
            expr = (f"(function(){{var e=document.querySelector('{sel}');"
                    f"if(!e)return null;var r=e.getBoundingClientRect();"
                    f"return {{x:r.x+r.width/2,y:r.y+r.height/2,"
                    f"w:r.width,h:r.height,text:(e.innerText||'').slice(0,60)}};}})()")
        else:
            return {"error": f"unsupported action {action}", "ok": False}
        val = self._cdp_eval(ws_url, expr)
        return {"result": val, "ok": val is not None}

    @staticmethod
    def _cdp_eval(ws_url: str, expression: str):
        try:
            import websocket  # websocket-client
        except Exception:                                  # noqa: BLE001
            return None
        try:
            ws = websocket.create_connection(ws_url, timeout=10)
        except Exception:                                  # noqa: BLE001
            return None
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expression,
                                           "returnByValue": True,
                                           "awaitPromise": True}}))
            for _ in range(12):
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    res = msg.get("result", {}).get("result", {})
                    return res.get("value")
        except Exception:                                  # noqa: BLE001
            return None
        finally:
            ws.close()
        return None

    # ── screenshot for Layer 3 (§6) ─────────────────────────────────────
    def screenshot(self, pid: int, window_id: int, out_path: str) -> str:
        """Capture the window PNG via get_window_state(vision) and write
        it to `out_path`. Returns the path. The PNG bytes come back base64
        in the `screenshot`/`image` field depending on cua-driver build."""
        import base64
        state = self.call("get_window_state",
                          {"pid": pid, "window_id": window_id,
                           "capture_mode": "vision"})
        # cua-driver 0.6.x returns the PNG as base64 in `screenshot_png_b64`.
        b64 = (state.get("screenshot_png_b64") or state.get("screenshot")
               or state.get("image") or state.get("screenshot_base64") or "")
        if isinstance(b64, str) and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        if b64:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
        return out_path

    def screenshot_dims(self, pid: int, window_id: int) -> tuple[int, int]:
        """Return (width, height) of the vision-mode PNG in pixels. The
        screenshot is in physical pixels (DPR-scaled); vision coordinates
        and `click(x,y)` both use this same space."""
        state = self.call("get_window_state",
                          {"pid": pid, "window_id": window_id,
                           "capture_mode": "vision"})
        return int(state.get("screenshot_width", 0)), \
            int(state.get("screenshot_height", 0))

    # ── Layer 1 extract: clipboard (§6) ─────────────────────────────────
    def clipboard(self) -> str:
        """Read the macOS clipboard (pbpaste). The brief lists the
        clipboard as a Layer-1 extract source: copy the app's result with
        Cmd+C, then read it here — no click, no LLM."""
        try:
            p = subprocess.run(["pbpaste"], capture_output=True, text=True,
                               timeout=5)
            return (p.stdout or "").strip()
        except Exception:                                  # noqa: BLE001
            return ""

    # ── recording (§11) ─────────────────────────────────────────────────
    def start_recording(self, output_dir: str, *, video: bool = False) -> dict:
        return self.call("start_recording",
                         {"output_dir": output_dir, "record_video": video})

    def stop_recording(self) -> dict:
        return self.call("stop_recording", {})

    def get_recording_state(self) -> dict:
        return self.call("get_recording_state", {})
