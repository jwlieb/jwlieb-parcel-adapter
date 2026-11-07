"""HTTP adapter compatibility repair with actual httpx mock requests and trace capture."""

from plugin.worker import run

STARTER = {
    "adapter.py": """def fetch_shipments(client, customer_id, pause):
    response = client.get("/v1/shipments", params={"customer_id": customer_id})
    response.raise_for_status()
    return response.json()["shipments"]
"""
}
RESULT_FILES = []
SPEC = """Repair only adapter.py, exposing fetch_shipments(client, customer_id, pause). The frozen public GET /shipments?customer_id=... endpoint will keep its response {"shipments":[{"id":str,"status":str,"eta":str|null}]}.
Upstream v2: GET /v2/shipments with query customer=<customer_id>, plus cursor=<opaque token> after the first page. JSON {"data":[{"shipment_id":str,"tracking":{"state":"created"|"in_transit"|"delivered","eta":str|null}}],"next_cursor":str|null}. Preserve page/record order. Map created->pending, in_transit->moving, delivered->delivered. Require well-formed fields, IDs, states, eta, and cursor; repeated cursors must fail, not loop.
Retry only GET responses 429 or 503, at most 3 attempts per page. Between retries call pause(seconds): use nonnegative integer Retry-After when supplied, otherwise 1. Never sleep in real time. Exhausted retries will raise RuntimeError (public 503 {"error":"upstream_unavailable"}); malformed data, unknown state, repeated cursor or other upstream errors will raise ValueError (public 502 {"error":"bad_upstream"}); 404 will raise LookupError (public 404 {"error":"customer_not_found"}). Empty data with next_cursor is legal. The client is a real httpx.Client with a pinned MockTransport; no external network is needed. Do not import libraries, access files, change routes/dependencies, monkeypatch, or use reflection. Runtime traces will check actual v2 paths, query parameters, pagination, attempts and virtual delays. Submit assess's output exactly."""


def fixture(scenario, seed):
    rows = [
        {
            "shipment_id": f"S-{seed}-{i}",
            "tracking": {
                "state": ["created", "in_transit", "delivered"][i % 3],
                "eta": None if i % 2 else "2026-11-20",
            },
        }
        for i in range(5)
    ]
    if scenario == "empty":
        rows = []
    modes = ["normal", "retry", "malformed", "cycle", "not_found", "exhausted", "empty_middle"]
    if scenario == "boundary":
        rows += [{"shipment_id": f"Unicode-{seed}-é", "tracking": {"state": "delivered", "eta": None}}]
    if scenario == "invalid":
        modes += ["unknown_state", "bad_cursor"]
    return {"scenario": scenario, "seed": seed, "customer_id": f"C-{seed}", "shipments": rows, "modes": modes}


def measure(files, raw, workspace):
    return run(
        files["adapter.py"],
        {"fixture": raw},
        """
import httpx
fixture = request["fixture"]
""",
        """
answers = []
traces = []
delays_all = []
for mode in fixture["modes"]:
    calls = []
    delays = []
    attempts = {}
    def handler(req):
        calls.append({"path":req.url.path,"query":dict(req.url.params)})
        cursor = req.url.params.get("cursor", "")
        attempts[cursor] = attempts.get(cursor,0) + 1
        if req.url.path != "/v2/shipments" or req.url.params.get("customer") != fixture["customer_id"]:
            return httpx.Response(400,json={"error":"incorrect endpoint"})
        if mode == "not_found":
            return httpx.Response(404)
        if mode == "exhausted":
            return httpx.Response(503)
        if mode == "retry" and not cursor and attempts[cursor] < 3:
            return httpx.Response(429 if attempts[cursor] == 1 else 503,headers={"Retry-After":"2"} if attempts[cursor] == 1 else {})
        if mode == "malformed":
            return httpx.Response(200,json={"data":"not a list","next_cursor":None})
        if mode == "unknown_state":
            return httpx.Response(200,json={"data":[{"shipment_id":"x","tracking":{"state":"teleported","eta":None}}],"next_cursor":None})
        if mode == "bad_cursor":
            return httpx.Response(200,json={"data":[],"next_cursor":12})
        if mode == "cycle":
            return httpx.Response(200,json={"data":[],"next_cursor":"repeat"})
        rows = fixture["shipments"]
        pages = [rows[:2],rows[2:4],rows[4:]] if rows else [[]]
        if mode == "empty_middle":
            pages = [rows[:2],[],rows[2:]]
        index = 0 if not cursor else int(cursor.split("-")[-1])
        if index >= len(pages):
            return httpx.Response(400)
        return httpx.Response(200,json={"data":pages[index],"next_cursor":f"opaque-{index+1}" if index+1<len(pages) else None})
    client = httpx.Client(base_url="https://upstream.invalid",transport=httpx.MockTransport(handler))
    try:
        data = namespace["fetch_shipments"](client,fixture["customer_id"],delays.append)
        answer = {"status":200,"body":{"shipments":data}}
    except LookupError:
        answer = {"status":404,"body":{"error":"customer_not_found"}}
    except RuntimeError:
        answer = {"status":503,"body":{"error":"upstream_unavailable"}}
    except (ValueError,TypeError,KeyError,httpx.HTTPError):
        answer = {"status":502,"body":{"error":"bad_upstream"}}
    client.close()
    answers.append(answer)
    traces.append(calls)
    delays_all.append(delays)
print(json.dumps({"responses":answers,"traces":traces,"delays":delays_all}))
""",
    )
