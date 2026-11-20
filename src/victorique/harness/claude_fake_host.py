"""Offline Claude-shaped executable; imports no model client and uses no network."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    scenario, response_file, delay = sys.argv[1:]
    packet = sys.stdin.buffer.read()
    Path("stdin-observed.bin").write_bytes(packet)
    # Record only synthetic/constant isolation controls, never inherited values.
    Path("isolation.json").write_text(json.dumps({
        "memory_disabled": os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1",
        "api_key_absent": "ANTHROPIC_API_KEY" not in os.environ,
        "auto_memory_disabled": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1",
    }), encoding="utf-8")
    time.sleep(float(delay))
    init = {"type": "system", "subtype": "init", "session_id": "fixture-claude", "model": "fixture-model",
            "tools": ["Bash"] if scenario == "tool" else ["StructuredOutput"], "mcp_servers": []}
    print(json.dumps(init), flush=True)
    if scenario == "timeout":
        time.sleep(20)
    if scenario == "oversize":
        print("x" * 10000)
        return 0
    if scenario == "malformed":
        print("{invalid")
        return 0
    result = {"type": "result", "subtype": "error_max_turns" if scenario == "failure" else "success",
              "is_error": scenario == "failure", "session_id": "fixture-claude",
              "usage": {"input_tokens": 9, "output_tokens": 4}}
    raw = Path(response_file).read_bytes().decode("utf-8").strip()
    if scenario == "mismatch":
        response = json.loads(raw)
        response["binding"]["packet_id"] = "wrong"
        raw = json.dumps(response)
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "StructuredOutput", "id": "output-1", "input": json.loads(raw)}]}}))
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "output-1", "content": "Structured output provided successfully"}]}}))
    print(json.dumps(result)[:-1] + ', "structured_output": ' + raw + '}', flush=True)
    return 1 if scenario == "failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
