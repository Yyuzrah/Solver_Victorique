"""INERT child executable for harness software tests. No model or network use."""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"success", "malformed", "nonzero", "timeout", "truncated"}:
        return 64
    request = json.loads(sys.stdin.read())
    packet, claim = request["packet"], request["claim"]
    if packet.get("data_classification") != "synthetic_software_test" or packet.get("inert") is not True:
        return 65
    mode = sys.argv[1]
    if mode == "timeout":
        print(json.dumps({"type": "fixture.started", "dispatch_id": claim["dispatch_id"]}), flush=True)
        time.sleep(30)
    if mode == "nonzero":
        print("INERT simulated process failure", file=sys.stderr)
        return 7
    if mode == "malformed":
        print("INERT not-json")
        return 0
    if mode == "truncated":
        print('{"inert":true,', end="")
        return 0
    print(json.dumps({"inert": True, "received_job_id": claim["job_id"],
                      "received_dispatch_id": claim["dispatch_id"], "response": "INERT_OK"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
