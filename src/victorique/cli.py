"""Victorique command-line interface."""
import argparse
import json
import sys
from . import __version__
from .harness.codex_contract import REASONING_EFFORTS
from .settings import add_arguments, command_settings


def main():
    parser = argparse.ArgumentParser(prog="victorique", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    solve = commands.add_parser("solve", help="solve a UTF-8 problem file")
    solve.add_argument("problem", help="UTF-8 file with the problem statement")
    solve.add_argument("--policy", choices=("single", "ladder", "resample", "bestofn", "full"), default="single",
                       help="single (default): one solution, the statement check and the proof check, delivered "
                            "only if both pass; ladder adds up to --repair-rounds repairs; full plans, solves and "
                            "repairs in parts, for long problems; resample and bestofn draw fresh solutions")
    solve.add_argument("--full-calls", help="paired same-problem full calls.jsonl token reference for bestofn")
    solve.add_argument("--full-references", help="explicit JSON list of full runs and difficulty labels for bestofn target fallback")
    solve.add_argument("--repair-rounds", type=int, choices=range(101), metavar="0..100", default=2,
                       help="maximum repair (ladder) or fresh resampling (resample) rounds")
    solve.add_argument("--investigate", action="store_true",
                       help="ladder only: a finding that a repair refuted and a check reports again gets one "
                            "separate investigation (default off)")
    solve.add_argument("--escalate", action="store_true",
                       help="ladder only: when the repair rounds are used up, continue with full on the remaining "
                            "calls and time (default off)")
    solve.add_argument("--model", help="model name passed to the CLI (default: the CLI's configured default model)")
    solve.add_argument("--reasoning-effort", choices=REASONING_EFFORTS,
                       help="Codex reasoning effort for this run; omitted uses CLI defaults")
    solve.add_argument("--calls", type=int, default=12,
                       help="maximum number of model calls in the run, 2-100 (default 12)")
    solve.add_argument("--hours", type=float, default=2, help="time limit for the whole run in hours (default 2)")
    solve.add_argument("--deadline-utc", help="absolute UTC deadline; can only shorten --hours")
    solve.add_argument("--out", help="new folder for the run's files (default: runs/run-<random id> in the "
                                     "current folder)")
    solve.add_argument("--transport", choices=("codex", "claude", "fake"), default="codex",
                       help="model service for every role (default codex, a real model service): codex (Codex "
                            "CLI), claude (Claude Code CLI; log it in with an Anthropic Console account) or fake "
                            "(practice run without a model)")
    checks = solve.add_argument_group(
        "checks", "the statement check and the proof check can use another model service; every other role "
                  "keeps the solver's (--transport, --model, --reasoning-effort)")
    checks.add_argument("--check-transport", choices=("codex", "claude", "fake"),
                        help="model service of the two checks (default: the same as --transport); fake only "
                             "with --transport fake")
    checks.add_argument("--check-model",
                        help="model of the two checks (default: --model when the checks use the solver's "
                             "service, else that CLI's default model)")
    checks.add_argument("--check-reasoning-effort", choices=REASONING_EFFORTS,
                        help="Codex reasoning effort of the two checks (default: --reasoning-effort when the "
                             "checks use the solver's service, else the CLI's default)")
    add_arguments(solve)
    for name, help_text in (("status", "read a run's current status"),
                            ("resume", "continue a run with its frozen settings"),
                            ("export", "export a run's proof and summary"),
                            ("stop", "ask a run to stop: no new model call, the call in progress is abandoned, "
                                     "the best draft is kept (end reason stopped_by_user)")):
        commands.add_parser(name, help=help_text, description=help_text).add_argument("directory")
    from . import bench, gui, verify
    bench.add_parser(commands)
    verify.add_parser(commands)
    gui.add_parser(commands)
    args = parser.parse_args()
    from . import api
    try:
        if args.command == "solve":
            options = command_settings(args.config, call_timeout_seconds=args.call_timeout_seconds,
                                       undecided_checks=args.undecided_checks, minor_findings=args.minor_findings,
                                       timeout_retries=args.timeout_retries)
            result = api.solve(args.problem, policy=args.policy, model=args.model, calls=args.calls, hours=args.hours,
                               out=args.out, transport=args.transport, repair_rounds=args.repair_rounds,
                               investigate=args.investigate, escalate=args.escalate, full_calls=args.full_calls,
                               full_references=args.full_references, deadline_utc=args.deadline_utc,
                               reasoning_effort=args.reasoning_effort, check_transport=args.check_transport,
                               check_model=args.check_model, check_reasoning_effort=args.check_reasoning_effort,
                               _command=list(sys.argv), **options)
        elif args.command == "bench":
            result = bench.run_args(args)
        elif args.command == "verify":
            raise SystemExit(verify.run_args(args))
        elif args.command == "gui":
            raise SystemExit(gui.run_args(args))
        else:
            result = getattr(api, args.command)(args.directory)
    except (ValueError, OSError) as error:
        parser.exit(2, f"victorique: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))
