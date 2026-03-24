import os
import hashlib
import requests
import stripe

from fastapi import FastAPI, Request, HTTPException
from supabase import create_client, Client

app = FastAPI()

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

stripe.api_key = STRIPE_SECRET_KEY

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# =========================
# FUNCIONES AUXILIARES
# =========================
def sha256_value(value: str) -> str:
    """Hashea un valor para Meta CAPI."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def update_user_status_by_customer_id(customer_id: str, status: str, subscription_id: str | None = None):
    """
    Actualiza el estado del usuario en Supabase usando stripe_customer_id.
    Espera una tabla users con columna stripe_customer_id.
    """
    data = {"subscription_status": status}

    if subscription_id:
        data["stripe_subscription_id"] = subscription_id

    response = (
        supabase.table("users")
        .update(data)
        .eq("stripe_customer_id", customer_id)
        .execute()
    )
    return response


def send_meta_purchase_event(email: str | None, value: float | int | None, currency: str = "USD"):
    """
    Envía una conversión a Meta CAPI.
    Se recomienda enviar email hasheado si lo tienes.
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
                "event_time": int(__import__("time").time()),
                "action_source": "website",
                "user_data": user_data,
                "custom_data": {
                    "currency": currency,
                    "value": float(value or 0)
                }
            }
        ]
    }

    url = f"https://graph.facebook.com/v19.0/{META_PIXEL_ID}/events?access_token={META_ACCESS_TOKEN}"
    r = requests.post(url, json=payload, timeout=20)
    print("Meta CAPI status:", r.status_code, r.text)


def get_customer_email_from_session(session_obj: dict) -> str | None:
    """
    Intenta sacar email desde checkout.session.completed.
    """
    customer_details = session_obj.get("customer_details") or {}
    email = customer_details.get("email")
    if email:
        return email

    customer_email = session_obj.get("customer_email")
    if customer_email:
        return customer_email

    return None


# =========================
# RUTAS BÁSICAS
# =========================
@app.get("/")
def root():
    return {"ok": True, "message": "Backend activo"}

@app.get("/health")
def health():
    return {"status": "running"}


# =========================
# CREAR CHECKOUT DE STRIPE
# =========================
@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    """
    Crea sesión de Stripe Checkout para suscripción.
    Requiere recibir:
    {
      "price_id": "price_xxx",
      "email": "cliente@correo.com"
    }
    """
    body = await request.json()
    price_id = body.get("price_id")
    email = body.get("email")

    if not price_id:
        raise HTTPException(status_code=400, detail="Falta price_id")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/cancel",
        )
        return {"checkout_url": session.url}
    except Exception as e:
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
            secret=STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Payload inválido")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Firma inválida")

    event_type = event["type"]
    obj = event["data"]["object"]

    print("Evento recibido:", event_type)

    # =========================
    # 1) CHECKOUT COMPLETADO
    # =========================
    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        email = get_customer_email_from_session(obj)

        # activar usuario
        if customer_id:
            update_user_status_by_customer_id(
                customer_id=customer_id,
                status="active",
                subscription_id=subscription_id
            )

        # enviar compra a meta
        amount_total = obj.get("amount_total", 0) / 100
        currency = (obj.get("currency") or "usd").upper()
        send_meta_purchase_event(email=email, value=amount_total, currency=currency)

    # =========================
    # 2) FACTURA PAGADA
    # =========================
    elif event_type == "invoice.paid":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        if customer_id:
            update_user_status_by_customer_id(
                customer_id=customer_id,
                status="active",
                subscription_id=subscription_id
            )

    # =========================
    # 3) FACTURA FALLIDA
    # =========================
    elif event_type == "invoice.payment_failed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")

        if customer_id:
            update_user_status_by_customer_id(
                customer_id=customer_id,
                status="past_due",
                subscription_id=subscription_id
            )

    # =========================
    # 4) SUSCRIPCIÓN CANCELADA
    # =========================
    elif event_type == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        subscription_id = obj.get("id")

        if customer_id:
            update_user_status_by_customer_id(
                customer_id=customer_id,
                status="canceled",
                subscription_id=subscription_id
            )

    return {"received": True}
