# Claude Code CLI transport

`ClaudeCliTransport` implements the existing `describe()`, `start_once(request)`,
and `observe_existing(request)` bridge interface. This opt-in adapter uses a
locally installed native Claude executable and the login the operator
completed in Claude Code. It adds no runtime dependencies.

## Log in with an Anthropic Console account

When Victorique uses Claude Code CLI, for solving (`--transport claude`) or for
the checks (`--check-transport claude`), log Claude Code in with an Anthropic
Console account: run `claude`, type `/login` and choose the Anthropic Console
account (API usage billing). Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
(checked 2026-11-19) says that the Claude.ai subscription login (Free, Pro,
Max, Ultra) is for ordinary use of Anthropic's own apps such as Claude Code,
that developers who build products should use an API key, and that third-party
developers may not offer a Claude.ai login in their apps or route requests
through Free, Pro, Max or Ultra credentials on behalf of users. The child
process environment below removes `ANTHROPIC_API_KEY` and the other provider
variables, so Claude Code runs with the login stored by `/login`; a key that is
only set in Victorique's environment is not used.

## Current validation

Development used synthetic public-help profiles and a bundled offline Python
executable. Tests exercise bridge admission, subprocess capture, concurrency,
timeouts, malformed responses, authorization, and restart behavior. Public
version/help and numeric argument parsing were also checked against Claude Code
2.1.321 without inference. The first real smoke attempt on 2026-11-13 launched
that native CLI in an external temporary directory. Its init event listed only
`StructuredOutput`, no MCP servers, and model `claude-opus-5-5`. The terminal
event returned `Not logged in · Please run /login`, with zero input/output
tokens. That attempt consumed one of the ten allocated process starts; no
replacement was sent. Successful inference and end-to-end schema compatibility
still await a logged-in CLI. An init model string identifies startup
configuration, not a successfully executed model turn.

## Connecting the adapter

The public commands accept `--transport claude`, and `solve` accepts
`--check-transport claude` for the two checks only:

```text
victorique solve examples/toy.md --transport claude --policy single --calls 3 --out runs/claude-toy
victorique solve examples/toy.md --transport codex --check-transport claude --out runs/claude-checks
victorique verify proof.md --problem problem.md --transport claude --calls 2 --out runs/claude-review
```

