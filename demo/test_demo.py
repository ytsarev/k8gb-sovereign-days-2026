"""Behavior checks; no cluster or Docker daemon required."""
import http.server
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

import core
import dashboard as ui
from resolver import unbound_config

CFG = core.load_config(Path(__file__).parent / "config.json")
DIG = ''';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1440
;; flags: qr aa; QUERY: 1, ANSWER: 2, AUTHORITY: 0, ADDITIONAL: 0

;; ANSWER SECTION:
failover.cloud.example.com.  5 IN A 172.20.0.5
failover.cloud.example.com.  5 IN A 172.20.0.6
'''


class ReadinessTests(unittest.TestCase):
    def test_ready_false_nil_and_empty_addresses(self):
        data = {"items": [{"endpoints": [
            {"addresses": ["1"], "conditions": {"ready": True}},
            {"addresses": ["2"], "conditions": {"ready": False}},
            {"addresses": ["3"], "conditions": {"ready": None}},
            {"addresses": ["4"]}, {"addresses": [], "conditions": {"ready": True}},
            {"addresses": ["1"], "conditions": {"ready": True}}
        ]}]}
        self.assertEqual(core.endpoint_counts(data), {"ready": 3, "total": 4})

    def test_no_slices_is_zero_but_api_failure_is_unknown(self):
        self.assertEqual(core.endpoint_counts({"items": []})["ready"], 0)
        with patch("core.kube", side_effect=RuntimeError("API timeout")):
            data = core.collect_readiness(CFG, "A")
        self.assertFalse(data["ok"])
        self.assertNotIn("ready", data)


class DNSTests(unittest.TestCase):
    def test_records_and_ttl_are_parsed(self):
        data = core.parse_dig(DIG, authoritative=True)
        self.assertTrue(data["authoritative"])
        self.assertEqual([r["ttl"] for r in data["records"]], [5, 5])
        self.assertEqual([r["ip"] for r in data["records"]], ["172.20.0.5", "172.20.0.6"])

    def test_non_authoritative_response_rejected_only_for_authority(self):
        data = DIG.replace("qr aa;", "qr rd ra;")
        self.assertEqual(len(core.parse_dig(data)["records"]), 2)
        with self.assertRaisesRegex(ValueError, "authoritative"):
            core.parse_dig(data, authoritative=True)

    def test_errors_are_not_empty_success(self):
        for output in ("connection timed out", DIG.replace("NOERROR", "SERVFAIL"), DIG.replace("NOERROR", "NXDOMAIN")):
            with self.assertRaises(ValueError):
                core.parse_dig(output)

    def test_noerror_empty_answer_is_distinct(self):
        self.assertEqual(core.parse_dig(DIG.split(";; ANSWER SECTION:")[0])["records"], [])

    def test_query_is_bounded_and_nonrecursive_for_authorities(self):
        with patch("core.command", return_value=DIG) as command:
            data = core.query_dns("failover.cloud.example.com", "127.0.0.1", 5053, True)
        self.assertTrue(data["ok"])
        self.assertIn("+norecurse", command.call_args.args[0])
        self.assertEqual(command.call_args.kwargs["timeout"], 4)


class Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    payload = {"message": "Ingress NGINX: eu"}

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.payload).encode())

    def log_message(self, *args):
        pass


class HTTPLoopbackTests(unittest.TestCase):
    def setUp(self):
        Handler.status, Handler.payload = 200, {"message": "Ingress NGINX: eu"}
        try:
            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("Environment denies loopback socket binding; run outside sandbox")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self):
        return core.request_application("localhost", CFG["clusters"], self.server.server_port)

    def test_served_cluster_comes_from_real_http_payload(self):
        self.assertEqual(self.request()["served_by"], "A")
        Handler.payload = {"message": "Ingress NGINX: us"}
        self.assertEqual(self.request()["served_by"], "B")

    def test_unknown_identity_is_never_inferred(self):
        Handler.payload = {"message": "ready"}
        response = self.request()
        self.assertTrue(response["success"])
        self.assertIsNone(response["served_by"])

    def test_error_status_is_failure_even_with_cluster_message(self):
        Handler.status = 503
        response = self.request()
        self.assertFalse(response["success"])
        self.assertEqual(response["status"], 503)
        self.assertIsNone(response["served_by"])


