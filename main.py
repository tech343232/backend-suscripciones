import asyncio
import os
import hashlib
import socket
import ssl
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import asyncpg
import httpx
import stripe
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


# =========================
# CONFIG BÁSICA
# =========================
def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_value(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def unix_to_iso(timestamp: Optional[int]) -> Optional[str]:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def get_required_env(name: str) -> str:
    value = get_env(name)
    if not value:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return value


# =========================
# CONNECTION POOL (asyncpg)
# =========================
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = get_required_env("DATABASE_URL")
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        print("✅ DB connecting via DATABASE_URL")
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
            ssl=ssl_ctx,
        )
    return _pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar pool al arrancar
    try:
        await get_pool()
        print("✅ Pool PostgreSQL inicializado")
    except Exception as e:
        print(f"⚠️ No se pudo inicializar el pool al arrancar: {e}")
    yield
    # Cerrar pool al apagar
    global _pool
    if _pool:
        await _pool.close()
        print("Pool PostgreSQL cerrado")


app = FastAPI(title="Backend Suscripciones", version="4.0.0", lifespan=lifespan)


# =========================
# RETRY HELPER
# =========================
async def _async_retry(coro_fn, max_retries: int = 7, base_delay: float = 5.0, max_delay: float = 60.0):
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except Exception as e:
            last_exc = e
            err_lower = str(e).lower()
            is_network = any(tok in err_lower for tok in (
                "name or service not known",
                "connection refused",
                "connection reset",
                "timed out",
                "timeout",
                "temporary failure in name resolution",
                "network unreachable",
                "failed to establish",
                "connection terminated",
                "too many clients",
            ))
            if is_network and attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(f"⚠️ Error de red (intento {attempt + 1}/{max_retries}), reintentando en {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                raise
    raise last_exc


# =========================
# STRIPE HELPERS
# =========================
def get_stripe_ready():
    stripe.api_key = get_required_env("STRIPE_SECRET_KEY")


def get_price_map() -> Dict[str, Dict[str, Any]]:
    return {
        get_required_env("PRICE_ID_BASICO"): {"plan": "basico", "contact_limit": 100},
        get_required_env("PRICE_ID_PROFESIONAL"): {"plan": "profesional", "contact_limit": 450},
        get_required_env("PRICE_ID_AVANZADO"): {"plan": "avanzado", "contact_limit": 1000},
    }


def get_plan_catalog() -> Dict[str, Dict[str, Any]]:
    return {
        "basico": {"price_id": get_required_env("PRICE_ID_BASICO"), "contact_limit": 100},
        "profesional": {"price_id": get_required_env("PRICE_ID_PROFESIONAL"), "contact_limit": 450},
        "avanzado": {"price_id": get_required_env("PRICE_ID_AVANZADO"), "contact_limit": 1000},
    }


# =========================
# MANEJO GLOBAL DE ERRORES
# =========================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"❌ Error global en {request.url.path}: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "Error interno del servidor",
            "detail": str(exc),
            "path": request.url.path,
        },
    )


# =========================
# META CAPI (OPCIONAL)
# =========================
async def send_meta_purchase_event(
    email: Optional[str], value: Optional[float], currency: str = "USD"
) -> None:
    meta_pixel_id = get_env("META_PIXEL_ID")
    meta_access_token = get_env("META_ACCESS_TOKEN")

    if not meta_pixel_id or not meta_access_token:
        print("Meta CAPI no configurado. Saltando evento.")
        return

    user_data = {}
    if email:
        user_data["em"] = [sha256_value(email)]

    payload = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": int(time.time()),
                "action_source": "website",
                "user_data": user_data,
                "custom_data": {"currency": currency, "value": float(value or 0)},
            }
        ]
    }

    try:
        url = f"https://graph.facebook.com/v19.0/{meta_pixel_id}/events?access_token={meta_access_token}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload)
            print("Meta CAPI status:", r.status_code, r.text)
    except Exception as e:
        print("Error enviando evento a Meta CAPI:", str(e))


