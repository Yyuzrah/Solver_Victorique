# Plan dependency assumptions

Initial plans and replans must declare compatible assumptions on every dependency
edge. The dependency's assumptions must be a subset of the consuming obligation's
assumptions. For example, a dependency with `["x is positive"]` can be used by an
obligation with `["x is positive", "y is integral"]`, but cannot be used by one
with an empty assumptions list.

This check uses exact strings. Reordering assumptions is allowed; paraphrasing an
assumption does not satisfy the check. The current protocol has no operation for
discharging assumptions across a dependency edge. Candidates must still declare
exactly the assumptions of their own obligation.

Validation first checks the existing route structure, including missing nodes,
cycles, depth and root closure. It then checks every edge within its own route,
including edges between intermediate obligations. An incompatible edge reports
the route, consumer obligation and dependency obligation. The existing candidate
integration check also remains in force.

The rule applies to plans created or read by this validator, including replay.
Historical runs with incompatible plans must continue using their original code
version: this validator rejects those stored plans, and does not migrate them.
