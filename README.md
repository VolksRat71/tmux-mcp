# tmux-mcp

An MCP server that lets an AI agent drive tmux panes without tripping over
itself. Includes guarded peer messaging and optional shared local Qwen jobs.
The core requires only Python, `mcp` and tmux.

## Why this exists

Generic "send keys to tmux" tools make agents misbehave in three predictable
ways:

- They type a command, then send Enter separately, and run it twice.
- They read the pane before the output has arrived, decide the command did
  not run, and send it again.
- They cannot tell whether their keystrokes went to a shell or into the stdin
  of a program that is still running.

This server is designed around those failure modes. Every tool that types
returns evidence, waits are real waits, and a suspicious duplicate send is
refused.

## Tools

| Tool | Use it for |
|------|-----------|
| `tmux_message(text, target, wait_seconds=25)` | Peer Claude/Codex messages. Waits initially, then returns a queued ID while safe delivery continues automatically. |
| `tmux_message_status(message_id)`, `tmux_cancel_message(message_id)` | Inspect or request cancellation of a queued peer message. |
| `tmux_input_status(target)` | Read the reason a pane is protected. |
| `worker_submit(task, cwd, scope, owner, timeout_seconds=600)` | Submit bounded, read-only work to local Qwen; returns a job ID immediately. |
| `worker_status(job_id)`, `worker_cancel(job_id)` | Inspect or cancel a headless job, including its OpenCode session, timing and final report path. |
| `tmux_run(command, target)` | **Start here.** Types a shell command, presses Enter, and polls until the shell prompt is back and the screen has stopped changing, or until `timeout` seconds. Returns a status header plus the pane tail. |
| `tmux_wait_and_read(target, seconds, until_text)` | Wait for a pane to go idle, or for `until_text` to appear, with `seconds` as an upper bound. Returns as soon as either happens. |
| `tmux_send_text(text, target, enter=True)` | Type literal text into an interactive program such as a REPL or a prompt. |
| `tmux_send_keys(keys, target, enter=False)` | Control keys: `C-c`, `Escape`, `Up Up Enter`. Does not press Enter unless asked. |
| `tmux_read_pane(target, lines)` | Read the pane right now, without waiting. |
| `tmux_list_sessions`, `tmux_list_panes` | Discover targets. |
| `tmux_new_pane`, `tmux_kill_pane`, `tmux_select_pane`, `tmux_resize_pane` | Pane management. |

`target` is required on every tool that types. tmux's idea of the "current
pane" can be the pane the agent itself is running in.

### The status header

Every send and wait returns a first line like this:

```
[setup:0.0 | foreground: bash (idle) | sent + Enter | finished in 0.6s]
```

- `foreground` is the process in front of the pane and whether it is a shell
  (`idle`) or something else (`busy`).
- The outcome is one of `finished in Ns`, `still running after Ns`,
  `idle after Ns`, `matched '...' after Ns`, or `timed out after Ns`.
- If text was typed into a pane whose foreground was not a shell, a `NOTE:`
  line says so, because the text went to that program's stdin.

### Duplicate guard

If the identical text is sent to the same pane within 15 seconds while that
pane is still busy, the send is refused and the response points at
`tmux_wait_and_read`. Pass `force=True` to override. Idle panes accept
repeats freely.

### Human input protection

Agent panes are detected by session/foreground name, or the pane option
`@mcp_agent_kind` (`claude` / `codex` / `opencode`). All three legacy send tools also use
the guard; `force` cannot bypass human input protection. The guard waits up
to 25 seconds for a recognized empty composer, no question/dialog, three
seconds without client activity, and one second of stable input state. Unrelated
progress output does not restart the stability timer. It
rechecks under a shared per-pane process lock immediately before delivery.

An unfinished draft stays protected regardless of how long the user pauses.
Unknown layouts, copy mode, a stopped agent, an active question dialog, and
tracked question-tool calls block delivery. Ordinary conversation text does
not lock the input, including direct prose questions, quoted questions and
summaries of decisions in other panes. Use a structured question tool when an
answer needs protected input; an untracked question outside a recognized dialog
does not create a hold. The screen adapters currently recognize the observed
Claude and Codex layouts and Codex's `Ask Codex to do anything` placeholder.
Other placeholders/layouts may remain blocked until cleared or supported.

