"""
tmux-mcp — MCP server for interacting with tmux sessions.

Tools for listing sessions/panes, reading pane content, sending keys,
and waiting for output changes.
"""

import subprocess
import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tmux")


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a tmux command and return stdout."""
    result = subprocess.run(
        ["tmux"] + cmd,
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0 and result.stderr:
        return "ERROR: {}".format(result.stderr.strip())
    return result.stdout


@mcp.tool()
def tmux_list_sessions() -> str:
    """List all tmux sessions with window/pane counts."""
    return _run(["list-sessions"]) or "No tmux sessions found."


@mcp.tool()
def tmux_list_panes(session: str = "") -> str:
    """List all panes in a session (or all sessions if empty).

    Returns pane index, size, current command, and pane ID for each.
    """
    fmt = "#{session_name}:#{window_index}.#{pane_index}  #{pane_width}x#{pane_height}  #{pane_current_command}  #{pane_id}"
    if session:
        return _run(["list-panes", "-t", session, "-F", fmt])
    return _run(["list-panes", "-a", "-F", fmt])


@mcp.tool()
def tmux_read_pane(target: str = "", lines: int = 50) -> str:
    """Capture visible content from a tmux pane.

    Args:
        target: Pane target (e.g. "setup:0.0"). Empty = current pane.
        lines: Number of lines to capture from the bottom (default 50).
    """
    cmd = ["capture-pane", "-p", "-S", str(-lines)]
    if target:
        cmd += ["-t", target]
    return _run(cmd)


@mcp.tool()
def tmux_send_keys(keys: str, target: str = "", enter: bool = True) -> str:
    """Send keystrokes to a tmux pane.

    Args:
        keys: The text or key names to send.
        target: Pane target (e.g. "setup:0.0"). Empty = current pane.
        enter: Whether to send Enter after the keys (default True).
    """
    cmd = ["send-keys", "-t", target, keys] if target else ["send-keys", keys]
    result = _run(cmd)
    if enter:
        enter_cmd = ["send-keys", "-t", target, "Enter"] if target else ["send-keys", "Enter"]
        _run(enter_cmd)
    return result or "Keys sent."


@mcp.tool()
def tmux_send_text(text: str, target: str = "") -> str:
    """Send literal text to a tmux pane (no interpretation of special keys).

    Use this for sending prompts or commands that might contain special characters.

    Args:
        text: Literal text to type.
        target: Pane target (e.g. "setup:0.0"). Empty = current pane.
    """
    cmd = ["send-keys", "-l"]
    if target:
        cmd += ["-t", target]
    cmd.append(text)
    _run(cmd)
    # Send enter separately
    enter_cmd = ["send-keys"]
    if target:
        enter_cmd += ["-t", target]
    enter_cmd.append("Enter")
    _run(enter_cmd)
    return "Text sent."


@mcp.tool()
def tmux_wait_and_read(target: str = "", seconds: int = 5, lines: int = 50) -> str:
    """Wait for a specified duration then capture pane content.

    Useful for sending a command and then reading the result after it completes.

    Args:
        target: Pane target (e.g. "setup:0.0"). Empty = current pane.
        seconds: How long to wait before reading (default 5, max 120).
        lines: Number of lines to capture (default 50).
    """
    seconds = min(max(seconds, 1), 120)
    time.sleep(seconds)
    cmd = ["capture-pane", "-p", "-S", str(-lines)]
    if target:
        cmd += ["-t", target]
    return _run(cmd)


@mcp.tool()
def tmux_new_pane(target: str = "", vertical: bool = False, command: str = "") -> str:
    """Split a pane to create a new one.

    Args:
        target: Pane to split (e.g. "setup:0.0"). Empty = current pane.
        vertical: If True, split vertically. Default is horizontal.
        command: Optional command to run in the new pane.
    """
    cmd = ["split-window"]
    if vertical:
        cmd.append("-v")
    else:
        cmd.append("-h")
    if target:
        cmd += ["-t", target]
    if command:
        cmd.append(command)
    return _run(cmd) or "Pane created."


@mcp.tool()
def tmux_kill_pane(target: str) -> str:
    """Close a tmux pane.

    Args:
        target: Pane to kill (e.g. "setup:0.1").
    """
    return _run(["kill-pane", "-t", target]) or "Pane closed."


@mcp.tool()
def tmux_select_pane(target: str) -> str:
    """Focus a specific pane.

    Args:
        target: Pane to focus (e.g. "setup:0.1").
    """
    return _run(["select-pane", "-t", target]) or "Pane selected."


@mcp.tool()
def tmux_resize_pane(target: str, width: int = 0, height: int = 0) -> str:
    """Resize a tmux pane.

    Args:
        target: Pane to resize (e.g. "setup:0.0").
        width: Set absolute width in columns (0 = don't change).
        height: Set absolute height in rows (0 = don't change).
    """
    results = []
    if width > 0:
        results.append(_run(["resize-pane", "-t", target, "-x", str(width)]))
    if height > 0:
        results.append(_run(["resize-pane", "-t", target, "-y", str(height)]))
    return "\n".join(r for r in results if r) or "Pane resized."


if __name__ == "__main__":
    mcp.run(transport="stdio")
