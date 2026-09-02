#!/usr/bin/env python3
"""
mock_sonar.py — a recording GraphQL stub that stands in for a Sonar tenant.

Purpose: let stage L4 exercise the gateway's real Jinjava rendering, real
receiveFrom chaining and real result publication without touching a customer
instance, and without any mutation ever reaching real data.

It does three things and nothing else:
  * answers POST /api/graphql by matching on `operationName`
  * appends every received {operationName, query, variables} to a JSONL
    recording so the harness can assert on what was actually sent
  * serves GET /__recording and POST /__reset for the harness to drive

It is deliberately dumb. It does not validate GraphQL against a schema and it
does not model Sonar's semantics. Anything that depends on how Sonar actually
behaves belongs in stage L5 against a real tenant, not here.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOCK = threading.Lock()
STATE = {"scenarios": {}, "recording": [], "path": None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):          # keep harness output readable
        pass

    def _send(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/__recording":
            with LOCK:
                self._send(200, {"requests": list(STATE["recording"])})
        elif self.path == "/__health":
            self._send(200, {"status": "ok",
                             "scenarios": sorted(STATE["scenarios"])})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""

        if self.path == "/__reset":
            with LOCK:
                STATE["recording"].clear()
            self._send(200, {"reset": True})
            return

        if not self.path.endswith("/graphql"):
            self._send(404, {"error": "not found"})
            return

        try:
            body = json.loads(raw)
        except Exception as exc:
            # A malformed body is the single most useful failure this stub can
            # report: it means the gateway rendered a broken document.
            with LOCK:
                STATE["recording"].append({"malformed": True, "raw": raw,
                                           "error": str(exc)})
            self._send(400, {"errors": [{"message": f"malformed body: {exc}"}]})
            return

        entry = {"operationName": body.get("operationName"),
                 "query": body.get("query"),
                 "variables": body.get("variables"),
                 "auth": self.headers.get("Authorization", "")[:16]}
        with LOCK:
            STATE["recording"].append(entry)
            if STATE["path"]:
                with open(STATE["path"], "a") as fh:
                    fh.write(json.dumps(entry) + "\n")

        scenario = STATE["scenarios"].get(entry["operationName"])
        if scenario is None:
            self._send(200, {"data": None, "errors": [
                {"message": f"no scenario for {entry['operationName']}"}]})
            return

        # A scenario may branch on a variable value, which is how the
        # serial-not-found path is exercised.
        for case in scenario.get("cases", []):
            var, want = case["when"]["variable"], case["when"]["equals"]
            if (entry["variables"] or {}).get(var) == want:
                self._send(200, case["respond"])
                return
        self._send(200, scenario["default"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--scenarios", default="harness/scenarios.json")
    ap.add_argument("--record-to", default=None)
    args = ap.parse_args()

    STATE["scenarios"] = json.loads(Path(args.scenarios).read_text())["sonar"]
    STATE["path"] = args.record_to
    srv = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"mock-sonar listening on :{args.port} "
          f"({len(STATE['scenarios'])} scenarios)")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
