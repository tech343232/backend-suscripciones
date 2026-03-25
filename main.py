import os
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
import stripe
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from supabase import create_client, Client

app = FastAPI(title="Backend Suscripciones", version="1.0.0")

# =========================
# VARIABLES DE ENTORNO
# =========================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
META_PIXEL_ID = os.getenv("META_PIXEL_ID", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
APP_URL = os.getenv("APP_URL", "")
PRICE_ID = os.getenv("PRICE_ID", "")

REQUIRED_VARS = {
    "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
    "APP_URL": APP_URL,
    "PRICE_ID": PRICE_ID,
}

missing = [name for name, value in REQUIRED_VARS.items() if not value]
if missing:
    raise RuntimeError(f"Faltan variables de entorno requeridas: {', '.join(missing)}")

stripe.api_key = STRIPE_SECRET_KEY

# 🔥 SOLUCIÓN:
# No dejamos que un fallo de conexión con Supabase rompa toda la app
supabase: Optional[Client] = None
supabase_error: Optional[str] = None

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    print("✅ Supabase conectado correctamente")
except Exception as e:
    supabase_error = str(e)
    print("❌ Error conectando a Supabase:", supabase_error)


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
# FUNCIONES AUXILIARES
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_value(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def unix_to_iso(timestamp: Optional[int]) -> Optional[str]:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def ensure_supabase() -> Client:
    if supabase is None:
        raise HTTPException(
            status_code=500,
            detail=f"Supabase no está disponible. {supabase_error or ''}".strip()
        )
    return supabase


def send_meta_purchase_event(email: Optional[str], value: Optional[float], currency: str = "USD") -> None:
    """
    Meta CAPI opcional. Si faltan variables, simplemente no envía nada.
    """
    if not META_PIXEL_ID or not META_ACCESS_TOKEN:
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
                "custom_data": {
                    "currency": currency,
                    "value": float(value or 0),
                },
            }
        ]
    }

    try:
        url = f"https://graph.facebook.com/v19.0/{META_PIXEL_ID}/events?access_token={META_ACCESS_TOKEN}"
        r = requests.post(url, json=payload, timeout=20)
        print("Meta CAPI status:", r.status_code, r.text)
    except Exception as e:
        print("Error enviando evento a Meta CAPI:", str(e))


def get_customer_email_from_session(session_obj: Dict[str, Any]) -> Optional[str]:
    customer_details = session_obj.get("customer_details") or {}
    email = customer_details.get("email")
    if email:
        return email

    customer_email = session_obj.get("customer_email")
    if customer_email:
        return customer_email

    metadata = session_obj.get("metadata") or {}
    if metadata.get("email"):
        return metadata["email"]

    return None


def get_subscription(subscription_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not subscription_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        return sub
    except Exception as e:
        print("No se pudo obtener la suscripción:", str(e))
        return None


def upsert_user_by_email(
    email: str,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    status: Optional[str] = None,
    access_active: Optional[bool] = None,
    price_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
) -> None:
    sb = ensure_supabase()

    existing = sb.table("users").select("*").eq("email", email).execute()

    data: Dict[str, Any] = {"updated_at": now_iso()}

    if customer_id is not None:
        data["stripe_customer_id"] = customer_id
    if subscription_id is not None:
        data["stripe_subscription_id"] = subscription_id
    if status is not None:
        data["subscription_status"] = status
    if access_active is not None:
        data["access_active"] = access_active
    if price_id is not None:
        data["price_id"] = price_id
    if current_period_end is not None:
        data["current_period_end"] = current_period_end

    if existing.data:
        sb.table("users").update(data).eq("email", email).execute()
    else:
        data["email"] = email
        data["created_at"] = now_iso()
        sb.table("users").insert(data).execute()


def update_user_by_customer_id(
    customer_id: str,
    status: Optional[str] = None,
    subscription_id: Optional[str] = None,
    access_active: Optional[bool] = None,
    price_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
) -> None:
    sb = ensure_supabase()

    data: Dict[str, Any] = {"updated_at": now_iso()}

    if status is not None:
        data["subscription_status"] = status
    if subscription_id is not None:
        data["stripe_subscription_id"] = subscription_id
    if access_active is not None:
        data["access_active"] = access_active
    if price_id is not None:
        data["price_id"] = price_id
    if current_period_end is not None:
        data["current_period_end"] = current_period_end

    sb.table("users").update(data).eq("stripe_customer_id", customer_id).execute()


# =========================
# RUTAS BÁSICAS
# =========================
@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Backend activo",
        "supabase_connected": supabase is not None,
    }


