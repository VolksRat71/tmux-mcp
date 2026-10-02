# tmux-mcp

An MCP server that lets an AI agent drive tmux panes without tripping over
itself. Single Python file, no dependencies beyond `mcp` and tmux.

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
| `tmux_message(text, target, wait_seconds=25)` | Peer Claude/Codex messages. Automatically waits for a safe empty composer; never overrides human input. |
| `tmux_input_status(target)` | Read the reason a pane is protected. |
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

### Human input protection (Claude and Codex)

Agent panes are detected by session/foreground name, or the pane option
`@mcp_agent_kind` (`claude` / `codex`). All three legacy send tools also use
the guard; `force` cannot bypass human input protection. The guard waits up
to 25 seconds for a recognized empty composer, no question/dialog, three
seconds without client activity, and one second of stable screen state. It
rechecks under a shared per-pane process lock immediately before delivery.

An unfinished draft stays protected regardless of how long the user pauses.
Unknown layouts, copy mode, a stopped agent, and possible unanswered prose
questions block delivery. Only the latest visible assistant response is
checked for prose questions; this is conservative heuristic detection, not
semantic certainty. The screen adapters currently recognize the observed
Claude and Codex layouts and Codex's `Ask Codex to do anything` placeholder.
Other placeholders/layouts may remain blocked until cleared or supported.

`NOT SENT` means nothing is queued for later delivery. The caller can retry
through the guard after the interaction completes. Recent identical agent
messages are refused across processes for 15 seconds. Attempt fingerprints
and locks live in the OS temp directory; message contents are not stored.
An uncertain delivery error requires inspecting the pane before retrying.

`guard_hook.py` tracks `AskUserQuestion` (Claude) and `request_user_input`
(Codex, when exposed through tool hooks), matching completion by tool-call
ID. It needs `TMUX_PANE` in the hook environment. Screen detection still
applies when hooks are absent. SessionStart clears a leftover question flag.
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
/usr/bin/python3 /absolute/path/to/tmux-mcp/guard.py %7 --file /tmp/peer-message.txt
```

Write the exact message to the UTF-8 file using a file tool, avoiding shell
interpolation. Label peer messages with their sender. Never submit a peer
message as the answer to a human question. A small race remains between the
final screen check and physical keyboard input: eliminating it would require
an input proxy, not polling. No changes to tmux keyboard bindings are made.

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
