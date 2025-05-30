from fastapi import FastAPI, Depends, HTTPException, Header, status
from pydantic import BaseModel
from jose import JWTError, jwt
from clickhouse_driver import Client
import os, time, datetime

# ------------------------------------------------------------------
# Environment / secrets
# ------------------------------------------------------------------
JWT_SECRET        = os.getenv("AUTH_SHARED_SECRET")         # eg. a long random string
JWT_ALGORITHM     = "HS256"
RAW_SHARED_TOKEN  = os.getenv("AUTH_STATIC_TOKEN")          # optional fallback
CLICKHOUSE_HOST   = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_DB     = os.getenv("CLICKHOUSE_DB", "metrics")
CLICKHOUSE_USER   = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASS   = os.getenv("CLICKHOUSE_PASSWORD", "")

# ------------------------------------------------------------------
# ClickHouse connection
# ------------------------------------------------------------------
ch = Client(host=CLICKHOUSE_HOST,
            database=CLICKHOUSE_DB,
            user=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASS)

# Create table if it doesn’t exist
ch.execute("""
CREATE TABLE IF NOT EXISTS container_metrics (
    ts             DateTime,
    cluster        String,
    node           String,
    namespace      String,
    pod            String,
    container      String,
    cpu_usage_sec  Float64,
    mem_usage_b    UInt64
) ENGINE = MergeTree()
ORDER BY (ts, cluster, node, namespace, pod, container)
""")

# ------------------------------------------------------------------
# FastAPI setup
# ------------------------------------------------------------------
app = FastAPI()

# -------------- models --------------
class AuthRequest(BaseModel):
    username: str
    password: str

class MetricsSample(BaseModel):
    ts: float                     # epoch seconds
    cluster: str
    node: str
    namespace: str
    pod: str
    container: str
    cpu_usage_sec: float
    mem_usage_b: int

class MetricsPayload(BaseModel):
    records: list[MetricsSample]

# -------------- auth helpers --------------
def verify_jwt_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")  # username
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token")

def auth_guard(authorization: str = Header(...)):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bad auth header")
    # Static-token path (optional)
    if RAW_SHARED_TOKEN and token == RAW_SHARED_TOKEN:
        return "static-client"
    return verify_jwt_token(token)

# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@app.post("/auth")
def authenticate(req: AuthRequest):
    # You can validate username/password however you like
    if req.username != os.getenv("SCRAPER_USER") \
       or req.password != os.getenv("SCRAPER_PASS"):
        raise HTTPException(status_code=401, detail="Bad credentials")

    exp = int(time.time()) + 60*60  # 1 h
    to_encode = {"sub": req.username, "exp": exp}
    token = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "expires_in": 3600}

@app.post("/ingest")
def ingest(payload: MetricsPayload, client_id: str = Depends(auth_guard)):
    # Transform records to ClickHouse rows
    rows = [
        (datetime.datetime.fromtimestamp(r.ts),
         r.cluster, r.node, r.namespace, r.pod, r.container,
         r.cpu_usage_sec, r.mem_usage_b)
        for r in payload.records
    ]
    ch.execute(
        "INSERT INTO container_metrics VALUES",
        rows,
        types_check=True,
    )
    return {"inserted": len(rows)}
