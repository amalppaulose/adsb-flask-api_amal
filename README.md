# ADSB Flask API

This project uses Flask to access flight data from the MySQL `class_datapoints` table.

It has three endpoints:

- `/getData`: returns up to 500 datapoints between two dates.
- `/countInPolygon`: counts datapoints inside a polygon.
- `/getAvgSpeedByFlight`: returns average speed by flight, with optional date filters.

All requests require `key=123`. Authentication and request logs use the `pappal_auth` and `pappal_api_logs` tables.

To run, install the packages in `requirements.txt`, add your database settings to `config.yml`, and run `main.py`. Then run `test-client.py` in another terminal.

Response schemas are in `response-schemas.json`. The database password is not included in this repository.