class HTTPResponseTests(unittest.TestCase):
    def test_payload_identity_and_original_hostname(self):
        for message, expected in (("Ingress NGINX: eu", "A"), ("Ingress NGINX: us", "B"), ("ok", None)):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = json.dumps({"message": message}).encode()
            with patch("core.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = response
                data = core.request_application(CFG["hostname"], CFG["clusters"])
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, "http://failover.cloud.example.com/")
            self.assertTrue(data["success"])
            self.assertEqual(data["served_by"], expected)

    def test_http_error_and_network_error_are_retained(self):
        error = core.urllib.error.HTTPError("http://example.com", 503, "Unavailable", {}, io.BytesIO(b"no"))
        with patch("core.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            data = core.request_application(CFG["hostname"], CFG["clusters"])
        self.assertFalse(data["success"])
        self.assertEqual(data["status"], 503)
        with patch("core.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("request timed out")
            data = core.request_application(CFG["hostname"], CFG["clusters"])
        self.assertFalse(data["ok"])
        self.assertIn("timed out", data["error"])


class DashboardTests(unittest.TestCase):
    def state_at(self, offset):
        start, messages = ui.preview_messages(CFG)
        state = ui.State(CFG, "ILLUSTRATIVE PREVIEW", start)
        for message in messages:
            if message["data"]["at"] <= start + offset:
                state.apply(message)
        return state, start + offset

    def test_failover_cache_lag_and_recovery_are_visible(self):
        state, clock = self.state_at(18)
        text = ui.render(state, 132, 44, clock).plain()
        self.assertIn("The client resolver returns a different answer", text)
        self.assertIn("FAILED", text)
        self.assertIn("0/0 READY ENDPOINTS", text)
        self.assertIn("ILLUSTRATIVE PREVIEW", text)
        state, clock = self.state_at(26)
        self.assertIn("Request served by CLUSTER B", ui.render(state, 132, 44, clock).plain())
        state, clock = self.state_at(46)
        self.assertIn("Request served by CLUSTER A", ui.render(state, 132, 44, clock).plain())

    def test_stale_observations_cannot_stay_green(self):
        state, clock = self.state_at(26)
        text = ui.render(state, 132, 44, clock + 20).plain()
        self.assertIn("STALE", text)
        self.assertNotIn("1/1 READY ENDPOINTS", text)
        self.assertNotIn("Request served by CLUSTER B", text)
        self.assertNotIn("TTL returned:", text)
        future_text = ui.render(state, 132, 44, clock - 20).plain()
        self.assertIn("observer clock is ahead", future_text)
        self.assertNotIn("1/1 READY ENDPOINTS", future_text)

    def test_duplicate_and_old_observations_not_counted(self):
        state, clock = self.state_at(26)
        before = state.counts.copy()
        data = state.latest["request"].copy()
        state.apply({"source": "request", "data": data})
        data["at"] -= 2
        state.apply({"source": "request", "data": data})
        self.assertEqual(before, state.counts)

    def test_resize_and_svg(self):
        state, clock = self.state_at(18)
        for width, height in ((90, 25), (110, 38), (132, 44), (170, 52)):
            canvas = ui.render(state, width, height, clock)
            self.assertEqual(len(canvas.cells), height)
            self.assertTrue(all(len(row) == width for row in canvas.cells))
            self.assertIn("<svg", canvas.svg())
        self.assertIn("Enlarge terminal", ui.render(state, 90, 25, clock).plain())

    def test_terminal_control_characters_are_removed(self):
        self.assertNotIn("\x1b", core.clean("oops\x1b[2J"))
        self.assertNotIn("\n", core.clean("oops\ntext"))

    def test_replay_requires_live_origin_and_preserves_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            recording = ui.Recording(path, CFG, 100)
            message = {"source": "request", "received_at": 102,
                "data": {"at": 101, "ok": False, "error": "connection refused"}}
            recording.append(message)
            recording.close()
            header, messages = ui.replay_file(path)
            self.assertEqual(messages, [message])
            self.assertEqual(header["start"], 100)
            with self.assertRaises(FileExistsError):
                ui.Recording(path, CFG, 103)
            path.write_text(json.dumps({"schema": "k8gb-cli-v1", "origin": "preview"}) + "\n")
            with self.assertRaisesRegex(ValueError, "live observation"):
                ui.replay_file(path)


class ResolverTests(unittest.TestCase):
    def test_parent_delegation_and_real_cache(self):
        text = unbound_config({**CFG, "parent_address": "172.20.0.10"})
        self.assertIn("stub-addr: 172.20.0.10@30053", text)
        self.assertIn('name: "example.com"', text)
        self.assertIn("stub-prime: no", text)
        self.assertIn("serve-expired: no", text)
        self.assertIn("cache-min-ttl: 0", text)
        self.assertNotIn("local-data", text)


if __name__ == "__main__":
    unittest.main()
