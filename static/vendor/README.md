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
