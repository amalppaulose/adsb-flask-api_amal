"""HTTP integration tests: start main.py first, then run this file."""
import argparse
import json
import sys
from pathlib import Path

import requests
from jsonschema import Draft202012Validator

START = "2022-11-23 05:28:10"
END = "2022-11-23 05:28:15"
POLYGON = "-74 42,-75 39,-74 41,-74 42"


def run_tests(base_url):
    schemas = json.loads((Path(__file__).resolve().parent / "response-schemas.json").read_text())
    cases = [
        ("getData", {"start": START, "end": END}, 200),
        ("getData", {"start": START, "end": START}, 200),
        ("getData", {"start": "1900-01-01 00:00:00", "end": "1900-01-01 00:00:01"}, 200),
        ("countInPolygon", {"polygon": POLYGON}, 200),
        ("getAvgSpeedByFlight", {"start": "2022-11-23 05:00:00", "end": "2022-11-23 08:00:00"}, 200),
        ("getAvgSpeedByFlight", {}, 200),
        ("getAvgSpeedByFlight", {"start": START}, 200),
        ("getAvgSpeedByFlight", {"end": END}, 200),
        ("getData", {}, 400),
        ("getData", {"start": START}, 400),
        ("getData", {"end": END}, 400),
        ("getData", {"start": END, "end": START}, 400),
        ("getData", {"start": "2022-02-30 00:00:00", "end": END}, 400),
        ("getData", {"start": "not-a-date", "end": END}, 400),
        ("getAvgSpeedByFlight", {"start": ""}, 400),
        ("getAvgSpeedByFlight", {"start": END, "end": START}, 400),
        ("countInPolygon", {}, 400),
        ("countInPolygon", {"polygon": "-74 42,-75 39,-74 41"}, 400),
        ("countInPolygon", {"polygon": "0 0,1 1,2 2,0 0"}, 400),
        ("countInPolygon", {"polygon": "0 0,1 1,0 1,1 0,0 0"}, 400),
        ("countInPolygon", {"polygon": "nan 0,1 0,1 1,nan 0"}, 400),
        ("countInPolygon", {"polygon": "0 0,1 0,1 1,0 0)); DROP TABLE x;--"}, 400),
        ("missingEndpoint", {}, 404),
    ]
    # Authentication must protect every endpoint, not just getData.
    for endpoint in ("getData", "countInPolygon", "getAvgSpeedByFlight"):
        for key in (None, "wrong", "123' OR '1'='1"):
            cases.append((endpoint, {"key": key}, 401))
    failures = 0
    with requests.Session() as session:
        for index, (endpoint, params, expected) in enumerate(cases, 1):
            try:
                args = {"key": "123", **params}
                res = session.get(base_url.rstrip("/") + "/" + endpoint,
                                  params=args, timeout=90)
                body = res.json()
                assert res.status_code == expected, (res.status_code, body)
                assert body["code"] == (1 if expected == 200 else 0), body
                assert body["req"] == endpoint, body
                schema_name = endpoint if expected == 200 else "error"
                Draft202012Validator(schemas[schema_name]).validate(body)
                if "results" in body:
                    assert len(body["results"]) == body["num_results"] <= 500
                if endpoint == "getData" and expected == 200:
                    dates = [row["date_time"] for row in body["results"]]
                    assert dates == sorted(dates)
                    assert all(args["start"] <= dt <= args["end"] for dt in dates)
                    if args["start"].startswith("1900"):
                        assert body["results"] == []
                if endpoint == "getAvgSpeedByFlight" and expected == 200:
                    flights = [row["flight_num"] for row in body["results"]]
                    assert len(set(flights)) == len(flights)
                print(f"PASS {index:02d} {endpoint} HTTP {expected}")
            except Exception as error:
                failures += 1
                print(f"FAIL {index:02d} {endpoint}: {error}")
        # Method rejection and duplicate parameters are separate HTTP tests.
        extras = [
            ("POST method", session.post, "/getData", {"key": "123"}, 405),
            ("Duplicate key", session.get, "/getData", [("key", "123"), ("key", "wrong")], 400),
        ]
        for label, method, path, params, expected in extras:
            try:
                res = method(base_url.rstrip("/") + path, params=params, timeout=90)
                assert res.status_code == expected, res.text
                Draft202012Validator(schemas["error"]).validate(res.json())
                print("PASS", label)
            except Exception as error:
                failures += 1
                print("FAIL", label, error)
    total = len(cases) + len(extras)
    print(f"\n{total - failures}/{total} passed")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    args = parser.parse_args()
    sys.exit(1 if run_tests(args.base_url) else 0)
