"""The real local-Phi HTTP path exercised against a stub OpenAI-compatible server (the real model was not
available). These check the wire contract, the single-backend/four-role structure, serialized admission
and fail-closed handling only — never model quality."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cqc.config import load_config
from cqc.contracts import ABSTAIN
from cqc.llm.phi import (FakePhiBackend, InferenceQueue, OpenAICompatiblePhiBackend, PhiFailure, PhiInvalidOutput,
                         PhiOverloaded, PhiService, ContextTooLarge)
from cqc.selection import PhiDecisionSelector
from cqc.util import utc

NOW = utc(2026, 1, 1)
PLAN_IDS = ["cand_a", "cand_b"]


def _body_for(role, req):
    schema = req["response_format"]["json_schema"]["schema"]
    if role == "decision_maker":
        allowed = schema["properties"]["selected_id"]["enum"]
        pick = _Stub.decision or allowed[0]
        return {"kind": "decision_choice", "selected_id": pick, "reason_codes": ["EVIDENCE_SUPPORTS"]}
    if role == "risk_analyst":
        return {"kind": "adjustment_proposal", "status": "no_change", "trigger": "t", "position_refs": [],
                "action_category": "none", "stress_scenarios": [], "evidence_ids": [], "reason_codes": ["ADVISORY_ONLY"]}
    if role == "screener":
        return {"kind": "fetch_plan", "request_id": "r", "fetch": [], "reason_codes": ["FETCH_WHITELISTED"]}
    return {"kind": "analysis_packet", "snapshot_id": "s", "hypothesis_id": "h", "status": "abstain",
            "tool_result_ids": [], "candidate_ids": [], "effective_sample_count": 0, "missing": [],
            "reason_codes": ["NO_ELIGIBLE_CANDIDATE"]}


class _Stub(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /v1/chat/completions server standing in for vLLM + Phi-4-mini-instruct."""
    seen, decision, fail, delay = [], None, False, 0.0

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(req)
        time.sleep(type(self).delay)
        if type(self).fail:
            data = b'{"error": "CUDA out of memory"}'
            self.send_response(500)
        else:
            role = req["response_format"]["json_schema"]["name"].replace("_result", "")
            content = json.dumps(_body_for(role, req))
            data = json.dumps({"choices": [{"message": {"content": content}}],
                               "usage": {"prompt_tokens": 900, "completion_tokens": 40}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def phi_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Stub.seen, _Stub.decision, _Stub.fail, _Stub.delay = [], None, False, 0.0
    yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    server.shutdown()


def _service(url, cfg=None):
    cfg = cfg or load_config()
    return cfg, PhiService(cfg, OpenAICompatiblePhiBackend(url, "microsoft/Phi-4-mini-instruct", "rev-pinned"))


def _plans():
    import copy
    from pathlib import Path
    base = next(r for r in json.loads((Path(__file__).resolve().parent.parent / "04_EXAMPLES.json").read_text())["records"]
                if r["kind"] == "candidate_plan")
    out = []
    for cid in PLAN_IDS:
        p = copy.deepcopy(base)
        p["candidate_id"], p["expires_at"] = cid, "2099-01-01T00:00:00Z"
        out.append(p)
    return out


def test_all_four_roles_use_one_endpoint_and_model_revision(phi_server):
    cfg, svc = _service(phi_server)
    sel = PhiDecisionSelector(cfg, svc)
    rec = sel.select(snapshot_id="s", hypothesis="h", facts={}, plans=_plans(), equity=10000, now=NOW,
                     correlation_id="c", ttl_seconds=60)
    svc.run("risk_analyst", {"trigger": "t"}, now=NOW, correlation_id="c")
    svc.run("screener", {"stage": "fetch", "x": 1}, now=NOW, correlation_id="c")
    svc.run("analyzer", {"stage": "packet", "x": 1}, now=NOW, correlation_id="c")
    assert {r["model"] for r in _Stub.seen} == {"microsoft/Phi-4-mini-instruct"}
    assert {r["response_format"]["json_schema"]["name"] for r in _Stub.seen} == {
        "decision_maker_result", "risk_analyst_result", "screener_result", "analyzer_result"}
    assert rec["validation_status"] == "valid" and rec["selected_id"] in PLAN_IDS + [ABSTAIN]
    assert rec["backend_id"] == svc.backend_id and rec["model_revision"] == "rev-pinned"
    assert rec["calibrated_selection_probability"] is None
    # the decision schema sent to the model narrows selected_id to the offered IDs + ABSTAIN
    dec_req = next(r for r in _Stub.seen if r["response_format"]["json_schema"]["name"] == "decision_maker_result")
    assert dec_req["response_format"]["json_schema"]["schema"]["properties"]["selected_id"]["enum"] == PLAN_IDS + [ABSTAIN]
    assert svc.resource_summary()["tokens_out_total"] == 160  # from server-reported usage


def test_decision_off_menu_or_server_error_means_abstain(phi_server):
    cfg, svc = _service(phi_server)
    sel = PhiDecisionSelector(cfg, svc)
    _Stub.decision = "cand_open_long_100x"
    rec = sel.select(snapshot_id="s1", hypothesis="h", facts={}, plans=_plans(), equity=10000, now=NOW,
                     correlation_id="c", ttl_seconds=60)
    assert rec["selected_id"] == ABSTAIN and rec["validation_status"] == "invalid"
    _Stub.decision, _Stub.fail = None, True
    rec = sel.select(snapshot_id="s2", hypothesis="h", facts={}, plans=_plans(), equity=10000, now=NOW,
                     correlation_id="c", ttl_seconds=60)
    assert rec["selected_id"] == ABSTAIN and rec["validation_status"] in ("timeout", "unavailable")


def test_expired_candidate_is_never_offered(phi_server):
    cfg, svc = _service(phi_server)
    plans = _plans()
    plans[0]["expires_at"] = "2025-01-01T00:00:00Z"
    rec = PhiDecisionSelector(cfg, svc).select(snapshot_id="s", hypothesis="h", facts={}, plans=plans, equity=10000,
                                               now=NOW, correlation_id="c", ttl_seconds=60)
    assert rec["selected_id"] == ABSTAIN and "CANDIDATE_EXPIRED" in rec["reason_codes"] and not _Stub.seen


def test_inference_is_serialized_and_risk_work_is_admitted_first(phi_server):
    cfg, svc = _service(phi_server)
    _Stub.delay = 0.15
    done = []

    def call(role, i):
        payload = {"trigger": f"t{i}"} if role == "risk_analyst" else {"stage": "fetch", "i": i}
        svc.run(role, payload, now=NOW, correlation_id=f"c{i}")
        done.append(role)
    threads = [threading.Thread(target=call, args=("screener", i)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)  # one screener job is active, three are waiting
    risk = threading.Thread(target=call, args=("risk_analyst", 99))
    risk.start()
    for t in threads + [risk]:
        t.join(10)
    assert svc.queue.max_active_seen == 1
    order = svc.queue.admission_order
    assert order[0] == "screener" and order[1] == "risk_analyst"  # risk jumps every waiting research job


def test_admission_deadline_and_overload_fail_closed():
    q = InferenceQueue(max_jobs=2, admission_deadline_s=0.2)
    q.acquire("screener")  # holds the only slot
    t0 = time.monotonic()
    with pytest.raises(PhiOverloaded):
        q.acquire("analyzer")  # cannot be admitted within its deadline
    assert time.monotonic() - t0 < 1.0
    q.release()


def test_repair_limit_host_owned_fields_and_context_budget():
    cfg = load_config()
    bad = PhiService(cfg, OpenAICompatiblePhiBackend("http://x", "m", "r", http_post=lambda b, t: {
        "choices": [{"message": {"content": '{"kind": "adjustment_proposal", "event_id": "forged"}'}}]}))
    with pytest.raises(PhiInvalidOutput):
        bad.run("risk_analyst", {"trigger": "t"}, now=NOW, correlation_id="c")
    assert bad.stats["calls"] == 1 + cfg["phi"]["max_schema_repair_retries"]
    svc = PhiService(cfg, FakePhiBackend())
    with pytest.raises(ContextTooLarge):
        svc.run("risk_analyst", {"trigger": "x", "blob": "y" * 20000}, now=NOW, correlation_id="c")


def test_role_cannot_return_another_roles_payload():
    cfg = load_config()
    post = lambda b, t: {"choices": [{"message": {"content": json.dumps({  # noqa: E731
        "kind": "fetch_plan", "request_id": "r", "fetch": [], "reason_codes": ["X"]})}}]}
    svc = PhiService(cfg, OpenAICompatiblePhiBackend("http://x", "m", "r", http_post=post))
    with pytest.raises(PhiFailure):
        svc.run("risk_analyst", {"trigger": "t"}, now=NOW, correlation_id="c")
