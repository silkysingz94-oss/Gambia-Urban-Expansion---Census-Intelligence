# GBoS GEOAI Census Intelligence — Live PostGIS Edition

This repository now contains a live-dashboard foundation backed by PostgreSQL/PostGIS. The dashboard reads census summaries and EA geometries from the database and receives change notifications through PostgreSQL `LISTEN/NOTIFY`.

## Components

- `database/postgis_census_schema.sql` — example PostGIS EA schema, indexes, audit log, change trigger and dashboard view.
- `backend/main.py` — FastAPI read API, health endpoint, spatial GeoJSON endpoint, change log and WebSocket listener.
- `backend/requirements.txt` and `backend/Dockerfile` — containerized API service.
- `backend/.env.example` — environment-variable template. Never commit real database credentials.
- `dashboard/census-live.html` — Leaflet dashboard with filters, KPIs, map and live refresh.

## Data model and field mapping

The starter schema uses one row per EA in `census.ea`:

| Field | Meaning |
|---|---|
| `ea_code` | Stable EA identifier, stored as text |
| `geom` | EA polygon, MultiPolygon, EPSG:4326 |
| `lga`, `district`, `ward` | Administrative hierarchy |
| `population_2013`, `population_2023` | Census population totals |
| `households_2013`, `households_2023` | Census household totals |

This is a proposed integration schema, not a claim that it matches an existing GBoS production database. Map the actual source table/column names before importing official data. Preserve EA codes as text and validate CRS, geometry validity, uniqueness and source totals.

## Database setup

1. Create a PostgreSQL database with PostGIS enabled.
2. Run `database/postgis_census_schema.sql` using a database administrator.
3. Load approved EA boundaries and aggregated census data into `census.ea`. Use a trusted ETL process; do not expose write access to the dashboard.
4. Create a dedicated read-only API account and grant SELECT on the dashboard tables/view and change log. The database listener account needs permission to connect and listen to the notification channel.
5. Keep production data and credentials inside an approved GBoS-managed or otherwise authorized hosting environment.

Example local setup:

```bash
createdb gambia_census
psql -d gambia_census -f database/postgis_census_schema.sql
```

The SQL creates an illustrative schema. It does not import the existing census CSV or GeoJSON automatically.

## Run the API locally

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit DATABASE_URL and CORS_ORIGINS in .env
uvicorn main:app --reload --port 8000
```

Health check: `http://localhost:8000/health`

API endpoints:

- `GET /api/v1/summary`
- `GET /api/v1/filters`
- `GET /api/v1/boundaries?lga=...`
- `GET /api/v1/changes?limit=50`
- `WS /api/v1/live`

Serve `dashboard/census-live.html` from a local static server and set its `?api=` query parameter to the API base URL, e.g. `http://localhost:8080/dashboard/census-live.html?api=http://localhost:8000`.

## Automatic update mechanism

The EA trigger writes an audit event and sends a transactional PostgreSQL notification for each insert, update or delete. The API maintains a dedicated LISTEN connection and forwards events to connected dashboards over WebSocket. The dashboard refreshes KPIs, map features and the recent-change feed after an event. A 60-second refresh is also used as a recovery fallback.

Notifications are event signals, not a durable message queue. The audit table is the durable change history. For production multi-instance deployments, use a dedicated pub/sub broker or consume the audit table by monotonically increasing event ID, and implement per-client WebSocket fan-out and back-pressure.

## Deployment and security

GitHub Pages can host the static HTML only. It cannot run FastAPI or maintain a private PostgreSQL connection. Deploy the API as a separate container/service on an approved host with network access to PostgreSQL, then configure the dashboard API URL.

- Use TLS for API and database connections.
- Keep `DATABASE_URL` in the hosting provider's secret/environment settings, never in HTML, GitHub commits or browser JavaScript.
- Use a least-privilege read-only database role for API queries.
- Put the API and WebSocket behind GBoS-approved authentication/authorization (for example, an identity-aware reverse proxy or institutional SSO). Do not expose the API anonymously with production census data.
- Restrict CORS to the published dashboard origin.
- Do not load or expose identifiable census microdata. This dashboard starter is designed for approved aggregated EA-level statistics.
- Back up PostgreSQL and monitor API, database and notification listener health.
- Confirm GBoS data governance, hosting, access approval and disclosure controls before production use.

## Current implementation scope

This is the integration scaffold and live dashboard shell. It is not connected to a real database yet because no PostgreSQL host, schema, credentials or approved network route has been provided. It must be mapped to the actual GBoS database schema and deployed alongside an authorized PostgreSQL/PostGIS service before it can display live institutional records.

Produced by Ansu Singhateh | GIS Analyst | GIS / Cartography Unit | Gambia Bureau of Statistics

GBoS GEOAI • Research and educational use
