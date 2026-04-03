import os
import hashlib
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
import stripe
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from supabase import create_client

app = FastAPI(title="Backend Suscripciones", version="3.0.0")


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
        raise HTTPException(status_code=500, detail=f"Falta variable de entorno: {name}")
    return value


def get_supabase_client():
    supabase_url = get_required_env("SUPABASE_URL")
    supabase_key = get_required_env("SUPABASE_SERVICE_ROLE_KEY")
    try:
        return create_client(supabase_url, supabase_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error conectando a Supabase: {str(e)}")


def get_stripe_ready():
    stripe_secret = get_required_env("STRIPE_SECRET_KEY")
    stripe.api_key = stripe_secret


def get_price_map() -> Dict[str, Dict[str, Any]]:
    return {
        get_required_env("PRICE_ID_BASICO"): {
            "plan": "basico",
            "contact_limit": 100,
        },
        get_required_env("PRICE_ID_PROFESIONAL"): {
            "plan": "profesional",
            "contact_limit": 450,
        },
        get_required_env("PRICE_ID_AVANZADO"): {
            "plan": "avanzado",
            "contact_limit": 1000,
        },
    }


def get_plan_catalog() -> Dict[str, Dict[str, Any]]:
    return {
        "basico": {
            "price_id": get_required_env("PRICE_ID_BASICO"),
            "contact_limit": 100,
        },
        "profesional": {
            "price_id": get_required_env("PRICE_ID_PROFESIONAL"),
            "contact_limit": 450,
        },
        "avanzado": {
            "price_id": get_required_env("PRICE_ID_AVANZADO"),
            "contact_limit": 1000,
        },
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
def send_meta_purchase_event(email: Optional[str], value: Optional[float], currency: str = "USD") -> None:
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
                "custom_data": {
                    "currency": currency,
                    "value": float(value or 0),
                },
            }
        ]
    }

    try:
        url = f"https://graph.facebook.com/v19.0/{meta_pixel_id}/events?access_token={meta_access_token}"
        r = requests.post(url, json=payload, timeout=20)
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
        sub = stripe.Subscription.retrieve(subscription_id)
        return sub
    except Exception as e:
        print("No se pudo obtener la suscripción:", str(e))
        return None


def resolve_plan_from_price_id(price_id: Optional[str]) -> Dict[str, Any]:
    price_map = get_price_map()

    if price_id and price_id in price_map:
        return {
            "price_id": price_id,
            "plan": price_map[price_id]["plan"],
            "contact_limit": price_map[price_id]["contact_limit"],
        }

    return {
        "price_id": price_id,
        "plan": None,
        "contact_limit": 0,
    }


