"""INERT Codex-shaped process fixture. No network, credential or model access."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid


def emit(event):
    sys.stdout.buffer.write((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--final-file", required=True)
    parser.add_argument("--response-file")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--output-bytes", type=int, default=4096)
    args = parser.parse_args()
    packet = sys.stdin.buffer.read()
    Path("stdin-observed.bin").write_bytes(packet)
    response = Path(args.response_file).read_bytes() if args.response_file else b'{"inert":true}'
    if args.scenario != "delay-after-thread-start":
        time.sleep(args.delay)
    final = Path(args.final_file)
    if args.scenario == "binary":
        sys.stdout.buffer.write(b"\xff\x00\r\n")
        sys.stderr.buffer.write(b"\xfe\x00\n")
        final.write_bytes(b"\x00\xff\x01")
        return 0
    if args.scenario == "oversize":
        sys.stdout.buffer.write(b"O" * args.output_bytes)
        sys.stderr.buffer.write(b"E" * args.output_bytes)
        final.write_bytes(b"F" * args.output_bytes)
        return 0
    emit({"type": "thread.started", "thread_id": "INERT-" + uuid.uuid4().hex})
    if args.scenario == "delay-after-thread-start":
        time.sleep(args.delay)
    emit({"type": "turn.started"})
    if args.scenario in ("reconnect-success", "reconnect-failed"):
        emit({"type": "error", "message": "Reconnecting... 2/5 (exceeded retry limit, last status: 429 Too Many Requests)"})
    if args.scenario == "timeout":
        time.sleep(60)
    if args.scenario in ("nonzero", "reconnect-failed"):
        emit({"type": "turn.failed", "error": {"message": "INERT failure"}})
        return 7
    if args.scenario != "no-final":
        final.write_bytes(response)
    if args.scenario == "malformed":
        sys.stdout.buffer.write(b"INERT invalid JSON\n")
        return 0
    emit({"type": "item.completed", "item": {"id": "INERT-final", "type": "agent_message",
          "text": '{"inert_mismatch":true}' if args.scenario == "mismatch" else response.decode("utf-8")}})
    emit({"type": "turn.completed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