# =========================
# HELPERS STRIPE
# =========================
def get_customer_email_from_session(session_obj: Dict[str, Any]) -> Optional[str]:
    customer_details = session_obj.get("customer_details") or {}
    email = customer_details.get("email")
    if email:
        return email
    customer_email = session_obj.get("customer_email")
    if customer_email:
        return customer_email
    metadata = session_obj.get("metadata") or {}
    return metadata.get("email")


def get_subscription(subscription_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not subscription_id:
        return None
    get_stripe_ready()
    try:
        return stripe.Subscription.retrieve(subscription_id)
    except Exception as e:
        print("No se pudo obtener la suscripción:", str(e))
        return None


def resolve_plan_from_price_id(price_id: Optional[str]) -> Dict[str, Any]:
    price_map = get_price_map()
    print(f"[resolve_plan] incoming price_id={price_id!r}")
    print(f"[resolve_plan] known price_ids={list(price_map.keys())}")
    if price_id and price_id in price_map:
        result = {
            "price_id": price_id,
            "plan": price_map[price_id]["plan"],
            "contact_limit": price_map[price_id]["contact_limit"],
        }
        print(f"[resolve_plan] matched → {result}")
        return result
    print(f"[resolve_plan] ⚠️ no match — plan will be None")
    return {"price_id": price_id, "plan": None, "contact_limit": 0}


# =========================
# HELPERS DB (asyncpg directo)
# =========================
async def upsert_user_by_email(
    email: str,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    status: Optional[str] = None,
    access_active: Optional[bool] = None,
    price_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
    plan: Optional[str] = None,
    contact_limit: Optional[int] = None,
) -> None:
    async def _do():
        pool = await get_pool()
        async with pool.acquire() as conn:
            print(f"[upsert] looking up email={email}")
            row = await conn.fetchrow(
                "SELECT id FROM usuarios WHERE email = $1", email
            )
            ts = now_iso()
            if row:
                print(f"[upsert] existing user id={row['id']} — running UPDATE")
                result = await conn.execute(
                    """
                    UPDATE usuarios SET
                        updated_at            = $1,
                        stripe_customer_id    = COALESCE($2, stripe_customer_id),
                        stripe_subscription_id= COALESCE($3, stripe_subscription_id),
                        subscription_status   = COALESCE($4, subscription_status),
                        access_active         = COALESCE($5, access_active),
                        price_id              = COALESCE($6, price_id),
                        current_period_end    = COALESCE($7, current_period_end),
                        plan                  = COALESCE($8, plan),
                        contact_limit         = COALESCE($9, contact_limit)
                    WHERE email = $10
                    """,
                    ts, customer_id, subscription_id, status, access_active,
                    price_id, current_period_end, plan, contact_limit, email,
                )
                print(f"[upsert] UPDATE result: {result}")
            else:
                print(f"[upsert] no existing user — running INSERT for email={email}")
                result = await conn.execute(
                    """
                    INSERT INTO usuarios
                        (email, created_at, updated_at, contacts_used,
                         stripe_customer_id, stripe_subscription_id,
                         subscription_status, access_active, price_id,
                         current_period_end, plan, contact_limit)
                    VALUES ($1,$2,$3,0,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    email, ts, ts,
                    customer_id, subscription_id, status, access_active,
                    price_id, current_period_end, plan, contact_limit,
                )
                print(f"[upsert] INSERT result: {result}")

    await _async_retry(_do)


async def update_user_by_customer_id(
    customer_id: str,
    status: Optional[str] = None,
    subscription_id: Optional[str] = None,
    access_active: Optional[bool] = None,
    price_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
    plan: Optional[str] = None,
    contact_limit: Optional[int] = None,
) -> None:
    async def _do():
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE usuarios SET
                    updated_at            = $1,
                    subscription_status   = COALESCE($2, subscription_status),
                    stripe_subscription_id= COALESCE($3, stripe_subscription_id),
                    access_active         = COALESCE($4, access_active),
                    price_id              = COALESCE($5, price_id),
                    current_period_end    = COALESCE($6, current_period_end),
                    plan                  = COALESCE($7, plan),
                    contact_limit         = COALESCE($8, contact_limit)
                WHERE stripe_customer_id = $9
                """,
                now_iso(), status, subscription_id, access_active,
                price_id, current_period_end, plan, contact_limit, customer_id,
            )

    await _async_retry(_do)


async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    async def _do():
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM usuarios WHERE email = $1 LIMIT 1", email
            )
            return dict(row) if row else None

    return await _async_retry(_do)


