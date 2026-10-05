# Shared Qwen Worker Implementation Plan

Spec: /Users/natertot/Downloads/qwen-shared-worker-design.md, approved by Nate's "Lets get to work on this".

Goal: opportunistic, bounded local delegation accessible to Claude, Codex and their subagents, with evidence-backed compact reports and retained orchestrator ownership.

Architecture: retain tmux message delivery as transport. Add a separate stdlib headless job runner around the installed OpenCode CLI, with thin MCP wrappers. No persistent new service, automatic semantic cache, code-editing worker, or new planner.

Constraints: preserve existing Claude/Codex guards and 4B worker. No raw pane sends. No PRs or changes to unrelated code. All model runs honor the existing GPU mkdir lock; never clear another owner's lock. Permissions constrain the worker, not the orchestrators. No claimed cost savings without measurement. Existing active scout must not be interrupted.

## Task 1: OpenCode input protection
Files: guard.py, guard_hook.py, test_guard.py, test_guard_hook.py; optional new adapter module/tests. No server.py edits.
- Add tests first: OpenCode detected by foreground/session; empty home/session composers; multiline/whitespace drafts; dialogs; busy; unknown layout; changed conversation identity.
- Implement explicit OpenCode kind and conservative adapter based on observed TUI; register unknown OpenCode states as protected rather than falling through as shell.
- Preserve existing protections and final-sample checking. Stable session targeting must not claim support where it cannot be verified; block interactive queued delivery if session identity is ambiguous.
- Verify targeted tests on a private tmux socket only. Root runs combined suite.

## Task 2: Headless worker lifecycle
Files: worker.py, test_worker.py, optional worker_events.py and corresponding tests. No guard/server/docs/config edits.
API: submit(task: str, cwd: str, scope: list[str], owner: str, timeout_seconds: int=600) -> dict; status(job_id: str) -> dict; cancel(job_id: str) -> dict. CLI accepts JSON submit on stdin plus status/cancel subcommands. Read-only scout only.
- Tests first with fake subprocesses and private lock paths: successful structured completion; progress-only output; nonzero exit; malformed/truncated events; cancellation; timeout; occupied GPU lock; in-flight dedupe; stale/missing worker; scope traversal; output limits.
- Detached jobs under a private per-user state dir. Atomic job metadata, per-job locks, globally serialized inference using shared GPU lock. Bound queue/run time, process-tree cleanup, never reclaim foreign/stale lock automatically.
- Invoke installed opencode run --agent scout --format json in the requested cwd with isolated explicit read-only agent configuration. Use argv not shell interpolation. Fresh per-job session; no automatic continue/fork; no default cloud fallback. Keep inherited MCP capabilities out of scout.
- Require valid final structured completion, nonempty bounded final text, successful exit and no protocol error before completed. Publish final report atomically. Progress/raw events stay out of status; bounded diagnostics only.
- Store owner/task/cwd/scope, revision and dirty state, run/config identity, times, output bytes and available token counts. Evidence validation is orchestrator responsibility; completed is not accepted. No inferred savings.
- In-flight duplicates reuse job. Completed reports are discoverable by ID, but never automatically reused without freshness verification. Explicit manual reuse guidance first; no expensive whole-repo hash on each submission.
- Tests must exercise actual detached runner with fake CLI, not just mock helper functions. GPU path overridden only in tests.

## Task 3: Guidance and permissions
Files: .claude/skills/coordinating-claude-and-codex/SKILL.md, .claude/CLAUDE.md, .opencode/AGENTS.md, .opencode/opencode.json. Portable scout config example in tmux-mcp/examples if useful.
- Preserve primary orchestrators and their own subagents. Add coarse-grained routing decision, bounded task/report contract, avoid trivial/already-loaded/judgment-heavy tasks.
- Scout default read-only: native read/grep/glob/list only, deny bash/MCP/edit/task/web by default. Preserve other agents/providers and 4B worker exactly. History/command-dependent tasks may have orchestrator-provided evidence initially, no unsafe shell allowlist.
- Narrow KERV browser instructions to explicit UI tasks, not docs/repo research.
- Align CLI/MCP names with Task 2. No fake cache or telemetry capabilities in docs. Skill scenario-test the routing and report contract.

## Task 4: Integration and verification (root)
Files: server.py, README.md, integration tests as needed.
- Expose worker_submit(task,cwd,scope,owner,timeout_seconds=600), worker_status(job_id), worker_cancel(job_id); concise summaries and report path only, separate from tmux message IDs.
- Run full pytest suite and MCP initialize/tools-list/call on new subprocess. Verify scout configuration permissions.
- Independently review changes, fix meaningful issues, run covering tests.
- Once GPU free, one bounded real read-only trial; validate cited evidence and final lifecycle. If another run retains lock, report live verification pending instead of disrupting it.
- Scan staged diffs for secrets; commit/push existing feature branches. No PR. Report actual activation requirements and verification limits.

## Review focus
- No false completion from progress text, permission denial, early process exit, truncation or token/step exhaustion.
- Cancellation cannot leave a model process alive while releasing the GPU lock.
- Parent config inheritance cannot give scout browser/terminal mutation permissions.
- Repeated requests cannot create duplicate queued work or consume a stale cached report silently.
- OpenCode unknown/session-switched screens must not bypass input protection.

## Implementation and validation record

All four tasks implemented. OpenCode interactive delivery remains deliberately
blocked because active-tab conversation identity cannot be verified; headless
jobs use explicit fresh session IDs. The installed shared V2 service requires
per-session permissions rather than per-process configuration overrides.

- Full suite: 137 passed in 58.51 seconds, private tmux socket and fake-worker
  inference only. Independent static reviews found no remaining high-confidence
  defects after scope freshness and queued-symlink fixes.
- Real MCP initialize, tool discovery and invalid submit/status checks passed.
- Real empty-session API checks confirmed exact permissions, inactivity and
  interruption without inference.
- First live trial exposed an OpenCode 2.0.20 CLI race that omits final
  step_finish during transcript replay. Regression coverage and strict
  authoritative-transcript verification now handle it. The failed job remains
  historically failed; no report was falsely published.
- Fresh live job qw-176a48f37a0742cc88e114cb completed in 47.691 seconds using
  session ses_ef210ae4affewOiI2KcyDpRqTT. Its report correctly identified the three
  worker tools at README.md:29-30 and the active-tab limitation at :84-87.
  Owner accepted these findings after checking the citations. The requested
  eight-line format was exceeded, but output remained within the worker limit;
  natural-language brevity remains an acceptance check, not a guaranteed format.
- Server active-session list was empty and the GPU lock absent after completion.
  The worker now also rejects foreign active sessions before inference because
  killing a CLI does not stop shared-service inference.
- Shared configuration can hot-reload into an active session. This affected the
  earlier scout's remaining shell calls; Claude was notified and stopped its own
  session. Guidance now defers shared profile changes until affected jobs finish.
- Existing 4B worker and all non-scout OpenCode configuration preserved. The
  local .opencode files are outside Git; shared guidance is in the .claude repo.

Activation: reconnect existing tmux MCP clients to discover worker tools.
The worker.py CLI works immediately. No PR created; orchestrators retain
acceptance and decisions. Code-editing/test-executing worker profiles remain
deferred as designed.
