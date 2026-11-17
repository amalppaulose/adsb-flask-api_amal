import json
import math
import pymysql
import time
import datetime
import yaml

from flask import Flask, request, g
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# 1. Read database settings.
with open("config.yml", "r") as file:
    config = yaml.safe_load(file)
    db_config = config["mysql"]

db_config["autocommit"] = True
db_config["connect_timeout"] = 10

# 2. Create authentication and logging tables.
def setup():
    conn = pymysql.connect(**db_config)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pappal_hw_auth (
                    token VARCHAR(50) PRIMARY KEY
                )
            """)

            cur.execute("""
                INSERT IGNORE INTO pappal_hw_auth (token)
                VALUES ('123')
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS pappal_hw_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    endpoint TEXT,
                    token TEXT,
                    response TEXT,
                    dt DATETIME
                )
            """)
    finally:
        conn.close()


# 3. Check the API key.
def auth(token):
    if token != "123":
        return False

    conn = pymysql.connect(**db_config)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT token FROM pappal_hw_auth
                WHERE token = %s
            """, (token,))

            return cur.fetchone() is not None
    finally:
        conn.close()


# 4. Save a request log.
def log(token, endpoint, response):
    conn = pymysql.connect(**db_config)

    try:
        with conn.cursor() as cur:
            sql = """
                INSERT INTO pappal_hw_logs
                (endpoint, token, response, dt)
                VALUES (%s, %s, %s, UTC_TIMESTAMP())
            """

            cur.execute(sql, (endpoint, token, response))
    finally:
        conn.close()


# 5. Run a query and measure SQL time.
def run_query(sql, values=()):
    conn = pymysql.connect(**db_config)

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            start = time.time()

            try:
                cur.execute(sql, values)
                return cur.fetchall()
            finally:
                g.sqltime += max(0, time.time() - start)
    finally:
        conn.close()


# 6. Return JSON with the required fields.
def send_response(res, status=200):
    res["req"] = request.path.strip("/")
    res["sqltime"] = round(getattr(g, "sqltime", 0), 6)

    return app.response_class(
        json.dumps(res, default=str, indent=4),
        status=status,
        mimetype="application/json"
    )


# 7. Read and check dates.
def get_dates(required=False):
    start = request.args.get("start")
    end = request.args.get("end")

    if required and (start is None or end is None):
        raise ValueError("start and end are required")

    dates = []

    for value in (start, end):
        if value is None:
            dates.append(None)
        else:
            try:
                parsed = datetime.datetime.strptime(
                    value, "%Y-%m-%d %H:%M:%S"
                )
                dates.append(parsed)
            except ValueError:
                raise ValueError(
                    "Use a valid date in YYYY-MM-DD HH:MM:SS format"
                ) from None

    if dates[0] is not None and dates[1] is not None:
        if dates[0] > dates[1]:
            raise ValueError("start must not be later than end")

    return dates[0], dates[1]


# 8. Authenticate all requests.
@app.before_request
def check_auth():
    g.sqltime = 0

    for name in request.args:
        if len(request.args.getlist(name)) > 1:
            return send_response(
                {"code": 0, "msg": "Query parameters must not be repeated"},
                400
            )

    if not auth(request.args.get("key")):
        return send_response(
            {"code": 0, "msg": "Invalid or missing key"},
            401
        )


# 9. Get datapoints between two dates.
@app.route("/getData")
def getData():
    start, end = get_dates(required=True)

    sql = """
        SELECT *, ST_AsText(geo_point) AS point_text
        FROM class_datapoints
        WHERE date_time BETWEEN %s AND %s
        ORDER BY date_time
        LIMIT 500
    """

    results = run_query(sql, (start, end))

    # Replace binary geometry with readable text.
    for row in results:
        row["geo_point"] = row.pop("point_text")

    res = {}
    res["code"] = 1
    res["msg"] = "Data retrieved"
    res["results"] = results
    res["num_results"] = len(results)

    return send_response(res)


# 10. Count datapoints inside a polygon.
@app.route("/countInPolygon")
def countInPolygon():
    polygon = request.args.get("polygon")

    if not polygon:
        raise ValueError("polygon is required")

    # Check that every point contains two valid numbers.
    points = []

    try:
        for point in polygon.split(","):
            x, y = map(float, point.split())

            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError()

            if not (-180 <= x <= 180 and -90 <= y <= 90):
                raise ValueError()

            points.append((x, y))

    except ValueError:
        raise ValueError(
            "Use valid longitude latitude pairs separated by commas"
        ) from None

    # The last point must repeat the first point.
    if len(points) < 4 or points[0] != points[-1]:
        raise ValueError(
            "Polygon needs at least four points, "
            "with the first and last matching"
        )

    if len(set(points[:-1])) < 3:
        raise ValueError(
            "Polygon needs at least three distinct vertices"
        )

    polygon = "POLYGON((" + ",".join(
        f"{x} {y}" for x, y in points
    ) + "))"

    # Check for invalid shapes, such as crossing edges.
    valid = run_query("""
        SELECT ST_IsValid(ST_GeomFromText(%s, 0)) AS valid
    """, (polygon,))

    if not valid[0]["valid"]:
        raise ValueError("Invalid polygon")

    sql = """
        SELECT COUNT(*) AS count
        FROM class_datapoints
        WHERE ST_Within(
            geo_point,
            ST_GeomFromText(%s, 0)
        )
    """

    results = run_query(sql, (polygon,))

    res = {}
    res["code"] = 1
    res["msg"] = "Points counted"
    res["count"] = results[0]["count"]

    return send_response(res)


# 11. Get average speed for each flight.
@app.route("/getAvgSpeedByFlight")
def getAvgSpeedByFlight():
    start, end = get_dates()

    sql = """
        SELECT flight_num, AVG(gs) AS avg_speed
        FROM class_datapoints
        WHERE 1 = 1
    """

    values = []

    if start is not None:
        sql += " AND date_time >= %s"
        values.append(start)

    if end is not None:
        sql += " AND date_time <= %s"
        values.append(end)

    sql += " GROUP BY flight_num ORDER BY flight_num LIMIT 500"

    results = run_query(sql, values)

    for row in results:
        if row["avg_speed"] is not None:
            row["avg_speed"] = float(row["avg_speed"])

    res = {}
    res["code"] = 1
    res["msg"] = "Average speeds retrieved"
    res["results"] = results
    res["num_results"] = len(results)

    return send_response(res)


# 12. Return errors in JSON format.
@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, ValueError):
        message = str(error)
        status = 400
    elif isinstance(error, HTTPException):
        message = error.description
        status = error.code
    else:
        print(error)
        message = "Server or database error. Check the terminal."
        status = 500

    return send_response(
        {"code": 0, "msg": message},
        status
    )


# 13. Log successful and failed requests.
@app.after_request
def save_log(response):
    try:
        log(
            request.args.get("key"),
            request.path,
            response.get_data(as_text=True)
        )
    except Exception as error:
        print("Logging failed:", error)

    return response


# 14. Start the API.
if __name__ == "__main__":
    setup()
    app.run(host="127.0.0.1", port=5000, debug=False)