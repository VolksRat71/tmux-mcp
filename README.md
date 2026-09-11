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