# =========================
# HELPERS DB
# =========================
def upsert_user_by_email(
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
    sb = get_supabase_client()

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
    if plan is not None:
        data["plan"] = plan
    if contact_limit is not None:
        data["contact_limit"] = contact_limit

    if existing.data:
        sb.table("users").update(data).eq("email", email).execute()
    else:
        data["email"] = email
        data["created_at"] = now_iso()
        if "contacts_used" not in data:
            data["contacts_used"] = 0
        sb.table("users").insert(data).execute()


def update_user_by_customer_id(
    customer_id: str,
    status: Optional[str] = None,
    subscription_id: Optional[str] = None,
    access_active: Optional[bool] = None,
    price_id: Optional[str] = None,
    current_period_end: Optional[str] = None,
    plan: Optional[str] = None,
    contact_limit: Optional[int] = None,
) -> None:
    sb = get_supabase_client()

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
    if plan is not None:
        data["plan"] = plan
    if contact_limit is not None:
        data["contact_limit"] = contact_limit

    sb.table("users").update(data).eq("stripe_customer_id", customer_id).execute()


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    sb = get_supabase_client()
    result = sb.table("users").select("*").eq("email", email).limit(1).execute()
    if result.data:
        return result.data[0]
    return None


def count_user_contacts(user_id: str) -> int:
    sb = get_supabase_client()
    result = sb.table("contacts").select("id").eq("user_id", user_id).execute()
    return len(result.data or [])


def sync_contacts_used(user_id: str) -> int:
    sb = get_supabase_client()
    total = count_user_contacts(user_id)
    sb.table("users").update(
        {
            "contacts_used": total,
            "updated_at": now_iso(),
        }
    ).eq("id", user_id).execute()
    return total


# =========================
# RUTAS BÁSICAS
# =========================
@app.get("/")
def root():
    return {"ok": True, "message": "Backend activo"}


@app.get("/health")
def health():
    return {
        "status": "running",
        "time": time.time(),
        "service": "backend-suscripciones",
    }


@app.get("/config-check")
def config_check():
    checks = {
        "STRIPE_SECRET_KEY": bool(get_env("STRIPE_SECRET_KEY")),
        "STRIPE_WEBHOOK_SECRET": bool(get_env("STRIPE_WEBHOOK_SECRET")),
        "SUPABASE_URL": bool(get_env("SUPABASE_URL")),
        "SUPABASE_SERVICE_ROLE_KEY": bool(get_env("SUPABASE_SERVICE_ROLE_KEY")),
        "APP_URL": bool(get_env("APP_URL")),
        "PRICE_ID_BASICO": bool(get_env("PRICE_ID_BASICO")),
        "PRICE_ID_PROFESIONAL": bool(get_env("PRICE_ID_PROFESIONAL")),
        "PRICE_ID_AVANZADO": bool(get_env("PRICE_ID_AVANZADO")),
        "META_PIXEL_ID": bool(get_env("META_PIXEL_ID")),
        "META_ACCESS_TOKEN": bool(get_env("META_ACCESS_TOKEN")),
    }

    supabase_ok = False
    supabase_error = None
    try:
        _ = get_supabase_client()
        supabase_ok = True
    except Exception as e:
        supabase_error = str(e)

    return {
        "ok": True,
        "env": checks,
        "supabase_connected": supabase_ok,
        "supabase_error": supabase_error,
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
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": plan_data["price_id"], "quantity": 1}],
            customer_email=email,
            success_url=f"{app_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{app_url}/cancel",
            metadata={
                "email": email,
                "selected_plan": plan,
            },
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

    user = get_user_by_email(email)

    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    if not user.get("access_active"):
        raise HTTPException(status_code=403, detail="Suscripción inactiva")

    user_id = user["id"]
    contact_limit = int(user.get("contact_limit") or 0)

    current_contacts = sync_contacts_used(user_id)

    if current_contacts >= contact_limit:
        raise HTTPException(
            status_code=403,
            detail=f"Límite alcanzado. Tu plan permite {contact_limit} contactos activos.",
        )

    sb = get_supabase_client()
    insert_result = sb.table("contacts").insert(
        {
            "user_id": user_id,
            "name": name,
            "phone": phone if phone else None,
            "notes": notes if notes else None,
        }
    ).execute()

    updated_contacts = sync_contacts_used(user_id)

    return {
        "ok": True,
        "message": "Contacto creado correctamente",
        "contact": insert_result.data[0] if insert_result.data else None,
        "contacts_used": updated_contacts,
        "contact_limit": contact_limit,
        "remaining": max(contact_limit - updated_contacts, 0),
    }


@app.get("/my-plan")
def my_plan(email: str):
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Falta email")

    user = get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user_id = user["id"]
    current_contacts = sync_contacts_used(user_id)

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
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
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

    event_type = event["type"]
    obj = event["data"]["object"]

    print("Evento recibido:", event_type)

    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        email = get_customer_email_from_session(obj)

        subscription = get_subscription(subscription_id)
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

        if email:
            upsert_user_by_email(
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
        elif customer_id:
            update_user_by_customer_id(
                customer_id=customer_id,
                status=status,
                subscription_id=subscription_id,
                access_active=True,
                price_id=plan_info["price_id"],
                current_period_end=current_period_end,
                plan=plan_info["plan"],
                contact_limit=plan_info["contact_limit"],
            )

        amount_total = (obj.get("amount_total") or 0) / 100
        currency = (obj.get("currency") or "usd").upper()
        send_meta_purchase_event(email=email, value=amount_total, currency=currency)

    elif event_type == "invoice.paid":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        subscription = get_subscription(subscription_id)
        status = subscription.get("status") if subscription else "active"
        current_period_end = unix_to_iso(subscription.get("current_period_end")) if subscription else None

        resolved_price_id = None
        if subscription:
            items = subscription.get("items", {}).get("data", [])
            if items and items[0].get("price", {}).get("id"):
                resolved_price_id = items[0]["price"]["id"]

        plan_info = resolve_plan_from_price_id(resolved_price_id)

        if customer_id:
            update_user_by_customer_id(
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
            update_user_by_customer_id(
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
            update_user_by_customer_id(
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
            update_user_by_customer_id(
                customer_id=customer_id,
                status="canceled",
                subscription_id=subscription_id,
                access_active=False,
            )

    return JSONResponse({"received": True})
