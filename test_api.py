"""Offline contract tests with a fake database; no university access required."""
import copy
import importlib.util
import json
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pymysql
from jsonschema import Draft202012Validator
import main


class FakeDB:
    """Small controlled fixture, not a replacement for testing MySQL spatial SQL."""
    def __init__(self):
        self.calls = []
        self.active = True
        self.broken_query = False
        self.closed = 0
        self.rows = [
            dict(date_time=datetime(2022, 11, 23, 5, 28, 10), flight_num="ABC1", gs=Decimal("100"), geo_point=b"binary", geo_point_wkt="POINT(-74.1 41)"),
            dict(date_time=datetime(2022, 11, 23, 5, 28, 15), flight_num="ABC1", gs=Decimal("200"), geo_point=b"binary", geo_point_wkt="POINT(-74.2 41)"),
        ]

    def connect(self):
        return Connection(self)


class Connection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return Cursor(self.db)

    def close(self):
        self.db.closed += 1


class Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.db.calls.append((sql, params))
        if "SELECT username" in sql:
            self.rows = [{"username": "pappal"}] if self.db.active else []
        elif "ST_IsValid" in sql:
            self.rows = [{"valid": int(params[0] not in (
                "POLYGON((0 0,1 1,2 2,0 0))", "POLYGON((0 0,1 1,0 1,1 0,0 0))"))}]
        elif "class_datapoints" in sql:
            if self.db.broken_query:
                raise pymysql.OperationalError(2013, "Simulated failure")
            if "COUNT(*)" in sql:
                self.rows = [{"count": 2}]
            elif "AVG(gs)" in sql:
                selected = self.db.rows
                if "date_time >=" in sql:
                    selected = [r for r in selected if r["date_time"] >= params[0]]
                if "date_time <=" in sql:
                    selected = [r for r in selected if r["date_time"] <= params[-1]]
                self.rows = ([{"flight_num": "ABC1", "avg_speed": sum(r["gs"] for r in selected)/len(selected)}] if selected else [])
            else:
                self.rows = copy.deepcopy([r for r in self.db.rows if params[0] <= r["date_time"] <= params[1]])[:500]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class APITests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.patcher = patch.object(main, "connect_db", self.db.connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.app = main.create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.schemas = json.loads(Path(__file__).with_name("response-schemas.json").read_text())

    def get(self, path, **params):
        return self.client.get(path, query_string={"key": "123", **params})

    def test_data_inclusive_dates_and_serialization(self):
        res = self.get("/getData", start="2022-11-23 05:28:10", end="2022-11-23 05:28:15")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        Draft202012Validator(self.schemas["getData"]).validate(body)
        self.assertEqual(body["num_results"], 2)
        self.assertEqual(body["results"][0]["gs"], 100.0)
        self.assertEqual(body["results"][0]["geo_point"], "POINT(-74.1 41)")
        query, params = next(c for c in self.db.calls if "SELECT d.*" in c[0])
        self.assertIn("BETWEEN %s AND %s", query)
        self.assertIn("ORDER BY d.date_time LIMIT 500", query)
        self.assertEqual(len(params), 2)
        self.assertEqual(self.db.closed, 2)

    def test_average_all_four_date_combinations(self):
        for params in ({}, {"start":"2022-11-23 05:00:00"}, {"end":"2022-11-23 08:00:00"}, {"start":"2022-11-23 05:00:00","end":"2022-11-23 08:00:00"}):
            with self.subTest(params=params):
                body = self.get("/getAvgSpeedByFlight", **params).get_json()
                Draft202012Validator(self.schemas["getAvgSpeedByFlight"]).validate(body)
                self.assertEqual(body["results"][0]["avg_speed"], 150)
                sql, values = next(c for c in reversed(self.db.calls) if "AVG(gs)" in c[0])
                self.assertEqual(len(values), len(params))
                self.assertIn("GROUP BY flight_num ORDER BY flight_num LIMIT 500", sql)

    def test_polygon_parameterization(self):
        res = self.get("/countInPolygon", polygon="-74 42,-75 39,-74 41,-74 42")
        self.assertEqual(res.status_code, 200)
        Draft202012Validator(self.schemas["countInPolygon"]).validate(res.get_json())
        sql, params = next(c for c in self.db.calls if "COUNT(*)" in c[0])
        self.assertIn("ST_Within", sql)
        self.assertNotIn("-74", sql)
        self.assertEqual(params, ("POLYGON((-74 42,-75 39,-74 41,-74 42))",))

    def test_auth_all_routes_and_logging(self):
        for path in ("/getData", "/countInPolygon", "/getAvgSpeedByFlight", "/unknown"):
            for key in (None, "wrong", "123' OR 1=1 --"):
                with self.subTest(path=path, key=key):
                    res = self.client.get(path, query_string={} if key is None else {"key":key})
                    self.assertEqual(res.status_code, 401)
                    self.assertEqual(res.json["code"], 0)
        self.assertFalse(any("class_datapoints" in sql for sql, _ in self.db.calls))
        self.assertEqual(sum("INSERT INTO `pappal_api_logs`" in sql for sql, _ in self.db.calls), 12)

    def test_inactive_key(self):
        self.db.active = False
        self.assertEqual(self.get("/getAvgSpeedByFlight").status_code, 401)

    def test_invalid_dates(self):
        for start, end in (("2022-02-30 00:00:00", "2022-11-23 00:00:00"), ("bad", "bad"), ("2022-11-24 00:00:00", "2022-11-23 00:00:00")):
            self.assertEqual(self.get("/getData", start=start, end=end).status_code, 400)
        self.assertEqual(self.get("/getData").status_code, 400)

    def test_invalid_polygons(self):
        for polygon in ("", "0 0,1 1,2 2", "0 0,1 1,2 2,0 0", "0 0,1 1,0 1,1 0,0 0", "nan 0,1 1,2 2,nan 0", "0 0,181 1,2 2,0 0", "0 0,1 0,1 1,0 0));DROP TABLE x"):
            with self.subTest(polygon=polygon):
                self.assertEqual(self.get("/countInPolygon", polygon=polygon).status_code, 400)

    def test_method_unknown_and_duplicate(self):
        for res, code in ((self.get("/unknown"),404), (self.client.post("/getData?key=123"),405), (self.client.get("/getData?key=123&key=wrong"),400)):
            self.assertEqual(res.status_code, code)
            Draft202012Validator(self.schemas["error"]).validate(res.json)

    def test_database_failure_is_json_and_logged(self):
        self.db.broken_query = True
        res = self.get("/getAvgSpeedByFlight")
        self.assertEqual(res.status_code, 503)
        Draft202012Validator(self.schemas["error"]).validate(res.json)
        log = [p for sql,p in self.db.calls if "INSERT INTO `pappal_api_logs`" in sql][-1]
        self.assertEqual(log[6:8], (503, 0))

    def test_log_columns_and_redaction(self):
        self.get("/getAvgSpeedByFlight")
        log = [p for sql,p in self.db.calls if "INSERT INTO `pappal_api_logs`" in sql][-1]
        self.assertEqual(log[1], "GET")
        self.assertEqual(log[2], "/getAvgSpeedByFlight")
        self.assertEqual(log[3], main.KEY_HASH)
        self.assertNotIn("key", json.loads(log[5]))
        self.assertEqual(log[6:8], (200, 1))

    def test_log_outage_keeps_response_and_writes_fallback(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            with patch.object(main, "BASE_DIR", Path(folder)), patch.object(main, "connect_db", side_effect=[self.db.connect(), pymysql.OperationalError(2003, "down")]):
                res = self.get("/getAvgSpeedByFlight")
                self.assertEqual(res.status_code, 200)
                record = json.loads((Path(folder)/"api-fallback.jsonl").read_text())
                self.assertEqual(record["http_status"], 200)
                self.assertEqual(record["endpoint"], "/getAvgSpeedByFlight")

    def test_timing_includes_both_polygon_queries(self):
        with patch.object(main.time, "time", side_effect=[10,10.25,11,11.5]):
            res = self.get("/countInPolygon", polygon="-74 42,-75 39,-74 41,-74 42")
        self.assertEqual(res.json["sqltime"], 0.75)

    def test_setup_does_not_modify_shared_data(self):
        main.init_db()
        self.assertFalse(any("class_datapoints" in sql or "DROP " in sql for sql,_ in self.db.calls))
        self.assertTrue(any("pappal_auth" in sql for sql,_ in self.db.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
