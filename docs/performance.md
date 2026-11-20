# Session transaction performance

Every session operation runs in one SQLite transaction that starts with
`BEGIN IMMEDIATE`. A run's loop and its bridge each hold an `AdaptiveSession`
object for the same database.

## What each transaction checks

The first transaction of an object runs the full replay: `open`, `create` on an
existing database, and every newly opened loop or bridge, so `resume`, `status`
and `export` as well. It applies every event from the first one, checks the
`requests`, `packets`, `observations`, `charges` and `endpoint` tables against
the replayed state, replays the domain journal, and compares `session_cache` and
`session_endpoint` with the projection (the domain state derived from the
journal). The object then keeps the state it verified and the stored rows it
compared.

A later transaction of the same object:

- compares directly with what the object holds: the schema rows in
  `sqlite_master` (the tables, and the triggers that refuse UPDATE and DELETE on
  every table except `requests` and `session_cache`), `user_version`, the
  metadata rows, and the stored rows of the last verified event and of the last
  verified journal entry;
- requires the event rows and the journal rows to be numbered from 1 to their
  row count;
- applies only the events after the last verified one, and derives only the
  journal entries after the last verified one, from the base states of those
  events;
- compares the rows these events added to `packets`, `observations`, `charges`
  and `endpoint` with the state, and the row counts of these tables;
- compares every `requests` row with the state, and `session_cache` and
  `session_endpoint` with the projection.

Events and journal entries that another object or process appended are applied
in the same way. Any other difference runs the full replay, and its result
decides: a changed database fails, and a row that holds the same
content in another JSON form passes. Rows of the protected tables before the
last verified ones are compared again by the next full replay (open, resume,
status or export). The checks use sequence numbers, row counts and direct
comparison of stored rows.

Each transaction checks the session root and each of its parents once, and the
database file and its three SQLite sidecar files once each. The bridge reads all
its jobs in one query for its status.

## Measurements

Windows 10, Ryzen 9 5900X (12 cores), Python 3.11.9, SQLite 3.45.1, 2026-11-19.
"Before" uses the session transaction code of version 0.1.0b2, which replays
every event in every transaction; "after" is this version. Both ran back to
back.
Light load: CPU 3-13 %, 2-4 Python processes. Under load: other test suites
running, CPU 34-70 %, 22-32 Python processes.

`victorique solve toy.md --transport fake --out DIR --policy full` (9 calls):

| | before | after |
|---|---|---|
| total, light load | 24.3 s | 4.3 s |
| gaps between calls, light load | 0.64, 0.93, 1.67, 2.30, 2.05, 3.83, 3.22, 3.77 s | 0.28, 0.29, 0.39, 0.40, 0.32, 0.43, 0.34, 0.37 s |
| total, under load | 36.8 s | 6.4, 7.0 and 6.5 s |
| `resume` of the finished run, light load | 3.0 s | 0.85 s |

The third, fourth and sixth gaps hold two more loop steps (feedback release and
receipt) and 30 instead of 20 transactions. Mean time of the check at the start
of a transaction, by the number of events already in the session (0-9, 10-19,
20-29, 30-39, 40-49; light load): before 6.8, 30.9, 69.4, 121.3 and 178.3 ms;
after 0.7, 1.0, 1.3, 1.6 and 2.0 ms. The rest of a transaction is mostly fixed
work (path checks, connection, commit) plus the copies of the current state
that the controller and `snapshot` make; these grow with the state, and the
gaps with one step go from 0.28 to 0.37 s.

On one `AdaptiveExchange` object, 40 request cycles (200 events, 8 transactions
per cycle) took 10.3 s before, a cycle 108-144 ms at the start and 370-394 ms at
the end, and 2.4 s after, 52-57 ms and 64-69 ms.
`tests/test_api.py::test_cli_fake_solve_and_resume[full-9]` alone took 31.0 s
before and 6.0 s after.
