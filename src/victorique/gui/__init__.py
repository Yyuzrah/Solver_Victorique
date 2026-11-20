"""Local web interface: ``victorique gui`` starts runs and shows their progress and results."""


def port(value):
    """An argparse type for a TCP port number."""
    number = int(value)
    if not 0 <= number <= 65535:
        raise ValueError(value)
    return number


def add_parser(commands):
    parser = commands.add_parser(
        "gui", help="open the local web interface",
        description="Serve a web page on this computer for starting runs and reading their results.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1: only this computer can connect)")
    parser.add_argument("--port", type=port, default=None,
                        help="port to listen on (default 8765, or when it is taken the next free port up to 8785, "
                             "then any free port; 0 picks a free port)")
    parser.add_argument("--runs-dir", default="runs", help="folder that holds the runs (default ./runs)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a --host that other computers can reach (they could then start runs)")
    return parser


def run_args(args):
    from .server import run_args as run
    return run(args)
