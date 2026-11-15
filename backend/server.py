import asyncio
import os
import uuid
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Query, Depends
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel

from arpg_email import send_welcome_email
from build_engine import plan_build, generate_part, count_artifacts

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

TRIAL_DAYS = 7
SESSION_DAYS = 7
CREATOR_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "CREATOR_EMAILS",
        "gabrielediconza014@gmail.com,Ahmadelmaiden@gmail.com",
    ).split(",")
    if e.strip()
]

INDEXES: dict[str, list[IndexModel]] = {
    "users": [IndexModel([("email", ASCENDING)], name="email", unique=True)],
    "sessions": [IndexModel([("session_token", ASCENDING)], name="session_token", unique=True)],
    "builds": [IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_created")],
    "studio_keys": [
        IndexModel([("key", ASCENDING)], name="key", unique=True),
        IndexModel([("user_id", ASCENDING)], name="user_id", unique=True),
    ],
    "payment_transactions": [IndexModel([("session_id", ASCENDING)], name="session_id")],
}


async def ensure_indexes() -> None:
    for collection, models in INDEXES.items():
        for model in models:
            try:
                await db[collection].create_indexes([model])
            except Exception as exc:
                logging.getLogger(__name__).error("ensure_indexes(%s): %s", collection, exc)


app = FastAPI()
api_router = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- helpers ----------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def _tz(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def access_info(user: dict) -> dict:
    now = now_utc()
    if user.get("is_creator"):
        return {"can_build": True, "mode": "creator", "message": "Accesso totale Creator: tutto gratis, per sempre."}
    status = user.get("subscription_status")
    period_end = _tz(user.get("subscription_period_end"))
    if status in ("active", "trialing", "canceling") and (not period_end or now < period_end):
        if status == "trialing":
            days_left = max(1, (period_end - now).days + 1) if period_end else 7
            return {
                "can_build": True,
                "mode": "trial",
                "trial_end": iso(period_end) if period_end else None,
                "days_left": days_left,
                "message": f"Prova gratuita attiva: {days_left} giorni rimasti, poi 0,99 €/mese.",
            }
        if status == "canceling":
            return {"can_build": True, "mode": "canceling", "message": "Abbonamento disdetto: resti attivo fino alla fine del periodo."}
        return {"can_build": True, "mode": "subscriber", "message": "ARPG Pro attivo."}
    if not user.get("trial_used") and not user.get("trial_end"):
        return {
            "can_build": False,
            "mode": "needs_trial",
            "message": "Attiva la prova gratuita di 7 giorni per iniziare a costruire (0 € oggi, poi 0,99 €/mese, disdici quando vuoi).",
        }
    trial_end = _tz(user.get("trial_end"))
    if trial_end and now < trial_end:
        days_left = max(1, (trial_end - now).days + 1)
        return {
            "can_build": True,
            "mode": "trial",
            "trial_end": iso(trial_end),
            "days_left": days_left,
            "message": f"Prova gratuita: {days_left} giorni rimasti.",
        }
    return {
        "can_build": False,
        "mode": "locked",
        "message": "La prova gratuita è finita. Attiva ARPG Pro (0,99 €/mese) per continuare a costruire.",
    }


def sanitize_user(user: dict) -> dict:
    return {
        "user_id": user.get("user_id"),
        "email": user.get("email"),
        "name": user.get("name"),
        "picture": user.get("picture"),
        "is_creator": bool(user.get("is_creator")),
        "terms_accepted_at": user.get("terms_accepted_at"),
        "subscription_status": user.get("subscription_status", "none"),
        "trial_used": bool(user.get("trial_used")),
        "subscription_period_end": user.get("subscription_period_end"),
        "access": access_info(user),
    }


def get_token(request: Request) -> str | None:
    cookie = request.cookies.get("session_token")
    if cookie:
        return cookie
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def get_current_user(request: Request) -> dict:
    token = get_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Non autenticato")
    sess = await db.sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess:
        raise HTTPException(status_code=401, detail="Sessione non trovata")
    expires_at = _tz(sess.get("expires_at"))
    if not expires_at or expires_at < now_utc():
        raise HTTPException(status_code=401, detail="Sessione scaduta")
    user = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Utente non trovato")
    return await refresh_if_expired(user)


async def get_studio_user(request: Request) -> dict:
    key = request.headers.get("X-Studio-Key", "")
    if not key:
        raise HTTPException(status_code=401, detail="X-Studio-Key mancante")
    key_doc = await db.studio_keys.find_one({"key": key}, {"_id": 0})
    if not key_doc:
        raise HTTPException(status_code=401, detail="Chiave Studio non valida")
    user = await db.users.find_one({"user_id": key_doc["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Utente non trovato")
    return await refresh_if_expired(user)


async def refresh_if_expired(user: dict) -> dict:
    """Re-read the subscription from Stripe when the stored period has ended (renewals without webhook)."""
    end = _tz(user.get("subscription_period_end"))
    if not user.get("subscription_id") or user.get("subscription_status") not in ("active", "trialing", "canceling", "past_due"):
        return user
    if end and now_utc() < end:
        return user
    try:
        sub = await asyncio.to_thread(stripe.Subscription.retrieve, user["subscription_id"])
    except Exception as e:
        logging.getLogger(__name__).warning(f"Refresh abbonamento fallito per {user.get('user_id')}: {e}")
        return user
    fields = _sub_fields(sub)
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": fields})
    return {**user, **fields}


# ---------- models ----------


class SessionRequest(BaseModel):
    session_id: str


class TermsRequest(BaseModel):
    accepted: bool = True


class BuildRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)


class CheckoutRequest(BaseModel):
    lookup_key: str
    origin_url: str


class StudioCompleteRequest(BaseModel):
    build_id: str
    part_index: int | None = None
    success: bool
    log: str = ""


# ---------- auth ----------


async def login_user(email: str, name: str | None, picture: str | None, session_token: str, response: Response | None) -> dict:
    now = now_utc()
    email = email.lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    is_new = False
    if user:
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"name": name or user.get("name"), "picture": picture, "last_login_at": iso(now)}},
        )
        user["name"] = name or user.get("name")
        user["picture"] = picture
    else:
        is_new = True
        user = {
            "user_id": f"user_{uuid.uuid4().hex[:12]}",
            "email": email,
            "name": name or email.split("@")[0],
            "picture": picture,
            "is_creator": email in CREATOR_EMAILS,
            "terms_accepted_at": None,
            "trial_end": None,
            "trial_used": False,
            "subscription_status": "none",
            "subscription_id": None,
            "subscription_period_end": None,
            "created_at": iso(now),
            "last_login_at": iso(now),
        }
        await db.users.insert_one(dict(user))
    await db.sessions.insert_one(
        {"user_id": user["user_id"], "session_token": session_token, "expires_at": iso(now + timedelta(days=SESSION_DAYS)), "created_at": iso(now)}
    )
    if response is not None:
        response.set_cookie(
            key="session_token", value=session_token, httponly=True, secure=True, samesite="none", path="/", max_age=SESSION_DAYS * 24 * 3600
        )
    if is_new:
        asyncio.create_task(_send_welcome_safe(user))
    return user


