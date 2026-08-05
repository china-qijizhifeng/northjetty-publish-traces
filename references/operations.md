# NorthJetty trace-site operations

## Prerequisites

- Run on a machine or pod that NorthJetty can reach and that will remain alive.
- Obtain a publisher API key from the NorthJetty administrator. Do not copy another user's key.
- Keep the generated site outside the installed skill directory.
- Supply PyTorch/Chrome traces at runtime only. The skill intentionally ships with zero traces.

## Build an empty viewer

An empty viewer is valid and still accepts a local trace through drag-and-drop:

```bash
python3 scripts/build_trace_site.py \
  --output /absolute/path/to/trace-site \
  --title "Team Torch Trace Viewer"
```

## Build with traces

Each group source may be a file or directory. Directory discovery is recursive.

```bash
python3 scripts/build_trace_site.py \
  --output /absolute/path/to/trace-site \
  --title "Model X traces" \
  --group prefill=/absolute/path/to/prefill-traces \
  --group decode=/absolute/path/to/decode-traces \
  --group-label "prefill=Prefill" \
  --group-label "decode=Decode"
```

Default directory patterns are `*.trace.json` and `*.trace.json.gz`. Use repeated
`--pattern` arguments for different names. Generated chunks are at most 8,000,000
bytes so they remain below the current NorthJetty edge-response limit.

The builder replaces only an empty output directory or a directory carrying its
`.northjetty-trace-site` marker. It refuses broad, unrelated, or in-skill output paths.

## Validate

```bash
python3 scripts/validate_trace_site.py /absolute/path/to/trace-site
```

Zero groups and zero traces are a successful validation result.

## Publish

Publishing creates external state. Do it only when the user explicitly requests a
live route. `start` runs in the background by default.

```bash
python3 scripts/nj-publish.py start /absolute/path/to/trace-site \
  --alias team-torch-trace \
  --require-auth
```

If `NORTHJETTY_API_KEY` is absent, the client prompts without echoing it. The command
prints a public URL and a distinct route access code. The API key authorizes publishing;
the route access code authorizes viewers. Treat both as secrets and never place either
in skill files, source control, shell command arguments, or the final response.

## Manage

```bash
python3 scripts/nj-publish.py list
python3 scripts/nj-publish.py logs team-torch-trace
python3 scripts/nj-publish.py stop team-torch-trace
```

Rebuilding the same generated directory updates a running static route after refresh.
For large sites, finish the atomic rebuild before asking viewers to refresh.

## Security and capacity

- `--require-auth` is the default recommendation for traces.
- The viewer loads `https://ui.perfetto.dev` in an iframe and transfers the selected
  trace buffer to that iframe in the browser. Self-host Perfetto for highly sensitive data.
- Chunking avoids the edge response-size limit; it does not reduce browser memory.
  The browser joins every part into one buffer before Perfetto parses it.
- The publisher process and its host/pod must remain alive. A dead pod produces a dead route.