async def count_user_contacts(user_id: str) -> int:
    async def _do():
        pool = await get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT COUNT(*) FROM contacts WHERE user_id = $1", user_id
            )
            return int(val)

    return await _async_retry(_do)


async def sync_contacts_used(user_id: str) -> int:
    total = await count_user_contacts(user_id)

    async def _do():
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE usuarios SET contacts_used = $1, updated_at = $2 WHERE id = $3",
                total, now_iso(), user_id,
            )

    await _async_retry(_do)
    return total


# =========================
# RUTAS BÁSICAS
# =========================
@app.get("/")
def root():
    return {"ok": True, "message": "Backend activo"}


@app.get("/health")
async def health():
    results = {}
    host = "lzxhrqfzpbyjyvoscjou.supabase.co"

    # 1. Resolución DNS directa con socket
    try:
        addrs = await asyncio.to_thread(socket.getaddrinfo, host, 443)
        resolved = [a[4][0] for a in addrs]
        results["dns_socket"] = {"ok": True, "host": host, "resolved": resolved}
    except Exception as e:
        results["dns_socket"] = {"ok": False, "host": host, "error": str(e)}

    # 2. HTTP a Google (internet general)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://google.com")
        results["google"] = {"ok": True, "status_code": r.status_code}
    except Exception as e:
        results["google"] = {"ok": False, "error": str(e)}

    # 3. HTTP a Supabase (dominio específico)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"https://{host}")
        results["supabase_http"] = {"ok": True, "status_code": r.status_code}
    except Exception as e:
        results["supabase_http"] = {"ok": False, "error": str(e)}

    # 4. Resolución DNS via 8.8.8.8 (bypass DNS de Railway)
    db_host = "aws-1-us-east-1.pooler.supabase.com"
    try:
        import dns.resolver
        def _resolve_external():
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ["8.8.8.8"]
            answer = resolver.resolve(db_host, "A")
            return [r.to_text() for r in answer]
        ips = await asyncio.to_thread(_resolve_external)
        results["dns_external_8888"] = {"ok": True, "host": db_host, "resolved": ips}
    except Exception as e:
        results["dns_external_8888"] = {"ok": False, "host": db_host, "error": str(e)}

    # 5. Conexión directa PostgreSQL por IP (asyncpg)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
        results["postgres_direct"] = {
            "ok": True,
            "select_1": val,
            "database_url_set": bool(get_env("DATABASE_URL")),
        }
    except Exception as e:
        results["postgres_direct"] = {
            "ok": False,
            "error": str(e),
            "database_url_set": bool(get_env("DATABASE_URL")),
        }

    return {
        "status": "running",
        "time": time.time(),
        "service": "backend-suscripciones",
        "diagnostics": results,
    }


@app.get("/config-check")
async def config_check():
    checks = {
        "STRIPE_SECRET_KEY": bool(get_env("STRIPE_SECRET_KEY")),
        "STRIPE_WEBHOOK_SECRET": bool(get_env("STRIPE_WEBHOOK_SECRET")),
        "DB_PASSWORD": bool(get_env("DB_PASSWORD")),
        "APP_URL": bool(get_env("APP_URL")),
        "PRICE_ID_BASICO": bool(get_env("PRICE_ID_BASICO")),
        "PRICE_ID_PROFESIONAL": bool(get_env("PRICE_ID_PROFESIONAL")),
        "PRICE_ID_AVANZADO": bool(get_env("PRICE_ID_AVANZADO")),
        "META_PIXEL_ID": bool(get_env("META_PIXEL_ID")),
        "META_ACCESS_TOKEN": bool(get_env("META_ACCESS_TOKEN")),
    }

    db_ok = False
    db_error = None
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception as e:
        db_error = str(e)

    return {
        "ok": True,
        "env": checks,
        "db_connected": db_ok,
        "db_error": db_error,
    }