OpenCode is recognized and protected, but interactive delivery is currently
blocked: the adapter cannot verify which conversation tab is active. Empty
composer detection alone cannot prevent a message reaching the wrong session.
Use headless worker jobs for Qwen; these target a fresh, explicit session ID.

`tmux_message` retains the message in a private OS-temp queue if the initial
wait expires, returning `QUEUED` and a message ID. A detached worker keeps
checking through the same guard for up to one hour. It never sends on expiry.
The caller should check status, not resend. Repeated pending submissions reuse
the same job. Delivery stops if the pane or identified conversation changes.
Cancellation is a request: check status to see whether delivery already occurred.
Queued text is deleted from the job when it reaches a terminal state.

The low-level send tools and plain `guard.py` remain synchronous: `NOT SENT`
means nothing queued. Recent identical agent writes are refused across
processes for 15 seconds. An uncertain delivery error is not retried automatically.

`guard_hook.py` tracks `AskUserQuestion` (Claude) and `request_user_input`
(Codex, when exposed through tool hooks), matching completion by tool-call
ID. A hook must match its session ID to the resumed CLI process inside the
pane; inherited `TMUX_PANE` alone is insufficient because Codex's shared daemon
can pass it to other conversations. Unidentified fresh sessions retain screen
protection without a hook latch. SessionStart only clears its own pane's flag.
For Codex, an observer also watches for that exact call's terminal transcript
result, including failures such as "unavailable in Default mode" which may
skip PostToolUse. It never releases on inactivity or an unrelated call's result.
Observer errors are logged in the private temp directory. This parser follows
the locally verified Codex rollout format; unknown formats leave input protected.
Async question tools are not latched using immediate PostToolUse completion.

For Codex, merge [examples/codex-hooks.json](examples/codex-hooks.json) into
your project `.codex/hooks.json`, replace the example script path, and trust
the new entries through `/hooks`. Preserve any existing hooks. Claude Code
can use the same groups under `hooks` in `.claude/settings.json`; also copy
the question `PostToolUse` group to `PostToolUseFailure` to clear failed calls.

The hook also denies ordinary raw shell tmux writes and legacy MCP sends to
agent panes, directing callers to `tmux_message`. It is a cooperative
guardrail, not a sandbox: shell indirection and tools that bypass hooks can
still bypass protection. Read-only tmux commands remain allowed. Hook
definitions must be loaded/trusted in the client; editing server.py does
not update an already-running MCP process.

Until an MCP connection is reloaded, use the stdlib-only guarded CLI:

```sh
/usr/bin/python3 /absolute/path/to/tmux-mcp/guard.py %7 --check
/usr/bin/python3 /absolute/path/to/tmux-mcp/guard.py %7 --file /tmp/peer-message.txt --queue
```

Write the exact message to the UTF-8 file using a file tool, avoiding shell
interpolation. Label peer messages with their sender. Never submit a peer
message as the answer to a human question. A small race remains between the
final screen check and physical keyboard input: eliminating it would require
an input proxy, not polling. No changes to tmux keyboard bindings are made.

### Shared local Qwen jobs

Claude, Codex and their subagents can offload a bounded discovery, tracing,
review or log-analysis task when reading the source would cost substantially
more context than checking a compact report. Keep trivial lookups, already-loaded
context and high-level decisions with the orchestrator. One substantial task
usually works better than many tiny calls.

The optional runner uses the installed OpenCode V2 service and local
`llamaswap/qwen38-27b` model. It creates a fresh restricted `scout` session;
the title `qwen-worker:<owner> [<short-id>]` and session ID let you find the work
in OpenCode.
Status includes owner, scope, effective tools, elapsed time, revision/dirty
state, available token counts and final report path. It also exposes phase,
exploration steps, observed context and the reason exploration stopped.
Queue time and active elapsed time are reported separately.
No raw progress is returned
to the calling agent. It does not estimate premium tokens saved.

```sh
python3 worker.py submit <<'JSON'
{"task":"Trace guarded delivery and its retry behavior. Cite paths, symbols, line numbers, inspected tests and gaps in at most 40 lines.","cwd":"/absolute/path/to/tmux-mcp","scope":["guard.py","delivery.py","test_guard.py"],"owner":"codex:delivery-review","timeout_seconds":600}
JSON
python3 worker.py status qw-JOB_ID
python3 worker.py cancel qw-JOB_ID
```

