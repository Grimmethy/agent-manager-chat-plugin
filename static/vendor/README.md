# Vendored: streaming-markdown

`smd.js` is vendored verbatim (unminified, for readability/debuggability) from
https://github.com/thetarnav/streaming-markdown, commit `6beb92d991ed`, MIT license (own
header carries the copyright notice -- see the top of the file).

Chosen for `chat.js`'s markdown rendering (2026-09-22, Grimmethy: "check github for
something that handles this issue well" re: unreadable/unformatted assistant responses in
the chat sidebar) because it's dependency-free, has no build step (drop the file in and
`import` it), and its `parser_write`/`parser_end` API matches this repo's existing SSE
chunk-accumulation loop in `chatSend()` almost exactly.

Not pulled via npm/a package manager: this repo has no JS build tooling at all (`chat.js`
is served as a plain static file), so vendoring the single file is the same pattern
`chat.css`/`chat.js` themselves already use.

# Vendored: dockview-core

`dockview.js` is dockview-core 8.4.1 (zero-dependency layout manager for tabs, groups, grids and split views), MIT license, https://github.com/dockview/dockview.

- **Source:** `dist/dockview-core.min.js` from the npm package `dockview-core@8.4.1`, fetched 2026-10-24 from https://registry.npmjs.org/dockview-core/-/dockview-core-8.4.1.tgz after the owner approved the download in chat (the multi-chat panes hub, HUB0003 piece 5, was owner-gated). The tarball's integrity (`sha512-LcvhGwmK/Q1tFDEPkiBpkLIYctimmu8JJHgG5yhIXwyf9IvWQkJa3EG7/a1NKUS2Q8w82uoYZTL+c84/zpQUjg==`) matched the registry's before extraction.
- **What is in the file:** the build's own bytes (sha256 of those bytes `272fe5e64b3cb07d6ac70a01c4fa5c97460b3710ecb33526ec95fb6bc409fa46`) preceded by a notice that carries the full MIT license text and copyright line. The minified build already embeds its stylesheet and injects it when loaded in a page, so there is no separate `dockview.css`.
- **How it loads:** `templates/chat.html` includes it as a plain `<script src="static/vendor/dockview.js">`. It is a UMD build, so in a browser it sets `globalThis['dockview-core']` (note the hyphen); it exports `createDockview`, `DockviewComponent` and about a hundred other names.
- **Known gap:** `static/multi-chat.js` looks for `window.DockviewGrid` (a constructor taking a host element) and falls back to its own simple panes when that is not a function. Nothing in this repository defines `DockviewGrid` and dockview-core does not export one, so the dockview path stays inactive until a small adapter that wraps `createDockview` is written.
- **Why minified:** unlike `smd.js` (kept unminified for debugging), the unminified build is about 967 KB against 541 KB here; the unminified file can be swapped in from the same npm package version.
