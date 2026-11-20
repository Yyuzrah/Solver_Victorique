# Codex reasoning effort per run

Use `victorique solve problem.md --reasoning-effort low --out NEW_DIRECTORY`
or `victorique.solve("problem.md", reasoning_effort="low", out="NEW_DIRECTORY")`.
The option is supported only with the Codex transport. Accepted request values
are `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; availability depends on
the selected model and client, as documented in the
[official OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Victorique does not silently replace a rejected effort level.

An explicit value adds one `--config model_reasoning_effort="low"` argument to
each fresh role invocation in that run. It does not add a model name, change
account configuration, or change a separately created reference-grading run.
Omitting the option keeps the existing CLI defaults and argv unchanged.

The requested value is recorded in `invocation.json` parameters, the frozen
transport profile in `state/run-config.json`, and the summary's optional
`requested_reasoning_effort` field. The transport also persists the profile in
its existing state. `cli_defaults` continues to describe the separately observed
CLI configuration; it is not relabeled as the requested or effective effort.
The recorded request does not prove the host applied it.

`resume` accepts no effort override and reuses the persisted transport profile,
budget, and absolute deadline. Old runs without the field retain their previous
behavior. A different requested effort requires an explicit new run.