@api_router.get("/auth/config")
async def auth_config():
    return {"mode": "google_own" if GOOGLE_CLIENT_ID else "emergent"}


@api_router.post("/auth/session")
async def auth_session(req: SessionRequest, response: Response):
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": req.session_id},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Sessione Google non valida")
    data = r.json()
    if not data.get("email"):
        raise HTTPException(status_code=401, detail="Email Google mancante")
    user = await login_user(data["email"], data.get("name"), data.get("picture"), data["session_token"], response)
    return {**sanitize_user(user), "session_token": data["session_token"]}


# ----- own Google OAuth (used when GOOGLE_CLIENT_ID is set, e.g. self-hosting) -----
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]


def _callback_url(request: Request) -> str:
    base = os.environ.get("PUBLIC_API_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/google/callback"


@api_router.get("/auth/google/login")
async def google_login(request: Request, origin: str = Query(...)):
    # REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=404, detail="Login Google proprio non configurato")
    origin = origin.rstrip("/")
    if origin not in FRONTEND_ORIGINS:
        raise HTTPException(status_code=400, detail="Origine non autorizzata")
    state = uuid.uuid4().hex
    await db.oauth_states.insert_one({"state": state, "origin": origin, "created_at": iso(now_utc())})
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _callback_url(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@api_router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    st = await db.oauth_states.find_one_and_delete({"state": state})
    if not st or not code:
        raise HTTPException(status_code=400, detail="Login Google non valido, riprova")
    async with httpx.AsyncClient(timeout=15) as http:
        tok = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": _callback_url(request),
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            raise HTTPException(status_code=401, detail="Google ha rifiutato il login")
        info = await http.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
        )
    data = info.json()
    if info.status_code != 200 or not data.get("email") or not data.get("email_verified", True):
        raise HTTPException(status_code=401, detail="Email Google non verificata")
    session_token = f"sess_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    await login_user(data["email"], data.get("name"), data.get("picture"), session_token, None)
    one_time = uuid.uuid4().hex
    await db.oauth_codes.insert_one({"code": one_time, "session_token": session_token, "created_at": iso(now_utc())})
    return RedirectResponse(f"{st['origin']}/app#arpg_code={one_time}")


class ExchangeRequest(BaseModel):
    code: str


@api_router.post("/auth/exchange")
async def auth_exchange(req: ExchangeRequest, response: Response):
    doc = await db.oauth_codes.find_one_and_delete({"code": req.code})
    if not doc or (now_utc() - _tz(doc["created_at"])).total_seconds() > 300:
        raise HTTPException(status_code=401, detail="Codice di login scaduto, riprova")
    sess = await db.sessions.find_one({"session_token": doc["session_token"]}, {"_id": 0})
    user = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    response.set_cookie(
        key="session_token", value=doc["session_token"], httponly=True, secure=True, samesite="none", path="/", max_age=SESSION_DAYS * 24 * 3600
    )
    return {**sanitize_user(user), "session_token": doc["session_token"]}


async def _send_welcome_safe(user: dict) -> None:
    try:
        await send_welcome_email(user)
    except Exception as e:
        logging.getLogger(__name__).error(f"Welcome email fallita per {user.get('email')}: {e}")


@api_router.get("/auth/me")
async def auth_me(request: Request):
    user = await get_current_user(request)
    return sanitize_user(user)


@api_router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = get_token(request)
    if token:
        await db.sessions.delete_one({"session_token": token})
    response.delete_cookie(key="session_token", path="/")
    return {"ok": True}


# ---------- terms ----------


@api_router.post("/terms/accept")
async def accept_terms(user=Depends(get_current_user)):
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": {"terms_accepted_at": iso(now_utc())}})
    return {"ok": True}


# ---------- builds ----------

FRIENDLY_AI_ERROR = "Il motore AI è momentaneamente non disponibile. Riprova tra qualche minuto."


def friendly_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "budget" in low or "ratelimit" in low or "429" in low or "concurrent" in low:
        return FRIENDLY_AI_ERROR
    if "codice non valido" in low:
        return "Non sono riuscito a scrivere un codice senza errori per questa parte. Prova a riformulare la richiesta."
    return "Errore durante la generazione. Riprova."


async def refresh_exec_flag(build_id: str) -> None:
    b = await db.builds.find_one({"build_id": build_id}, {"_id": 0, "parts": 1, "next_index": 1, "status": 1, "exec_claimed": 1})
    if not b or b.get("status") in ("failed", "executed"):
        return
    parts = b.get("parts") or []
    idx = b.get("next_index", 0)
    ready = idx < len(parts) and parts[idx].get("status") == "ready" and not b.get("exec_claimed")
    await db.builds.update_one({"build_id": build_id}, {"$set": {"exec_ready": ready}})


async def fail_build(build_id: str, message: str, log: str = "") -> None:
    await db.builds.update_one(
        {"build_id": build_id},
        {"$set": {"status": "failed", "error": message, "error_log": log[:2000], "exec_ready": False, "exec_claimed": False}},
    )


async def run_generation(build_id: str, prompt: str) -> None:
    log = logging.getLogger(__name__)
    b = await db.builds.find_one({"build_id": build_id}, {"_id": 0})
    plan = b.get("plan") if b else None
    if not plan:
        try:
            plan = await plan_build(prompt, build_id)
        except Exception as e:
            log.error(f"Piano build {build_id} fallito: {e}")
            await fail_build(build_id, friendly_error(e), str(e))
            return
        seed = uuid.uuid4().int % 100000
        parts = [{"name": p["name"], "description": p["description"], "status": "pending", "code": None, "repairs": 0, "error": None} for p in plan["parts"]]
        await db.builds.update_one(
            {"build_id": build_id},
            {"$set": {"title": plan["title"], "summary": plan["summary"], "steps": [p["name"] for p in parts], "parts": parts, "plan": plan, "seed": seed, "next_index": 0}},
        )
    else:
        seed = b.get("seed", 4242)
        parts = b["parts"]
    for i in range(len(parts)):
        if parts[i].get("code"):
            continue
        await db.builds.update_one({"build_id": build_id}, {"$set": {f"parts.{i}.status": "generating"}})
        try:
            code = await generate_part(prompt, plan, i, build_id, seed)
        except Exception as e:
            log.error(f"Parte {i} build {build_id} fallita: {e}")
            await fail_build(build_id, friendly_error(e), str(e))
            return
        cur = await db.builds.find_one({"build_id": build_id}, {"_id": 0, "status": 1})
        if not cur or cur.get("status") == "failed":
            return
        await db.builds.update_one({"build_id": build_id}, {"$set": {f"parts.{i}.status": "ready", f"parts.{i}.code": code}})
        await refresh_exec_flag(build_id)
    full = await db.builds.find_one({"build_id": build_id}, {"_id": 0, "parts": 1})
    joined = "\n\n".join(p.get("code") or "" for p in full["parts"])
    await db.builds.update_one(
        {"build_id": build_id, "status": "generating"},
        {"$set": {"status": "ready", "build_code": joined, "file_count": count_artifacts(joined), "ready_at": iso(now_utc())}},
    )
    await db.builds.update_one(
        {"build_id": build_id, "status": "executing"},
        {"$set": {"build_code": joined, "file_count": count_artifacts(joined)}},
    )


async def resume_unfinished() -> None:
    cutoff = iso(now_utc() - timedelta(hours=6))
    async for b in db.builds.find({"status": {"$in": ["generating", "ready", "executing"]}}, {"_id": 0, "build_id": 1, "prompt": 1, "parts": 1, "created_at": 1}):
        if b.get("created_at", "") < cutoff:
            await fail_build(b["build_id"], "Costruzione scaduta. Premi Riprova.")
            continue
        parts = b.get("parts") or []
        for i, p in enumerate(parts):
            if p.get("status") == "repairing":
                asyncio.create_task(repair_part(b["build_id"], i, p.get("error") or "errore sconosciuto"))
            elif p.get("status") == "generating":
                await db.builds.update_one({"build_id": b["build_id"]}, {"$set": {f"parts.{i}.status": "pending"}})
        if not parts or any(not p.get("code") and p.get("status") != "repairing" for p in parts):
            asyncio.create_task(run_generation(b["build_id"], b["prompt"]))


async def repair_part(build_id: str, index: int, runtime_error: str) -> None:
    b = await db.builds.find_one({"build_id": build_id}, {"_id": 0})
    if not b:
        return
    try:
        code = await generate_part(
            b["prompt"], b["plan"], index, build_id, b.get("seed", 4242), runtime_error,
            b["parts"][index].get("code"), safe_mode=b["parts"][index].get("repairs", 0) >= 2,
        )
    except Exception as e:
        await fail_build(build_id, friendly_error(e), str(e))
        return
    r = await db.builds.update_one(
        {"build_id": build_id, f"parts.{index}.status": "repairing", "status": {"$nin": ["failed", "executed"]}},
        {"$set": {f"parts.{index}.status": "ready", f"parts.{index}.code": code, f"parts.{index}.error": None, "exec_claimed": False}},
    )
    if r.matched_count == 0:
        return
    parts = (await db.builds.find_one({"build_id": build_id}, {"_id": 0, "parts": 1}))["parts"]
    joined = "\n\n".join(p.get("code") or "" for p in parts)
    await db.builds.update_one({"build_id": build_id}, {"$set": {"build_code": joined}})
    await refresh_exec_flag(build_id)


def build_public(b: dict) -> dict:
    parts = b.get("parts") or []
    out = {k: v for k, v in b.items() if k not in ("_id", "plan", "error_log")}
    out["parts"] = [{"name": p.get("name"), "status": p.get("status"), "repairs": p.get("repairs", 0)} for p in parts]
    out["parts_total"] = len(parts)
    out["parts_done"] = sum(1 for p in parts if p.get("status") == "done")
    out["parts_generated"] = sum(1 for p in parts if p.get("status") in ("ready", "done", "executing"))
    return out


@api_router.post("/builds")
async def create_build(req: BuildRequest, user=Depends(get_current_user)):
    acc = access_info(user)
    if not acc["can_build"]:
        raise HTTPException(status_code=402, detail=acc["message"])
    if not user.get("terms_accepted_at"):
        raise HTTPException(status_code=403, detail="Devi accettare i termini prima di costruire")
    running = await db.builds.count_documents({"user_id": user["user_id"], "status": "generating"})
    if running >= 2:
        raise HTTPException(status_code=429, detail="Hai già 2 costruzioni in generazione: aspetta che finiscano.")
    build = {
        "build_id": f"build_{uuid.uuid4().hex[:12]}",
        "user_id": user["user_id"],
        "prompt": req.prompt,
        "status": "generating",
        "title": None,
        "summary": None,
        "steps": [],
        "parts": [],
        "next_index": 0,
        "exec_ready": False,
        "exec_claimed": False,
        "build_code": None,
        "file_count": 0,
        "error": None,
        "studio_log": None,
        "executed_at": None,
        "created_at": iso(now_utc()),
    }
    await db.builds.insert_one(dict(build))
    asyncio.create_task(run_generation(build["build_id"], req.prompt))
    return build_public(build)


@api_router.get("/builds")
async def list_builds(request: Request, limit: int = Query(50, ge=1, le=100)):
    user = await get_current_user(request)
    builds = await db.builds.find(
        {"user_id": user["user_id"]},
        {"_id": 0, "build_code": 0, "plan": 0, "parts.code": 0, "parts.description": 0},
    ).sort("created_at", DESCENDING).limit(limit).to_list(limit)
    return [build_public(b) for b in builds]


@api_router.get("/builds/{build_id}")
async def get_build(build_id: str, request: Request):
    user = await get_current_user(request)
    build = await db.builds.find_one({"build_id": build_id, "user_id": user["user_id"]}, {"_id": 0})
    if not build:
        raise HTTPException(status_code=404, detail="Build non trovata")
    return build_public(build)


@api_router.delete("/builds/{build_id}")
async def delete_build(build_id: str, request: Request):
    user = await get_current_user(request)
    r = await db.builds.delete_one({"build_id": build_id, "user_id": user["user_id"]})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Build non trovata")
    return {"ok": True}


@api_router.post("/builds/{build_id}/retry")
async def retry_build(build_id: str, request: Request):
    user = await get_current_user(request)
    if not access_info(user)["can_build"]:
        raise HTTPException(status_code=402, detail=access_info(user)["message"])
    b = await db.builds.find_one({"build_id": build_id, "user_id": user["user_id"]}, {"_id": 0})
    if not b:
        raise HTTPException(status_code=404, detail="Build non trovata")
    if b.get("status") != "failed":
        raise HTTPException(status_code=400, detail="Questa build non è fallita")
    parts = b.get("parts") or []
    updates = {"error": None, "exec_claimed": False, "exec_ready": False}
    missing = not parts
    for i, p in enumerate(parts):
        if p.get("status") == "done":
            continue
        if p.get("code"):
            updates[f"parts.{i}.status"] = "ready"
            updates[f"parts.{i}.repairs"] = 0
        else:
            updates[f"parts.{i}.status"] = "pending"
            missing = True
    updates["status"] = "generating" if missing else ("executing" if b.get("next_index", 0) > 0 else "ready")
    await db.builds.update_one({"build_id": build_id}, {"$set": updates})
    if missing:
        asyncio.create_task(run_generation(build_id, b["prompt"]))
    await refresh_exec_flag(build_id)
    return {"ok": True}


@api_router.post("/builds/{build_id}/resend")
async def resend_build(build_id: str, request: Request):
    user = await get_current_user(request)
    if not access_info(user)["can_build"]:
        raise HTTPException(status_code=402, detail=access_info(user)["message"])
    b = await db.builds.find_one({"build_id": build_id, "user_id": user["user_id"]}, {"_id": 0, "parts": 1})
    if not b:
        raise HTTPException(status_code=404, detail="Build non trovata")
    parts = b.get("parts") or []
    if not parts or any(not p.get("code") for p in parts):
        raise HTTPException(status_code=400, detail="Questa build non ha codice da inviare: creane una nuova.")
    updates = {f"parts.{i}.status": "ready" for i in range(len(parts))}
    updates.update({f"parts.{i}.repairs": 0 for i in range(len(parts))})
    updates.update({"status": "ready", "next_index": 0, "exec_claimed": False, "error": None, "studio_log": None})
    await db.builds.update_one({"build_id": build_id}, {"$set": updates})
    await refresh_exec_flag(build_id)
    return {"ok": True}


# ---------- studio plugin ----------

STUDIO_ONLINE_SECONDS = 20
PLUGIN_TEMPLATE = ROOT_DIR / "plugin_template.lua"


async def _studio_key_for(user_id: str) -> str:
    existing = await db.studio_keys.find_one({"user_id": user_id}, {"_id": 0})
    if existing:
        return existing["key"]
    key = f"arpg_{uuid.uuid4().hex}"
    await db.studio_keys.insert_one({"key": key, "user_id": user_id, "created_at": iso(now_utc())})
    return key


@api_router.post("/studio/key")
async def create_studio_key(user=Depends(get_current_user)):
    return {"key": await _studio_key_for(user["user_id"])}


@api_router.post("/studio/key/regenerate")
async def regenerate_studio_key(user=Depends(get_current_user)):
    await db.studio_keys.delete_one({"user_id": user["user_id"]})
    return {"key": await _studio_key_for(user["user_id"])}


@api_router.get("/studio/status")
async def studio_status(user=Depends(get_current_user)):
    doc = await db.studio_keys.find_one({"user_id": user["user_id"]}, {"_id": 0})
    last = _tz(doc.get("last_seen")) if doc else None
    connected = bool(last and (now_utc() - last).total_seconds() < STUDIO_ONLINE_SECONDS)
    return {"connected": connected, "last_seen": iso(last) if last else None, "place": (doc or {}).get("place_name")}


@api_router.get("/studio/plugin")
async def download_plugin(api_url: str = Query(..., min_length=8), user=Depends(get_current_user)):
    key = await _studio_key_for(user["user_id"])
    src = PLUGIN_TEMPLATE.read_text(encoding="utf-8")
    src = src.replace("__ARPG_API_URL__", api_url.rstrip("/")).replace("__ARPG_STUDIO_KEY__", key)
    return Response(
        content=src,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ARPGBuilder.lua"'},
    )


@api_router.get("/studio/poll")
async def studio_poll(request: Request, place: str = Query("", max_length=120)):
    user = await get_studio_user(request)
    await db.studio_keys.update_one(
        {"user_id": user["user_id"]}, {"$set": {"last_seen": iso(now_utc()), "place_name": place or None}}
    )
    acc = access_info(user)
    if not acc["can_build"]:
        return {"job": None, "locked": acc["message"]}
    stale = iso(now_utc() - timedelta(minutes=4))
    build = await db.builds.find_one_and_update(
        {
            "user_id": user["user_id"],
            "status": {"$in": ["generating", "ready", "executing"]},
            "$or": [{"exec_ready": True}, {"exec_claimed": True, "exec_claimed_at": {"$lt": stale}}],
        },
        {"$set": {"exec_ready": False, "exec_claimed": True, "exec_claimed_at": iso(now_utc())}},
        sort=[("created_at", ASCENDING)],
        projection={"_id": 0},
    )
    if not build:
        return {"job": None}
    idx = build.get("next_index", 0)
    part = build["parts"][idx]
    await db.builds.update_one(
        {"build_id": build["build_id"]},
        {"$set": {f"parts.{idx}.status": "executing", **({"status": "executing"} if build["status"] == "ready" else {})}},
    )
    return {
        "job": {
            "build_id": build["build_id"],
            "title": build.get("title") or build["prompt"][:80],
            "part_index": idx,
            "part_total": len(build["parts"]),
            "part_name": part.get("name"),
            "code": part.get("code"),
            "build_code": part.get("code"),
        }
    }


MAX_REPAIRS = 3


@api_router.post("/studio/complete")
async def studio_complete(req: StudioCompleteRequest, request: Request):
    user = await get_studio_user(request)
    b = await db.builds.find_one({"build_id": req.build_id, "user_id": user["user_id"]}, {"_id": 0, "parts": 1, "status": 1, "next_index": 1})
    if not b:
        raise HTTPException(status_code=404, detail="Build non trovata")
    idx = b.get("next_index", 0) if req.part_index is None else req.part_index
    parts = b.get("parts") or []
    if idx < 0 or idx >= len(parts):
        raise HTTPException(status_code=400, detail="Parte non valida")
    if b.get("status") in ("failed", "executed") or idx != b.get("next_index", 0) or parts[idx].get("status") != "executing":
        return {"ok": True, "ignored": True, "repairing": False}
    if req.success:
        nxt = idx + 1
        updates = {f"parts.{idx}.status": "done", "next_index": nxt, "exec_claimed": False, "studio_log": req.log[:2000]}
        if nxt >= len(parts) and all(p.get("code") for p in parts):
            updates.update({"status": "executed", "executed_at": iso(now_utc())})
        await db.builds.update_one({"build_id": req.build_id}, {"$set": updates})
        await refresh_exec_flag(req.build_id)
        return {"ok": True, "repairing": False}
    repairs = parts[idx].get("repairs", 0)
    if repairs >= MAX_REPAIRS:
        await fail_build(req.build_id, f"La parte \"{parts[idx].get('name')}\" non è riuscita in Studio nemmeno dopo {MAX_REPAIRS} correzioni.", req.log)
        return {"ok": True, "repairing": False}
    await db.builds.update_one(
        {"build_id": req.build_id},
        {"$set": {f"parts.{idx}.status": "repairing", f"parts.{idx}.error": req.log[:1500], f"parts.{idx}.repairs": repairs + 1, "studio_log": req.log[:2000]}},
    )
    asyncio.create_task(repair_part(req.build_id, idx, req.log))
    return {"ok": True, "repairing": True}


# ---------- billing (Stripe Flow A) ----------

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY") or "sk_test_emergent"
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


@api_router.get("/billing")
async def billing_info(request: Request):
    user = await get_current_user(request)
    return {
        "user": sanitize_user(user),
        "price": {"lookup_key": "arpg_monthly", "amount": 0.99, "currency": "eur", "interval": "month"},
    }


@api_router.post("/payments/checkout")
async def create_checkout(req: CheckoutRequest, user=Depends(get_current_user)):
    if user.get("is_creator"):
        raise HTTPException(status_code=400, detail="I Creator hanno tutto gratis, nessun pagamento richiesto")
    if user.get("subscription_status") in ("active", "trialing", "canceling") and access_info(user)["can_build"]:
        raise HTTPException(status_code=400, detail="Hai già un abbonamento attivo")
    prices = stripe.Price.list(lookup_keys=[req.lookup_key], active=True, limit=1).data
    if not prices:
        raise HTTPException(status_code=500, detail=f"Prezzo non trovato: {req.lookup_key}")
    price = prices[0]
    with_trial = not user.get("trial_used") and not user.get("trial_end")
    kwargs = dict(
        line_items=[{"price": price.id, "quantity": 1}],
        mode="subscription" if price.recurring else "payment",
        success_url=f"{req.origin_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{req.origin_url}/payment/cancel",
        metadata={"user_id": user["user_id"], "lookup_key": req.lookup_key},
        customer_email=user.get("email"),
    )
    if with_trial and price.recurring:
        kwargs["subscription_data"] = {"trial_period_days": TRIAL_DAYS, "metadata": {"user_id": user["user_id"]}}
        kwargs["payment_method_collection"] = "always"
    elif price.recurring:
        kwargs["subscription_data"] = {"metadata": {"user_id": user["user_id"]}}
    tax_mode = "full"
    try:
        session = stripe.checkout.Session.create(**kwargs, managed_payments={"enabled": True})
    except stripe.InvalidRequestError as e:
        logging.getLogger(__name__).warning(f"Managed payments non disponibile, fallback: {getattr(e, 'user_message', '') or e}")
        try:
            session = stripe.checkout.Session.create(**kwargs, automatic_tax={"enabled": True}, billing_address_collection="required")
            tax_mode = "calc_only"
        except stripe.InvalidRequestError:
            session = stripe.checkout.Session.create(**kwargs)
            tax_mode = "none"
    await db["payment_transactions"].insert_one(
        {
            "session_id": session.id,
            "user_id": user["user_id"],
            "lookup_key": req.lookup_key,
            "amount": (price.unit_amount or 0),
            "currency": price.currency,
            "with_trial": with_trial,
            "status": "initiated",
            "payment_status": "pending",
            "created_at": iso(now_utc()),
            "updated_at": iso(now_utc()),
        }
    )
    return {"checkout_url": session.url, "session_id": session.id, "tax_mode": tax_mode}


@api_router.get("/payments/status/{session_id}")
async def get_status(session_id: str):
    record = await db["payment_transactions"].find_one({"session_id": session_id}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="Transazione non trovata")
    if record.get("payment_status") != "paid":
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            if s.payment_status == "paid" or s.status == "complete":
                await db["payment_transactions"].update_one(
                    {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                    {
                        "$set": {
                            "status": "completed",
                            "payment_status": "paid",
                            "stripe_subscription_id": s.subscription,
                            "stripe_payment_intent_id": s.payment_intent,
                            "updated_at": iso(now_utc()),
                        }
                    },
                )
                await activate_subscription(record.get("user_id"), s.subscription)
                record = await db["payment_transactions"].find_one({"session_id": session_id}, {"_id": 0})
        except stripe.StripeError:
            pass
    return {
        "session_id": record["session_id"],
        "status": record["status"],
        "payment_status": record["payment_status"],
        "with_trial": bool(record.get("with_trial")),
    }


def _sub_fields(sub) -> dict:
    status = sub.get("status")
    end_ts = sub.get("trial_end") if status == "trialing" else None
    if not end_ts:
        end_ts = sub.get("current_period_end")
        if not end_ts:
            items = (sub.get("items") or {}).get("data") or []
            end_ts = items[0].get("current_period_end") if items else None
    mapped = {"trialing": "trialing", "active": "active", "past_due": "past_due", "canceled": "canceled", "unpaid": "canceled", "incomplete_expired": "canceled"}.get(status, status)
    if mapped in ("active", "trialing") and sub.get("cancel_at_period_end"):
        mapped = "canceling"
    return {
        "subscription_status": mapped,
        "subscription_period_end": iso(datetime.fromtimestamp(end_ts, tz=timezone.utc)) if end_ts else iso(now_utc() + timedelta(days=31)),
    }


async def activate_subscription(user_id: str | None, subscription_id: str | None) -> None:
    if not user_id:
        return
    fields = {"subscription_status": "active", "subscription_period_end": iso(now_utc() + timedelta(days=31))}
    if subscription_id:
        try:
            fields = _sub_fields(stripe.Subscription.retrieve(subscription_id))
        except stripe.StripeError:
            pass
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {**fields, "subscription_id": subscription_id, "trial_used": True}},
    )