@app.get("/plans")
def get_plans():
    return {
        "ok": True,
        "plans": {
            "basico": {"contact_limit": 100},
            "profesional": {"contact_limit": 450},
            "avanzado": {"contact_limit": 1000},
        },
    }


# =========================
# CREAR CHECKOUT
# =========================
@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Falta email")
    if not plan:
        raise HTTPException(status_code=400, detail="Falta plan")

    plan_catalog = get_plan_catalog()
    plan_data = plan_catalog.get(plan)
    if not plan_data:
        raise HTTPException(status_code=400, detail="Plan inválido")

    get_stripe_ready()
    app_url = get_required_env("APP_URL")

    try:
        session = await asyncio.to_thread(
            lambda: stripe.checkout.Session.create(
                mode="subscription",
                payment_method_types=["card"],
                line_items=[{"price": plan_data["price_id"], "quantity": 1}],
                customer_email=email,
                success_url=f"{app_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{app_url}/cancel",
                metadata={"email": email, "selected_plan": plan},
            )
        )
        return {"checkout_url": session.url}
    except Exception as e:
        print("Error creando checkout:", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# CONTACTOS
# =========================
@app.post("/create-contact")
async def create_contact(request: Request):
    body = await request.json()

    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    notes = (body.get("notes") or "").strip()

    if not email:
        raise HTTPException(status_code=400, detail="Falta email")
    if not name:
        raise HTTPException(status_code=400, detail="Falta name")

    user = await get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not user.get("access_active"):
        raise HTTPException(status_code=403, detail="Suscripción inactiva")

    user_id = user["id"]
    contact_limit = int(user.get("contact_limit") or 0)
    current_contacts = await sync_contacts_used(user_id)

    if current_contacts >= contact_limit:
        raise HTTPException(
            status_code=403,
            detail=f"Límite alcanzado. Tu plan permite {contact_limit} contactos activos.",
        )

    async def _do_insert():
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO contacts (user_id, name, phone, notes)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                user_id,
                name,
                phone if phone else None,
                notes if notes else None,
            )
            return dict(row) if row else None

    insert_result = await _async_retry(_do_insert)
    updated_contacts = await sync_contacts_used(user_id)

    return {
        "ok": True,
        "message": "Contacto creado correctamente",
        "contact": insert_result,
        "contacts_used": updated_contacts,
        "contact_limit": contact_limit,
        "remaining": max(contact_limit - updated_contacts, 0),
    }


@app.get("/my-plan")
async def my_plan(email: str):
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    user = await get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user_id = user["id"]
    current_contacts = await sync_contacts_used(user_id)

    return {
        "ok": True,
        "email": user["email"],
        "plan": user.get("plan"),
        "access_active": user.get("access_active"),
        "subscription_status": user.get("subscription_status"),
        "contact_limit": int(user.get("contact_limit") or 0),
        "contacts_used": current_contacts,
        "remaining": max(int(user.get("contact_limit") or 0) - current_contacts, 0),
        "current_period_end": user.get("current_period_end"),
    }


