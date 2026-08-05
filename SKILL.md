---
name: northjetty-publish-traces
description: Build a zero-data or runtime-populated browser viewer for PyTorch profiler and Chrome trace files, split large traces into NorthJetty-safe chunks, validate the generated static site, and optionally publish or manage an authenticated NorthJetty route backed by Perfetto. Use when Codex needs to initialize an empty trace viewer, share `.trace.json` or `.trace.json.gz` files, create a trace manifest grouped by scenario or rank, reproduce the team's NorthJetty Torch Trace workflow, or start, inspect, update, or stop a NorthJetty trace site.
---

# NorthJetty trace publishing

Create a portable static trace viewer without bundling trace data. Treat traces as runtime inputs and keep every generated site outside this skill directory.

## Non-negotiable rules

- Never copy a trace, generated chunk, manifest containing trace entries, API key, or route access code into this skill.
- Accept zero traces as a complete, valid result. Do not search for or invent sample traces merely to populate a new environment.
- Build only into a user-scoped absolute output path. The builder refuses unrelated non-empty directories and paths inside the skill.
- Validate locally before publishing.
- Create, replace, or delete a NorthJetty route only when the user explicitly requests a live-state change.
- Use `--require-auth` unless the user explicitly requests a public route and understands the exposure.
- Never repeat secrets in the final response. The publisher API key and viewer access code are different credentials.

## Resolve bundled tools

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`, then use:

- `scripts/build_trace_site.py` to initialize or rebuild a site.
- `scripts/validate_trace_site.py` to check its manifest and all chunks.
- `scripts/nj-publish.py` with sibling `scripts/njsite.py` to manage NorthJetty.
- `assets/index.html` as the generic zero-data Viewer template.

Read [references/operations.md](references/operations.md) before publishing, changing authentication, handling a large trace, or diagnosing a dead route.

## Build workflow

1. Resolve the requested output directory and trace sources. If no trace source was supplied, build an empty viewer without asking for placeholder data.
2. For trace-backed builds, inspect only the supplied paths. Prefer `.trace.json.gz`; raw `.trace.json` also works.
3. Build the site:

```bash
python3 "$SKILL_DIR/scripts/build_trace_site.py" \
  --output /absolute/path/to/trace-site \
  --title "Team Torch Trace Viewer"
```

Add one or more runtime groups when traces exist:

```bash
python3 "$SKILL_DIR/scripts/build_trace_site.py" \
  --output /absolute/path/to/trace-site \
  --title "Model traces" \
  --group prefill=/absolute/path/to/prefill \
  --group decode=/absolute/path/to/decode \
  --group-label "prefill=Prefill" \
  --group-label "decode=Decode"
```

4. Validate every build:

```bash
python3 "$SKILL_DIR/scripts/validate_trace_site.py" /absolute/path/to/trace-site
```

Treat `Groups: 0`, `Traces: 0`, and `Validation: OK` as success for a clean environment.

## Publish workflow

Publish only after local validation and explicit user authorization:

```bash
python3 "$SKILL_DIR/scripts/nj-publish.py" start \
  /absolute/path/to/trace-site \
  --alias team-torch-trace \
  --require-auth
```

Let the client securely prompt for `NORTHJETTY_API_KEY`; do not put the key in a command or file. `start` backgrounds itself. Preserve the printed public URL, but tell the user to retrieve and share the route access code through the local publisher output or log rather than reproducing it in chat.

Verify the route state:

```bash
python3 "$SKILL_DIR/scripts/nj-publish.py" list
```

For updates, rebuild the same generated output and re-run validation; the static server serves refreshed files without route recreation. For lifecycle work, use `logs <alias>` and `stop <alias>` only within the user's requested scope.

## Handoff

Report:

- generated site path;
- whether the site is empty or the exact group/trace counts;
- local validation result;
- route alias and public URL when published;
- that the access code remains secret and where the user can retrieve it;
- the remote Perfetto dependency when trace confidentiality matters.
