# Manual Verification – Multi-Chat Side-by-Side Panes (Phase 2)

> One-off checklist. Work through each section top-to-bottom in a clean
> browser profile (or with localStorage cleared) before moving on.

## Prerequisites

- [ ] `static/vendor/dockview.js` exists on disk (or is served by the dev server) – confirm via the file system or a direct URL fetch of `/static/vendor/dockview.js`.
- [ ] Open DevTools → Network tab, reload the chat page, and confirm `multi-chat.js` appears in the request list with status 200.
- [ ] The session switcher shows **at least two** active sessions (create a second one if needed) so that multi-pane layouts have content to display.

## Layout presets

- [ ] On first load (no saved layout) the UI shows a **single pane** (the default preset).
- [ ] Switch to the **2-column** preset: two live panes appear side by side, each rendering its own session transcript.
- [ ] Switch to the **2×2** preset: four panes are visible in a 2-by-2 grid, each independently scrollable.
- [ ] Switch back to the **1-pane** preset: the UI collapses to a single full-width pane again.

## Save / Restore

- [ ] With a multi-pane layout active, **reload the page**. The same panes and the same preset are restored.
- [ ] Open DevTools → Application → Local Storage and confirm the key `chat-workspace-layout` is present and holds a JSON object describing the current layout.
- [ ] Clear `chat-workspace-layout` (or clear all localStorage), then reload. The UI falls back to the **single-pane** default.

## Close does not end session

- [ ] In a 2-column layout, **close one pane** (use the pane's close control, not the session switcher).
- [ ] The closed session is **still listed** in the session switcher.
- [ ] Re-open that session from the switcher: its **full history is intact** (no messages lost, no "new conversation" reset).

## No cross-pane interference

- [ ] In a 2-column layout, send a message in **Pane A**.
- [ ] Pane B's transcript is **unchanged** (no new messages, no re-render of existing content).
- [ ] If Pane B has an **in-flight stream** (e.g., a long model response), that stream **continues uninterrupted** after the send in Pane A.

## Edge cases

- [ ] Open the **same session** in two different panes; verify both panes display the identical transcript and that sending in one does not corrupt the other.
- [ ] **Resize the browser window** while in a multi-pane layout; panes reflow without overlapping, clipping, or losing content.
- [ ] **Rapidly toggle** between presets (1 → 2-column → 2×2 → 1) several times in quick succession; the final state is stable and matches the last preset selected.
- [ ] At a **viewport width < 760 px** (the phone-width breakpoint), the layout collapses to a **single group** and the session switcher remains visible and functional.