async def sync_subscription(sub) -> None:
    user_id = (sub.get("metadata") or {}).get("user_id")
    query = {"user_id": user_id} if user_id else {"subscription_id": sub.get("id")}
    await db.users.update_one(query, {"$set": {**_sub_fields(sub), "subscription_id": sub.get("id")}})


@api_router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    obj, t = event["data"]["object"], event["type"]
    if t == "checkout.session.completed":
        await db["payment_transactions"].update_one(
            {"session_id": obj["id"], "payment_status": {"$ne": "paid"}},
            {
                "$set": {
                    "status": "completed",
                    "payment_status": obj.get("payment_status", "paid"),
                    "stripe_subscription_id": obj.get("subscription"),
                    "stripe_payment_intent_id": obj.get("payment_intent"),
                    "updated_at": iso(now_utc()),
                }
            },
        )
        if obj.get("payment_status", "paid") in ("paid", "no_payment_required") or obj.get("status") == "complete":
            metadata = obj.get("metadata") or {}
            await activate_subscription(metadata.get("user_id"), obj.get("subscription"))
    elif t in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
        await sync_subscription(obj)
    elif t == "checkout.session.async_payment_succeeded":
        metadata = obj.get("metadata") or {}
        await db["payment_transactions"].update_one(
            {"session_id": obj["id"]},
            {"$set": {"payment_status": "paid", "updated_at": iso(now_utc())}},
        )
        await activate_subscription(metadata.get("user_id"), obj.get("subscription"))
    elif t == "checkout.session.async_payment_failed":
        await db["payment_transactions"].update_one(
            {"session_id": obj["id"]},
            {"$set": {"status": "failed", "payment_status": "failed", "updated_at": iso(now_utc())}},
        )
    elif t == "checkout.session.expired":
        await db["payment_transactions"].update_one(
            {"session_id": obj["id"]},
            {"$set": {"status": "expired", "payment_status": "expired", "updated_at": iso(now_utc())}},
        )
    elif t == "charge.refunded":
        await db["payment_transactions"].update_one(
            {"stripe_payment_intent_id": obj.get("payment_intent")},
            {"$set": {"status": "refunded", "payment_status": "refunded", "updated_at": iso(now_utc())}},
        )
    return {"status": "ok"}


