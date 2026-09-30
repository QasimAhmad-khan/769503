"""Real-transport code paths exercised against local stubs (the actual models were not available).
These check wire contracts and failure handling only — never model quality."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cqc.config import load_config
from cqc.llm.jev import JevSelector, OpenJevTransport
from cqc.llm.phi import OpenAICompatiblePhiBackend, PhiFailure, PhiService
from cqc.util import utc

CANDS = [{"id": "cand_a", "action": "open_long", "size_class": "full", "utility_lcb_quote": "2"},
         {"id": "cand_b", "action": "open_long", "size_class": "reduced", "utility_lcb_quote": "1"}]


class _OpenJevStub(BaseHTTPRequestHandler):
    """Mimics Open-Jev serving.py: accepts model None/open-jev, returns format_response()-shaped answers."""
    mode = "ok"
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"models": [{"id": "Open-Jev-9B"}], "aliases": ["open-jev", "jev-latest"]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(req)
        if self.path != "/v1/systemone" or req.get("model") not in (None, "open-jev", "Open-Jev-9B"):
            return self._send(422, {"error": "unknown model"})
        if type(self).mode == "error":
            return self._send(500, {"error": "model inference failed", "error_type": "OutOfMemoryError"})
        keys = list(req["questions"]["selected_candidate"]["criteria"])
        probs = [0.1] * len(keys)
        probs[keys.index("cand_b")] = 1 - 0.1 * (len(keys) - 1)
        self._send(200, {"answers": {"selected_candidate": {
            "type": "choice", "choice": "cand_b", "probabilities": dict(zip(keys, probs)), "confidence": 0.42}}})


@pytest.fixture()
def openjev():
    server = HTTPServer(("127.0.0.1", 0), _OpenJevStub)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    _OpenJevStub.mode, _OpenJevStub.seen = "ok", []
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_openjev_http_round_trip_produces_valid_receipt(openjev):
    cfg = load_config()
    tr = OpenJevTransport(openjev, timeout_s=2)
    assert tr.models() == ["Open-Jev-9B"]
    receipt, raw = JevSelector(cfg, tr).select(snapshot_id="s", hypothesis="h", facts={"x": 1}, candidates=CANDS,
                                               now=utc(2026, 1, 1), correlation_id="c", ttl_seconds=60)
    assert receipt["validation_status"] == "valid" and receipt["selected_id"] == "cand_b"
    assert receipt["provider"] == "open_jev" and receipt["score_semantics"] == "categorical_preference_not_win_probability"
    sent = _OpenJevStub.seen[-1]
    assert sent["model"] == "open-jev" and set(sent["questions"]) == {"selected_candidate"}


def test_openjev_server_error_and_unreachable_mean_abstain(openjev):
    cfg = load_config()
    _OpenJevStub.mode = "error"
    receipt, _ = JevSelector(cfg, OpenJevTransport(openjev, 2)).select(
        snapshot_id="s", hypothesis="h", facts={}, candidates=CANDS, now=utc(2026, 1, 1), correlation_id="c",
        ttl_seconds=60)
    assert receipt["selected_id"] == "ABSTAIN" and receipt["validation_status"] == "unavailable"
    receipt, _ = JevSelector(cfg, OpenJevTransport("http://127.0.0.1:9/v1", 1)).select(
        snapshot_id="s", hypothesis="h", facts={}, candidates=CANDS, now=utc(2026, 1, 1), correlation_id="c",
        ttl_seconds=60)
    assert receipt["selected_id"] == "ABSTAIN" and receipt["validation_status"] in ("unavailable", "timeout")


def test_phi_openai_compatible_backend_structured_output_and_repair_limit():
    cfg = load_config()
    calls = []

    def post(body, timeout):
        calls.append(body)
        assert body["response_format"]["type"] == "json_schema" and body["max_tokens"] == cfg["phi"]["max_output_tokens"]
        content = json.dumps({"kind": "adjustment_proposal", "status": "no_change", "trigger": "t", "position_refs": [],
                              "action_category": "none", "stress_scenarios": [], "evidence_ids": [],
                              "reason_codes": ["ADVISORY_ONLY"]})
        return {"choices": [{"message": {"content": content}}]}
    svc = PhiService(cfg, OpenAICompatiblePhiBackend("http://x", "microsoft/Phi-4-mini-instruct", "rev", http_post=post))
    out = svc.run("risk_analyst", {"trigger": "t"}, now=utc(2026, 1, 1), correlation_id="c")
    assert out["kind"] == "adjustment_proposal" and out["producer"].startswith("phi:risk_analyst")

    bad = PhiService(cfg, OpenAICompatiblePhiBackend("http://x", "m", "r", http_post=lambda b, t: {
        "choices": [{"message": {"content": '{"kind": "adjustment_proposal", "event_id": "forged"}'}}]}))
    with pytest.raises(PhiFailure):
        bad.run("risk_analyst", {"trigger": "t"}, now=utc(2026, 1, 1), correlation_id="c")
    assert bad.stats["calls"] == 1 + cfg["phi"]["max_schema_repair_retries"]  # bounded retries, then fail


def test_phi_role_cannot_return_another_roles_payload():
    cfg = load_config()
    post = lambda b, t: {"choices": [{"message": {"content": json.dumps({  # noqa: E731
        "kind": "tool_request", "tool": "retrieve_evidence_batch", "tool_version": "1", "arguments": {},
        "input_ids": []})}}]}
    svc = PhiService(cfg, OpenAICompatiblePhiBackend("http://x", "m", "r", http_post=post))
    with pytest.raises(PhiFailure):
        svc.run("risk_analyst", {"trigger": "t"}, now=utc(2026, 1, 1), correlation_id="c")
