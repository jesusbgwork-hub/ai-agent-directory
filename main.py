from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import sqlite3
import json
import base64
import requests
import logging
from datetime import datetime
import contextlib

# ── Configuration ────────────────────────────────────────────────
WALLET_ADDRESS   = "0x0F330101B2eA5347AEBAF4257eE46e1355d2F953"
USDC_CONTRACT    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NETWORK_ID       = "8453"          # Base mainnet
FACILITATOR_URL  = "https://x402.org/facilitator"
REGISTER_PRICE   = 500000          # 0.50 USDC (6 decimals)
SEARCH_PRICE     = 10000           # 0.01 USDC (6 decimals)
DB_PATH          = "/root/agents.db"

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────
app = FastAPI(title="AI Agent Directory", version="1.0.0",
              description="B2A monetized directory of AI agents. Pay per use via x402.")

# ── Database ─────────────────────────────────────────────────────
@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL UNIQUE,
                description      TEXT NOT NULL,
                payment_endpoint TEXT NOT NULL,
                registered_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint     TEXT    NOT NULL,
                payment_hash TEXT,
                amount_usdc  REAL,
                timestamp    TEXT    NOT NULL,
                status       TEXT
            );
        """)
    logger.info("Database initialized at %s", DB_PATH)

# ── x402 helpers ─────────────────────────────────────────────────
def build_requirements(resource_url: str, amount: int, description: str) -> dict:
    return {
        "version": "1",
        "scheme": "exact",
        "networkId": NETWORK_ID,
        "maxAmountRequired": str(amount),
        "resource": resource_url,
        "description": description,
        "mimeType": "application/json",
        "payTo": WALLET_ADDRESS,
        "maxTimeoutSeconds": 300,
        "asset": USDC_CONTRACT,
        "extra": {"name": "USDC", "version": "2"}
    }

def requirements_header(requirements: dict) -> str:
    return base64.b64encode(json.dumps(requirements).encode()).decode()

def verify_payment(payment_header: str, requirements: dict) -> tuple[bool, str]:
    try:
        payload = json.loads(base64.b64decode(payment_header).decode())
        resp = requests.post(
            f"{FACILITATOR_URL}/verify",
            json={"payment": payload, "paymentRequirements": requirements},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("isValid", False), data.get("txHash", "unverified")
    except Exception as exc:
        logger.error("Payment verification failed: %s", exc)
    return False, ""

def log_tx(endpoint: str, tx_hash: str, amount: float, status: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO transactions (endpoint, payment_hash, amount_usdc, timestamp, status)"
            " VALUES (?, ?, ?, ?, ?)",
            (endpoint, tx_hash, amount, datetime.utcnow().isoformat(), status)
        )

# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    logger.info("Conway Automaton — Agent Directory API is LIVE")

# ── POST /register ────────────────────────────────────────────────
@app.post("/register")
async def register_agent(request: Request):
    resource_url  = "https://agent-directory.life.conway.tech" + str(request.url.path)
    requirements  = build_requirements(resource_url, REGISTER_PRICE,
                                       "Register agent in AI Directory — 0.50 USDC")
    payment_hdr   = request.headers.get("X-PAYMENT")

    if not payment_hdr:
        logger.info("POST /register — 402 issued (no payment header)")
        return JSONResponse(
            status_code=402,
            content={"error": "Payment required", "price_usdc": 0.50},
            headers={"X-PAYMENT-REQUIREMENTS": requirements_header(requirements)}
        )

    valid, tx_hash = verify_payment(payment_hdr, requirements)
    if not valid:
        log_tx("/register", tx_hash, 0.50, "invalid")
        logger.warning("POST /register — invalid payment rejected")
        return JSONResponse(status_code=402, content={"error": "Invalid or insufficient payment"})

    try:
        body             = await request.json()
        name             = body.get("name", "").strip()
        description      = body.get("description", "").strip()
        payment_endpoint = body.get("payment_endpoint", "").strip()

        if not all([name, description, payment_endpoint]):
            raise HTTPException(status_code=400,
                                detail="Required fields: name, description, payment_endpoint")

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO agents (name, description, payment_endpoint, registered_at)"
                " VALUES (?, ?, ?, ?)",
                (name, description, payment_endpoint, datetime.utcnow().isoformat())
            )
            agent_id = cur.lastrowid

        log_tx("/register", tx_hash, 0.50, "success")
        logger.info("REGISTER OK | agent=%s | id=%s | tx=%s", name, agent_id, tx_hash)
        return {"success": True, "agent_id": agent_id,
                "message": f"Agent '{name}' registered. tx: {tx_hash}"}

    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        log_tx("/register", tx_hash, 0.50, "duplicate")
        raise HTTPException(status_code=409, detail="Agent name already registered")
    except Exception as exc:
        logger.error("Register error: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error")

# ── GET /search ───────────────────────────────────────────────────
@app.get("/search")
async def search_agents(request: Request, q: str = ""):
    resource_url  = "https://agent-directory.life.conway.tech" + str(request.url.path)
    requirements  = build_requirements(resource_url, SEARCH_PRICE,
                                       "Search AI Agent Directory — 0.01 USDC")
    payment_hdr   = request.headers.get("X-PAYMENT")

    if not payment_hdr:
        logger.info("GET /search — 402 issued (no payment header)")
        return JSONResponse(
            status_code=402,
            content={"error": "Payment required", "price_usdc": 0.01},
            headers={"X-PAYMENT-REQUIREMENTS": requirements_header(requirements)}
        )

    valid, tx_hash = verify_payment(payment_hdr, requirements)
    if not valid:
        log_tx("/search", tx_hash, 0.01, "invalid")
        return JSONResponse(status_code=402, content={"error": "Invalid or insufficient payment"})

    with get_db() as conn:
        if q:
            rows = conn.execute(
                "SELECT id, name, description, payment_endpoint, registered_at FROM agents"
                " WHERE name LIKE ? OR description LIKE ?",
                (f"%{q}%", f"%{q}%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, description, payment_endpoint, registered_at FROM agents"
            ).fetchall()

    results = [dict(r) for r in rows]
    log_tx("/search", tx_hash, 0.01, "success")
    logger.info("SEARCH OK | q='%s' | results=%d | tx=%s", q, len(results), tx_hash)
    return {"results": results, "count": len(results), "query": q}

# ── GET /health ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    with get_db() as conn:
        agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        row         = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_usdc), 0)"
            " FROM transactions WHERE status='success'"
        ).fetchone()
    tx_count, revenue = row[0], row[1]
    return {
        "status"              : "operational",
        "agents_registered"   : agent_count,
        "total_transactions"  : tx_count,
        "total_revenue_usdc"  : round(revenue, 4),
        "wallet"              : WALLET_ADDRESS,
        "network"             : f"Base (chainId {NETWORK_ID})"
    }

# ── GET / (info) ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service"    : "AI Agent Directory",
        "version"    : "1.0.0",
        "operator"   : "Conway Automaton",
        "endpoints"  : {
            "POST /register" : "Register your agent — 0.50 USDC (x402)",
            "GET /search"    : "Search agents by keyword — 0.01 USDC (x402)",
            "GET /health"    : "Service stats (free)"
        },
        "payment_protocol": "x402",
        "payment_network" : "Base mainnet",
        "wallet"          : WALLET_ADDRESS
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
