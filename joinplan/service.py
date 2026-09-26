"""HTTP and stdin JSON entrypoint for the join planner."""

from __future__ import annotations

import argparse
import decimal
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .planner import PlannerError, plan_request


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class JoinPlanHandler(BaseHTTPRequestHandler):
    server_version = "JoinPlan/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/health":
            _write_json(self, HTTPStatus.OK, {"status": "ok"})
        else:
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/plan":
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty request body")
            raw = self.rfile.read(length)
            request = json.loads(raw.decode("utf-8"), parse_float=decimal.Decimal)
            result = plan_request(request)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, PlannerError) as exc:
            _write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # Keep malformed requests from killing the service.
            _write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        _write_json(self, HTTPStatus.OK, result)

    def log_message(self, fmt: str, *args: Any) -> None:
        super().log_message(fmt, *args)


def run_stdin() -> int:
    try:
        request = json.loads(
            sys.stdin.buffer.read().decode("utf-8"), parse_float=decimal.Decimal
        )
        result = plan_request(request)
        status = 0
    except (UnicodeDecodeError, json.JSONDecodeError, PlannerError) as exc:
        result = {"error": str(exc)}
        status = 1

    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return status


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), JoinPlanHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Database-free JSON join planner")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read one JSON request from stdin and print the plan",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.stdin:
        return run_stdin()

    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
