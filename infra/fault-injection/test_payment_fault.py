"""payment_fault.py를 목 서버로 확인한다 (실제 shop-api 없이)."""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "payment_fault", Path(__file__).with_name("payment_fault.py")
)
payment_fault = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(payment_fault)


class _Shop(BaseHTTPRequestHandler):
    state: dict = {"active": False}
    seen: list = []
    reject = False

    def _send(self, code: int, data: dict) -> None:
        payload = json.dumps({"data": data}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        type(self).seen.append((method, self.path, self.headers.get("Authorization"), body))
        if type(self).reject:
            return self._send(403, {})
        if method == "POST":
            type(self).state = {"active": True, **body}
            return self._send(201, type(self).state)
        if method == "DELETE":
            type(self).state = {"active": False}
        self._send(200, type(self).state)

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")

    def log_message(self, *args):
        pass


class PaymentFaultTest(unittest.TestCase):
    def setUp(self):
        _Shop.state, _Shop.seen, _Shop.reject = {"active": False}, [], False
        self.server = HTTPServer(("127.0.0.1", 0), _Shop)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        env = {
            "SHOP_API_URL": f"http://127.0.0.1:{self.server.server_port}",
            "SHOP_FAULT_TOKEN": "mock:admin",
        }
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_inject_then_clear(self):
        self.assertEqual(payment_fault.main(["inject"]), 0)
        self.assertTrue(_Shop.state["active"])
        self.assertEqual(_Shop.state["kind"], "failure")
        self.assertEqual(payment_fault.main(["clear"]), 0)
        self.assertFalse(_Shop.state["active"])
        self.assertIn("DELETE", [m for m, *_ in _Shop.seen])

    def test_latency_sends_delay(self):
        self.assertEqual(
            payment_fault.main(["inject", "--kind", "latency", "--delay-ms", "1500"]), 0
        )
        self.assertEqual(_Shop.state["delay_ms"], 1500)

    def test_http_error_is_nonzero(self):
        _Shop.reject = True
        self.assertEqual(payment_fault.main(["inject"]), 1)

    def test_unreachable_is_nonzero(self):
        with mock.patch.dict("os.environ", {"SHOP_API_URL": "http://127.0.0.1:1"}):
            self.assertEqual(payment_fault.main(["clear"]), 1)

    def test_token_not_printed(self):
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            payment_fault.main(["inject"])
        printed = "".join(str(c) for c in out.write.call_args_list + err.write.call_args_list)
        self.assertNotIn("mock:admin", printed)


if __name__ == "__main__":
    unittest.main()