The Python APIs accept `transport="claude"` with the same parameters. The call
creates its authorization from the requested budget and deadline. Install the
native `claude` executable on PATH, or set `VICTORIQUE_CLAUDE_BINARY` to its
absolute path, and complete Claude's own login first. Required flags are checked
before a run directory is created; an unavailable flag is named in the error.
Claude role calls run serially across all three solving policies, also when
only the checks use Claude Code. A Claude call stopped at its per-call time
limit is sent again (see `timeout_retries` in the
[settings](usage.md#settings)); the session ID of the
stream's first event identifies the stopped call. Use `resume`
for a solve or `resume_verify()` for a review to observe the original requests.

An integrator can also construct the adapter directly and pass it to
`AdaptiveHarnessBridge` using the same session, output schemas and execution
grant as the existing Codex adapter:

```python
from victorique.harness.claude_transport import ClaudeCliTransport, AUTHORIZATION

# profile: recorded public --version and --help evidence for a native binary.
# authorization: the existing execution-authorization fields below.
# output_schemas: the role schemas already used by this run.
adapter = ClaudeCliTransport.create(
    transport_directory,
    profile=profile,
    authorization=authorization,
    output_schemas=output_schemas,
    timeout_seconds=120,
)
description = adapter.describe()
# bridge = AdaptiveHarnessBridge.create(..., description, ...)
# bridge.dispatch(request_id, adapter); bridge.recover(request_id, adapter)
```

Use authorization schema `victorique.claude_execution_authorization.v1`.
Its remaining fields match the current Codex authorization: `authorization_id`,
`source_ref`, `allow_execution`, `session_id`, `protocol`, `binary`,
`host_version`, `requested_model`, `max_process_starts`, `expires_at_utc`,
`permission_review_ref`, and `authentication_precondition`. The last must be
`operator_completed_host_managed_login`. A bridge execution grant uses
`adapter_id="claude_cli"` and `adapter_version="claude_cli_v1"`. The model string
`cli-default` leaves model selection to Claude; other values add `--model`.
Reported model and token usage appear under `parser` in the transport carrier.

`summary.json` and `calls.jsonl` include the model reported by Claude and its
reported input/output token counts. Cached reads map to `cached_input_tokens`;
the complete native usage object remains in the transport carrier. Missing
reported fields remain null.

Each request reserves its process start durably before launching. Reopening
the adapter reads the same capture. Duplicate starts do not launch again, even
after a crash or timeout. Expired authorization permits observation of existing
requests only. The process ceiling supplements the core's call budget.

## Isolation and CLI compatibility

Every role starts in its own empty system temporary directory outside the
current workspace and run state, with no resume or continuation argument. The
directory is removed when capture finishes; request state, schemas, and original
streams stay in the run directory. Reopening reads this state without recreating
a completed role directory or launching a replacement request.
The native binary must advertise the isolation flags in successful, complete
version/help probes; missing support raises an error before dispatch. For the
documented but sometimes hidden `--max-turns`, the profile can instead retain
three help-only parser probes: valid `1`, a deliberately invalid number, and an
unknown-option control. Acceptance requires the CLI's specific numeric error
for `--max-turns` as well as successful help for the valid value. Each probe is
bound to the observed executable and version. Plain `--version` or successful
unknown-option help does not establish support. Windows
shell wrappers such as `.cmd` are rejected. Flags are passed as an argument
list with `shell=False`.

The [official CLI reference](https://code.claude.com/docs/en/cli-reference)
documents the controls used here:

| Purpose | Arguments |
| --- | --- |
| New ephemeral role | `--print --no-session-persistence` |
| Result stream and schema | `--output-format stream-json --verbose --json-schema …` |
| Bounded agent loop | `--max-turns 1` |
| Customizations and settings | `--safe-mode --setting-sources "" --settings …` |
| No built-in or MCP tools | `--tools "" --disallowedTools "mcp__*"` |
| Empty MCP configuration | `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` |
| Skills and browser | `--disable-slash-commands --no-chrome` |

The [programmatic-use documentation](https://code.claude.com/docs/en/headless)
says `--bare` omits subscription credentials. This adapter uses safe mode to
retain normal login. Structured output is read from the result event's
`structured_output` member. Its original JSON byte slice is passed to the bridge;
the complete stdout/stderr bytes remain available in the local carrier. The
response's schema and exact request binding are checked before domain validation.
Claude's [structured-output documentation](https://code.claude.com/docs/en/agent-sdk/structured-outputs#recognize-the-structuredoutput-tool-call)
describes its internal `StructuredOutput` tool. The parser permits only that
tool and its matching result, and takes the final data from `structured_output`.

The fixed child environment preserves OS/login locations, including an explicitly
configured `CLAUDE_CONFIG_DIR`, and removes inherited API keys, provider routing,
agent settings and runtime injection variables. It never serializes environment
values or opens credential files. The [official environment reference](https://code.claude.com/docs/en/env-vars)
documents `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` for user/project/auto memory,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, background-task and cron disabling, file
mention disabling, and `CLAUDE_CODE_AUTO_CONNECT_IDE=0`. These are fixed child
values, so ancestor `CLAUDE.md` discovery is addressed independently of the
working directory. Tests assert these controls at the child process boundary.

The authorization's `permission_review_ref` must identify the operator's host
policy review: managed hooks must be absent or disabled by the administrator,
and managed settings must preserve this isolation configuration. The
[settings reference](https://code.claude.com/docs/en/settings-reference#disableallhooks)
explains that `disableAllHooks` supplied outside managed settings does not disable
managed hooks. The adapter supplies that setting for user/project/plugin hooks;
safe mode also retains administrator policy. Output parsing rejects observed
tools, MCP servers, subagents and hook events. This check occurs after startup
and does not replace the host policy review.

## Offline test command

```text
python -m pytest -q tests/test_claude_contract.py tests/test_claude_transport.py tests/test_claude_public.py
```

`ClaudeFixtureTransport` runs only the bundled inert executable. Its profile,
model name and output are synthetic; it does not check a real CLI login.
