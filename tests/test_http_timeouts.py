"""Idle connections must not indefinitely block the serialized local server."""

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from app import ReviewHandler, ReviewHTTPServer, ReviewStore


class QuietHandler(ReviewHandler):
    def log_message(self, *args):
        pass


class HTTPTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.temporary.name) / "runtime", seed_demo=False)
        self.server = ReviewHTTPServer(("127.0.0.1", 0), self.store)
        self.server.connection_timeout_seconds = 0.1
        self.server.RequestHandlerClass = QuietHandler
        self.address = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.store.close()
        self.temporary.cleanup()

    def state(self):
        connection = http.client.HTTPConnection(*self.address, timeout=2)
        try:
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def assert_empty_workspace(self):
        state = self.state()
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["events"], [])
        self.assertEqual(list(self.store.evidence_dir.rglob("*")), [])

    def send_incomplete_body(self, end_stream=False):
        # The supplied JSON is valid by itself. Its larger declared length must
        # prevent any mutation, both on inactivity and on an early EOF.
        body = json.dumps({
            "id": "TIMEOUT-1", "title": "Synthetic request",
            "summary": "Must not be committed.", "owner": "", "actor": "Test",
            "note": "Incomplete HTTP body", "is_demo": True,
            "evidence": [{"name": "result.txt", "content": "synthetic\n"}],
        }).encode("utf-8")
        headers = (
            "POST /api/submissions HTTP/1.1\r\n"
            "Host: 127.0.0.1:{}\r\n"
            "X-ReviewOps-Token: {}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n\r\n"
        ).format(self.address[1], self.server.token, len(body) + 1).encode("ascii")
        with socket.create_connection(self.address, timeout=2) as connection:
            connection.sendall(headers + body)
            if end_stream:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            status = response.status
            payload = json.loads(response.read())
            response.close()
        return status, payload

    def test_idle_connection_expires_and_normal_request_still_works(self):
        with socket.create_connection(self.address, timeout=2) as connection:
            self.assertEqual(connection.recv(1), b"")
        self.assert_empty_workspace()

    def test_incomplete_headers_expire_and_normal_request_still_works(self):
        with socket.create_connection(self.address, timeout=2) as connection:
            connection.sendall(
                "GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "X-Unfinished: ".format(self.address[1]).encode("ascii"))
            self.assertEqual(connection.recv(1), b"")
        self.assert_empty_workspace()

    def test_incomplete_body_times_out_without_partial_submission(self):
        status, payload = self.send_incomplete_body()
        self.assertEqual(status, 408)
        self.assertIn("not received in time", payload["error"])
        self.assert_empty_workspace()

    def test_early_body_eof_cannot_commit_valid_json_with_wrong_length(self):
        status, payload = self.send_incomplete_body(end_stream=True)
        self.assertEqual(status, 400)
        self.assertIn("declared length", payload["error"])
        self.assert_empty_workspace()

    def test_complete_body_is_still_accepted(self):
        body = {
            "id": "COMPLETE-1", "title": "Complete synthetic request",
            "summary": "A normal submission.", "actor": "Test",
            "note": "Ready to inspect", "is_demo": True,
            "evidence": [{"name": "result.txt", "content": "synthetic\n"}],
        }
        connection = http.client.HTTPConnection(*self.address, timeout=2)
        try:
            connection.request("POST", "/api/submissions", body=json.dumps(body),
                               headers={
                                   "Content-Type": "application/json",
                                   "X-ReviewOps-Token": self.server.token,
                               })
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            detail = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(detail["run"]["id"], "COMPLETE-1")
        self.assertEqual(detail["run"]["version"], 1)
        self.assertEqual(detail["run"]["evidence_status"], "verified")
        self.assertEqual(len(self.state()["runs"]), 1)


if __name__ == "__main__":
    unittest.main()