@app.get("/health")
def health():
    return {
        "status": "running",
        "time": time.time(),
        "supabase_connected": supabase is not None,
    }


@app.get("/config-check")
def config_check():
    return {
        "ok": True,
        "stripe_configured": bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
        "supabase_connected": bool(supabase is not None),
        "supabase_error": supabase_error,
        "meta_configured": bool(META_PIXEL_ID and META_ACCESS_TOKEN),
        "app_url": APP_URL,
        "price_id_loaded": bool(PRICE_ID),
    }


# =========================
# CREAR CHECKOUT DE STRIPE
# =========================
@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    """
    Body esperado:
    {
      "email": "cliente@correo.com"
    }

    El price_id sale de la variable PRICE_ID en Railway.
    """
    body = await request.json()
    email = (body.get("email") or "").strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            customer_email=email,
            success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/cancel",
            metadata={"email": email},
        )
        return {"checkout_url": session.url}
    except Exception as e:
        print("Error creando checkout:", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# WEBHOOK DE STRIPE
# =========================
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")

    event_type = event["type"]
    obj = event["data"]["object"]

    print("Evento recibido:", event_type)

    # 1) Checkout completado
    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        email = get_customer_email_from_session(obj)

        subscription = get_subscription(subscription_id)
        status = subscription.get("status") if subscription else "active"
        current_period_end = unix_to_iso(subscription.get("current_period_end")) if subscription else None

        resolved_price_id = PRICE_ID
        if subscription:
            items = subscription.get("items", {}).get("data", [])
            if items and items[0].get("price", {}).get("id"):
                resolved_price_id = items[0]["price"]["id"]

        if email:
            upsert_user_by_email(
                email=email,
                customer_id=customer_id,
                subscription_id=subscription_id,
                status=status,
                access_active=True,
                price_id=resolved_price_id,
                current_period_end=current_period_end,
            )
        elif customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status=status,
                subscription_id=subscription_id,
                access_active=True,
                price_id=resolved_price_id,
                current_period_end=current_period_end,
            )

        amount_total = (obj.get("amount_total") or 0) / 100
        currency = (obj.get("currency") or "usd").upper()
        send_meta_purchase_event(email=email, value=amount_total, currency=currency)

    # 2) Factura pagada
    elif event_type == "invoice.paid":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        subscription = get_subscription(subscription_id)
        status = subscription.get("status") if subscription else "active"
        current_period_end = unix_to_iso(subscription.get("current_period_end")) if subscription else None

        resolved_price_id = PRICE_ID
        if subscription:
            items = subscription.get("items", {}).get("data", [])
            if items and items[0].get("price", {}).get("id"):
                resolved_price_id = items[0]["price"]["id"]

        if customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status=status,
                subscription_id=subscription_id,
                access_active=True,
                price_id=resolved_price_id,
                current_period_end=current_period_end,
            )

    # 3) Factura fallida
    elif event_type == "invoice.payment_failed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        if customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status="past_due",
                subscription_id=subscription_id,
                access_active=False,
            )

    # 4) Suscripción actualizada
    elif event_type == "customer.subscription.updated":
        customer_id = obj.get("customer")
        subscription_id = obj.get("id")
        status = obj.get("status")
        current_period_end = unix_to_iso(obj.get("current_period_end"))

        items = obj.get("items", {}).get("data", [])
        resolved_price_id = PRICE_ID
        if items and items[0].get("price", {}).get("id"):
            resolved_price_id = items[0]["price"]["id"]

        active = status in {"active", "trialing"}

        if customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status=status,
                subscription_id=subscription_id,
                access_active=active,
                price_id=resolved_price_id,
                current_period_end=current_period_end,
            )

    # 5) Suscripción cancelada
    elif event_type == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        subscription_id = obj.get("id")

        if customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status="canceled",
                subscription_id=subscription_id,
                access_active=False,
            )

    return JSONResponse({"received": True})
