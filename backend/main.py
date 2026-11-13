import asyncio
import json
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:8080").split(",") if x.strip()]
API_TOKEN = os.getenv("API_BEARER_TOKEN", "").strip()
db_pool = None
event_queue = queue.Queue(maxsize=1000)
listener_stop = threading.Event()
listener_thread = None

def db():
    if db_pool is None:
        raise RuntimeError("Database pool is not ready")
    return db_pool.getconn()

def release(conn):
    if conn is not None and db_pool is not None:
        db_pool.putconn(conn)

def require_token(authorization: Optional[str]):
    # Empty token means the API is intended to sit behind a private network or
    # an identity-aware access gateway. Do not expose an unprotected API publicly.
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def listen_for_changes():
    while not listener_stop.is_set():
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("LISTEN census_data_changed;")
            while not listener_stop.is_set():
                if select_readable(conn, 1.0):
                    conn.poll()
                    while conn.notifies:
                        note = conn.notifies.pop(0)
                        try:
                            event_queue.put_nowait(json.loads(note.payload))
                        except queue.Full:
                            # A full queue triggers a refresh marker instead of
                            # silently pretending all incremental events were delivered.
                            try: event_queue.get_nowait()
                            except queue.Empty: pass
                            try: event_queue.put_nowait({"type":"refresh_required"})
                            except queue.Full: pass
        except Exception:
            time.sleep(3)
        finally:
            if conn:
                conn.close()

def select_readable(conn, timeout):
    import select
    ready, _, _ = select.select([conn], [], [], timeout)
    return bool(ready)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, listener_thread
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 12, DATABASE_URL)
    listener_stop.clear()
    listener_thread = threading.Thread(target=listen_for_changes, daemon=True)
    listener_thread.start()
    yield
    listener_stop.set()
    if listener_thread:
        listener_thread.join(timeout=2)
    db_pool.closeall()

app = FastAPI(title="GBoS GEOAI Census API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["GET"], allow_headers=["Authorization","Content-Type"])

@app.get("/health")
def health():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), PostGIS_version(), now()")
            row = cur.fetchone()
        return {"status":"connected","database":row[0],"postgis":row[1],"server_time":row[2].isoformat()}
    except Exception as exc:
        raise HTTPException(503, f"Database health check failed: {exc}")
    finally:
        release(conn)

@app.get("/api/v1/summary")
def summary(authorization: Optional[str] = Header(default=None)):
    require_token(authorization)
    conn=db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
              SELECT COUNT(*)::int AS ea_count,
                COUNT(*) FILTER (WHERE geom IS NOT NULL)::int AS mapped_eas,
                COALESCE(SUM(population_2013),0)::bigint AS population_2013,
                COALESCE(SUM(population_2023),0)::bigint AS population_2023,
                COALESCE(SUM(households_2013),0)::bigint AS households_2013,
                COALESCE(SUM(households_2023),0)::bigint AS households_2023,
                COUNT(*) FILTER (WHERE population_2023 IS NULL)::int AS missing_population,
                COUNT(*) FILTER (WHERE households_2023 IS NULL)::int AS missing_households,
                MAX(updated_at) AS last_updated
              FROM census.ea
            """)
            row=dict(cur.fetchone())
            if row["population_2013"]:
                row["population_change_pct"]=round((row["population_2023"]-row["population_2013"])*100/row["population_2013"],2)
            else: row["population_change_pct"]=None
            row["last_updated"]=row["last_updated"].isoformat() if row["last_updated"] else None
            return row
    finally: release(conn)

@app.get("/api/v1/boundaries")
def boundaries(lga: Optional[str]=None, district: Optional[str]=None, ward: Optional[str]=None,
               authorization: Optional[str]=Header(default=None)):
    require_token(authorization)
    conn=db()
    try:
        clauses=["geom IS NOT NULL"]; args=[]
        for col,val in (("lga",lga),("district",district),("ward",ward)):
            if val:
                clauses.append(f"{col} = %s"); args.append(val)
        sql=f"""
          SELECT json_build_object('type','FeatureCollection','features',
            COALESCE(json_agg(json_build_object(
              'type','Feature','geometry',ST_AsGeoJSON(geom)::json,
              'properties',json_build_object(
                'ea_code',ea_code,'ea_name',ea_name,'lga',lga,'district',district,'ward',ward,
                'population_2013',population_2013,'population_2023',population_2023,
                'households_2013',households_2013,'households_2023',households_2023,
                'population_change_pct',population_change_pct,
                'average_household_size_2023',average_household_size_2023,
                'updated_at',updated_at))), '[]'::json))
          FROM census.ea_dashboard WHERE {' AND '.join(clauses)}
        """
        with conn.cursor() as cur:
            cur.execute(sql,args); return cur.fetchone()[0]
    finally: release(conn)

@app.get("/api/v1/filters")
def filters(authorization: Optional[str]=Header(default=None)):
    require_token(authorization)
    conn=db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result={}
            for col in ("lga","district","ward"):
                cur.execute(f"SELECT DISTINCT {col} AS value FROM census.ea WHERE {col} IS NOT NULL ORDER BY {col}")
                result[col]=[r["value"] for r in cur.fetchall()]
            return result
    finally: release(conn)

@app.get("/api/v1/changes")
def changes(limit:int=Query(50,ge=1,le=500), authorization: Optional[str]=Header(default=None)):
    require_token(authorization)
    conn=db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT event_id,table_name,record_key,operation,changed_at,changed_by,changed_fields
              FROM census.change_log ORDER BY event_id DESC LIMIT %s""",(limit,))
            rows=[]
            for r in cur.fetchall():
                r=dict(r); r["changed_at"]=r["changed_at"].isoformat(); rows.append(r)
            return rows
    finally: release(conn)

@app.websocket("/api/v1/live")
async def live(websocket: WebSocket):
    # For protected deployments, terminate WebSocket auth at an identity-aware
    # reverse proxy. Do not place database credentials or privileged tokens in HTML.
    await websocket.accept()
    try:
        await websocket.send_json({"type":"connected","message":"Listening for census database changes"})
        while True:
            try:
                event=await asyncio.to_thread(event_queue.get, True, 20)
                await websocket.send_json({"type":"database_change","event":event})
            except queue.Empty:
                await websocket.send_json({"type":"heartbeat","at":time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        try: await websocket.close()
        except Exception: pass
