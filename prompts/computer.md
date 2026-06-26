# Computer skill

Drives real desktop applications through a four-layer cascade on top of
`cua-driver` (extract → deterministic → a11y → vision), with a
`permissions` precondition. Same shape as the Session-9 Browser skill,
different substrate.

The skill does NOT go through the standard LLM-call dispatch. Like
Browser, it owns its own cascade and routes each layer's gateway call
internally, so the orchestrator hands it a `NodeSpec` and gets back a
`ComputerOutput` with the chosen layer surfaced as `output.path`.

Inputs (in `metadata`):
- `goal` (required): natural-language description of the desktop task.
- `app_name` + `bundle_id` (required): the target application.
- `force_path` (optional): `deterministic` | `a11y` | `vision` | `electron`
  to pin a layer; omit to let the cascade choose.
- `electron_port` / `page_steps` (optional): drive an Electron app's DOM
  through the CDP page path.
- `verify_title_contains` (optional): cheap post-condition via window title.

Use when the task is on the desktop rather than the web: a native app
(Calculator, Notes, Settings), an Electron app (VS Code, Slack, Cursor),
or a canvas/game surface that only vision can read.