The timeout includes time waiting for the existing shared GPU lock. Jobs never
reclaim another owner's lock. An active shared-service session also blocks a
new run, even if its CLI exited and its owner released the lock. Repeated
identical in-flight submissions reuse
the job when the bounded scope inventory still matches; completed reports are
not automatically cached or reused. Queued jobs fail if their scope changes
before execution. Check the
existing ID instead of resubmitting. Manual reuse requires checking relevant
content and file inventory, not just HEAD.

Exploration defaults to 8 provider steps, 120 seconds and a 16,000-token
observed context threshold. The first reached limit stops exploration. The
runner interrupts its exact session and verifies inactivity, then gives a
fresh session up to 60 seconds to write up a private evidence packet of at
most 12 KiB. That session has all tools denied. The requested write-up is
about 500 tokens; this is a prompt target, not a provider-enforced token cap.
The active-time allowance is exploration plus write-up; the existing timeout
still includes queue time and can end a job sooner.

Optional `worker_submit`/CLI JSON parameters are `max_steps=8`,
`max_context_tokens=16000`, `exploration_seconds=120` and `writeup_seconds=60`.
These are also the maximum values in this iteration; callers can lower them.
Increasing the ceilings requires a deliberate runner change and verification.
Steps count distinct completed provider turns. Observed context is the latest
input plus cache-read/cache-write token counts, excluding output; it is not
cumulative usage or the number of files read. A large result can overshoot the observed
context threshold before the runner stops it; it is not a strict input-token
ceiling. Keep starting inputs bounded.

A budget stop returns terminal `partial`, with its stop reason, evidence path
and report path. Successful native tool results are preserved as explicitly
bounded excerpts; progress narration is not evidence. If the write-up stalls,
the runner retains a deterministic evidence handoff instead of losing those
results. `partial` does not establish task completion. Inspect the gaps and
choose a narrower follow-up when useful; exhausted jobs are never extended
automatically. Cancellation, overall timeout and uncertain server cleanup
retain their existing behavior and precedence.

For large chat histories or logs, first identify matching files and extract
short relevant ranges into a scoped input file. Avoid giving the scout whole
JSONL transcripts with enormous records. Read-only scouting cannot prepare
those shell-derived inputs itself; the authorized orchestrator supplies them.

Sessions default-deny capabilities and allow native reads only within the
declared scope. Narrow scopes use `read`, which also lists directories.
Native `grep`/`glob` permissions match search patterns rather than paths, so
these tools require the whole Git-root scope with no escaping symlinks.
Symlink reads are denied. These are OpenCode permission rules, not an OS
sandbox. Shell commands, edits, test execution, delegation, web and MCP tools
are denied. Test review means reading tests or supplied results.

`completed` requires a final structured stop with nonempty text, successful
CLI exit and an inactive server session. If the CLI omits the final completion
event, the runner verifies the bounded authoritative session transcript,
including its completed assistant message and successful idle outcome.
It means a report was produced;
the orchestrator still checks its evidence and owns acceptance. Cancellation
interrupts the server session, since killing the CLI alone does not stop
inference. If server inactivity cannot be confirmed, status becomes
`cleanup_required` and retains the owned GPU lock for inspection.

This runner targets the locally inspected OpenCode V2 2.0.20 API/event protocol.
Unknown events or permission responses fail closed. Existing MCP connections
must reconnect to expose the new tools; the CLI is available immediately.
The model and shared GPU lock location are currently local defaults in
`worker.py`; configure those before using it on another machine.

### Server instructions

The server ships a short protocol in its MCP `instructions` field, so any
client that surfaces server instructions shows the agent how to use these
tools without extra prompting.

## Install

Requirements: Python 3.10+, tmux 3.x, and the `mcp` package.

```sh
git clone https://github.com/VolksRat71/tmux-mcp.git
pip install mcp
```

Register it with your MCP client. For Claude Code, add to `.mcp.json`:

```json
{
  "mcpServers": {
    "tmux": {
      "command": "python3",
      "args": ["/absolute/path/to/tmux-mcp/server.py"]
    }
  }
}
```

## Tests

The suite drives a real tmux server on a private socket (`-L tmux-mcp-test`),
so it never touches your own sessions.

```sh
pip install pytest
python3 -m pytest
```

## License

MIT. See [LICENSE](LICENSE).