@api_router.post("/billing/cancel")
async def cancel_subscription(request: Request, user=Depends(get_current_user)):
    if user.get("is_creator"):
        raise HTTPException(status_code=400, detail="I Creator non hanno un abbonamento da cancellare")
    if not user.get("subscription_id") or user.get("subscription_status") not in ("active", "trialing"):
        raise HTTPException(status_code=400, detail="Nessun abbonamento attivo")
    try:
        sub = stripe.Subscription.modify(user["subscription_id"], cancel_at_period_end=True)
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe: {e}")
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": _sub_fields(sub)})
    return {"ok": True, "message": "Abbonamento disdetto: nessun altro addebito. Resti attivo fino alla fine del periodo."}


@api_router.post("/billing/resume")
async def resume_subscription(user=Depends(get_current_user)):
    if user.get("subscription_status") != "canceling" or not user.get("subscription_id"):
        raise HTTPException(status_code=400, detail="Nessun abbonamento da riattivare")
    try:
        sub = stripe.Subscription.modify(user["subscription_id"], cancel_at_period_end=False)
    except stripe.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe: {e}")
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": _sub_fields(sub)})
    return {"ok": True, "message": "Abbonamento riattivato."}


@api_router.get("/")
async def root():
    return {"message": "ARPG API", "ok": True}


app.include_router(api_router)


@app.on_event("startup")
async def start_index_build():
    app.state.index_task = asyncio.create_task(ensure_indexes())
    app.state.resume_task = asyncio.create_task(resume_unfinished())


@app.on_event("shutdown")
async def shutdown_db_client():
    app.state.index_task.cancel()
    with suppress(asyncio.CancelledError):
        await app.state.index_task
    client.close()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