# =========================
# WEBHOOK STRIPE
# =========================
async def _process_stripe_event(event: Dict[str, Any]) -> None:
    event_type = event["type"]
    obj = event["data"]["object"]
    print("Evento recibido:", event_type)

    try:
        if event_type == "checkout.session.completed":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            email = get_customer_email_from_session(obj)

            print(f"[checkout.completed] customer_id={customer_id} subscription_id={subscription_id} email={email}")
            print(f"[checkout.completed] customer_details={obj.get('customer_details')} metadata={obj.get('metadata')}")

            subscription = await asyncio.to_thread(get_subscription, subscription_id)
            print(f"[checkout.completed] subscription fetched: {subscription is not None}, status={subscription.get('status') if subscription else 'N/A'}")

            status = subscription.get("status") if subscription else "active"
            current_period_end = unix_to_iso(subscription.get("current_period_end")) if subscription else None

            resolved_price_id = None
            if subscription:
                items = subscription.get("items", {}).get("data", [])
                if items and items[0].get("price", {}).get("id"):
                    resolved_price_id = items[0]["price"]["id"]
            if not resolved_price_id:
                resolved_price_id = obj.get("metadata", {}).get("price_id")

            plan_info = resolve_plan_from_price_id(resolved_price_id)
            print(f"[checkout.completed] resolved_price_id={resolved_price_id} plan_info={plan_info}")

            if email:
                print(f"[checkout.completed] calling upsert_user_by_email for {email}")
                await upsert_user_by_email(
                    email=email,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    status=status,
                    access_active=True,
                    price_id=plan_info["price_id"],
                    current_period_end=current_period_end,
                    plan=plan_info["plan"],
                    contact_limit=plan_info["contact_limit"],
                )
                print(f"[checkout.completed] upsert_user_by_email OK for {email}")
            elif customer_id:
                print(f"[checkout.completed] no email — calling update_user_by_customer_id for {customer_id}")
                await update_user_by_customer_id(
                    customer_id=customer_id,
                    status=status,
                    subscription_id=subscription_id,
                    access_active=True,
                    price_id=plan_info["price_id"],
                    current_period_end=current_period_end,
                    plan=plan_info["plan"],
                    contact_limit=plan_info["contact_limit"],
                )
                print(f"[checkout.completed] update_user_by_customer_id OK for {customer_id}")
            else:
                print("[checkout.completed] ⚠️ no email and no customer_id — nothing written to DB")

            amount_total = (obj.get("amount_total") or 0) / 100
            currency = (obj.get("currency") or "usd").upper()
            await send_meta_purchase_event(email=email, value=amount_total, currency=currency)

        elif event_type == "invoice.paid":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            subscription = await asyncio.to_thread(get_subscription, subscription_id)
            status = subscription.get("status") if subscription else "active"
            current_period_end = unix_to_iso(subscription.get("current_period_end")) if subscription else None

            resolved_price_id = None
            if subscription:
                items = subscription.get("items", {}).get("data", [])
                if items and items[0].get("price", {}).get("id"):
                    resolved_price_id = items[0]["price"]["id"]

            plan_info = resolve_plan_from_price_id(resolved_price_id)

            if customer_id:
                await update_user_by_customer_id(
                    customer_id=customer_id,
                    status=status,
                    subscription_id=subscription_id,
                    access_active=True,
                    price_id=plan_info["price_id"],
                    current_period_end=current_period_end,
                    plan=plan_info["plan"],
                    contact_limit=plan_info["contact_limit"],
                )

        elif event_type == "invoice.payment_failed":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")

            if customer_id:
                await update_user_by_customer_id(
                    customer_id=customer_id,
                    status="past_due",
                    subscription_id=subscription_id,
                    access_active=False,
                )

        elif event_type == "customer.subscription.updated":
            customer_id = obj.get("customer")
            subscription_id = obj.get("id")
            status = obj.get("status")
            current_period_end = unix_to_iso(obj.get("current_period_end"))

            items = obj.get("items", {}).get("data", [])
            resolved_price_id = None
            if items and items[0].get("price", {}).get("id"):
                resolved_price_id = items[0]["price"]["id"]

            plan_info = resolve_plan_from_price_id(resolved_price_id)
            active = status in {"active", "trialing"}

            if customer_id:
                await update_user_by_customer_id(
                    customer_id=customer_id,
                    status=status,
                    subscription_id=subscription_id,
                    access_active=active,
                    price_id=plan_info["price_id"],
                    current_period_end=current_period_end,
                    plan=plan_info["plan"],
                    contact_limit=plan_info["contact_limit"],
                )

        elif event_type == "customer.subscription.deleted":
            customer_id = obj.get("customer")
            subscription_id = obj.get("id")

            if customer_id:
                await update_user_by_customer_id(
                    customer_id=customer_id,
                    status="canceled",
                    subscription_id=subscription_id,
                    access_active=False,
                )

    except Exception as e:
        print(f"❌ Error procesando evento Stripe {event_type}: {e}")


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    get_stripe_ready()
    webhook_secret = get_required_env("STRIPE_WEBHOOK_SECRET")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")

    background_tasks.add_task(_process_stripe_event, event)
    return JSONResponse({"received": True})
