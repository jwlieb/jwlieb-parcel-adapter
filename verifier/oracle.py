"""Independent public contract and expected protocol traces from raw upstream records."""


def expected(raw):
    answers, traces, delays = [], [], []
    customer = raw["customer_id"]
    mapping = {"created": "pending", "in_transit": "moving", "delivered": "delivered"}
    rows = [
        {"id": r["shipment_id"], "status": mapping[r["tracking"]["state"]], "eta": r["tracking"]["eta"]}
        for r in raw["shipments"]
    ]

    def request(cursor=None):
        query = {"customer": customer}
        if cursor is not None:
            query["cursor"] = cursor
        return {"path": "/v2/shipments", "query": query}

    for mode in raw["modes"]:
        if mode in ("malformed", "unknown_state", "bad_cursor", "cycle"):
            answer = {"status": 502, "body": {"error": "bad_upstream"}}
        elif mode == "not_found":
            answer = {"status": 404, "body": {"error": "customer_not_found"}}
        elif mode == "exhausted":
            answer = {"status": 503, "body": {"error": "upstream_unavailable"}}
        else:
            answer = {"status": 200, "body": {"shipments": rows}}
        if mode == "cycle":
            trace = [request(), request("repeat")]
        elif mode == "exhausted":
            trace = [request()] * 3
        elif mode in ("malformed", "unknown_state", "bad_cursor", "not_found"):
            trace = [request()]
        else:
            count = 3 if raw["shipments"] or mode == "empty_middle" else 1
            trace = [request()] + [request(f"opaque-{i}") for i in range(1, count)]
            if mode == "retry":
                trace = [request(), request()] + trace
        answers.append(answer)
        traces.append(trace)
        delays.append([2, 1] if mode == "retry" else [1, 1] if mode == "exhausted" else [])
    return {"responses": answers, "traces": traces, "delays": delays}
