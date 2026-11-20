"""Fixed INERT subprocess transport; never launches a real host CLI.

Host command builders and skills are a different layer.  This candidate has no
arbitrary executable hook, authentication discovery, daemon or model transport.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .journal import HarnessError, PACKET_CLASS, RECEIPT_SCHEMA, validate_packet


class MockProcessTransport:
    MODES = frozenset({"success", "malformed", "nonzero", "timeout", "truncated"})

    def __init__(self, mode: str = "success", timeout_seconds: float = 5):
        if mode not in self.MODES:
            raise HarnessError("unsupported mock mode")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0.01 <= timeout_seconds <= 10:
            raise HarnessError("mock timeout must be between 0.01 and 10 seconds")
        self.mode = mode
        self.timeout_seconds = timeout_seconds

    def __call__(self, packet: dict, claim: dict) -> dict:
        packet = validate_packet(packet)
        if claim.get("data_classification") != PACKET_CLASS or not claim.get("dispatch_id") or not claim.get("job_id"):
            raise HarnessError("mock transport requires an inert dispatch claim")
        # Keep only OS startup essentials. Do not forward user authentication env.
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        fixture = Path(__file__).with_name("fake_host.py")
        argv = [sys.executable, "-I", "-B", str(fixture), self.mode]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", shell=False, env=environment,
                                   cwd=str(fixture.parent), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timed_out = False
        try:
            stdout, stderr = process.communicate(json.dumps({"packet": packet, "claim": claim}, ensure_ascii=True),
                                                  timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate()
        output = None
        error = None
        state = "unknown" if timed_out else "failed"
        if timed_out:
            error = "observation_timeout"
        elif process.returncode != 0:
            error = "nonzero_exit"
        else:
            try:
                candidate = json.loads(stdout)
                if not isinstance(candidate, dict) or candidate.get("inert") is not True:
                    raise ValueError("not an inert object")
                if candidate.get("received_job_id") != claim["job_id"] or candidate.get("received_dispatch_id") != claim["dispatch_id"]:
                    raise ValueError("wrong dispatch")
                output = candidate
                state = "completed"
            except (ValueError, TypeError):
                error = "invalid_output"
        return {"schema": RECEIPT_SCHEMA, "data_classification": PACKET_CLASS,
                "job_id": claim["job_id"], "dispatch_id": claim["dispatch_id"],
                "transport_status": state,
                "process": {"pid": process.pid, "exit_code": process.returncode,
                            "timed_out": timed_out, "kill_requested": timed_out},
                "argv": argv, "stdout": stdout, "stderr": stderr,
                "host_session_id": f"inert-{claim['dispatch_id']}" if output is not None else None,
                "output": output, "error": error,
                "requested_model": packet.get("requested_model"), "actual_model": None,
                "usage": None, "cost": None, "isolation_verified": False,
                "external_host_verified": False}
