import os
import uuid
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, EmailStr, condecimal, field_validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

import stripe

# ---------------------------
# Logging Configuration
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# Environment & Stripe Setup
# ---------------------------
load_dotenv()
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
if not STRIPE_PUBLIC_KEY or not STRIPE_SECRET_KEY:
    logger.error("Stripe keys are not set in .env file")
    raise Exception("Stripe keys must be set in .env file.")

stripe.api_key = STRIPE_SECRET_KEY
MY_DOMAIN = "http://localhost:8000"

# ---------------------------
# Database Setup (SQLite)
# ---------------------------
DATABASE_URL = "sqlite:///./payments.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# PaymentLink model
class PaymentLink(Base):
    __tablename__ = "payment_links"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True)
    order_id = Column(String, index=True)
    email = Column(String, index=True)
    amount = Column(Float)  # amount in dollars
    created_at = Column(DateTime, default=datetime.now)
    status = Column(String, default="pending")  # pending, paid, expired


Base.metadata.create_all(bind=engine)


# ---------------------------
# Pydantic Model
# ---------------------------
class PaymentLinkCreate(BaseModel):
    order_id: str
    email: EmailStr
    amount: condecimal(gt=0)

    @field_validator("order_id")
    def order_id_not_empty(cls, v):
        if not v.strip():
            raise ValueError("order_id cannot be empty")
        return v


# ---------------------------
# Dependency to Get DB Session
# ---------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------
# FastAPI App & Middleware
# ---------------------------
app = FastAPI()


# Rate Limiting Middleware
class RateLimitMiddleware:
    def __init__(self, app, rate_limit=10, time_window=60):
        self.app = app
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.client_requests = {}  # {client_ip: [timestamps]}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = scope.get("client")[0]
        now = time.time()
        self.client_requests.setdefault(client_ip, [])
        # Remove outdated timestamps
        self.client_requests[client_ip] = [
            ts for ts in self.client_requests[client_ip] if now - ts < self.time_window
        ]

        if len(self.client_requests[client_ip]) >= self.rate_limit:
            response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            await response(scope, receive, send)
            return

        self.client_requests[client_ip].append(now)
        await self.app(scope, receive, send)


app.add_middleware(RateLimitMiddleware, rate_limit=10, time_window=60)


class ContentSecurityPolicyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                csp = (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
                    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                    "font-src 'self' data:; "
                    "img-src 'self' data: https://fastapi.tiangolo.com;"
                )
                message.setdefault("headers", [])
                message["headers"].append(
                    (b"content-security-policy", csp.encode("utf-8"))
                )

            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(ContentSecurityPolicyMiddleware)

# Mount static files and provide a route for sw.js at the root.
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


# ---------------------------
# Endpoints
# ---------------------------


# Create Payment Link with Transaction Handling
@app.post("/create_payment_link")
async def create_payment_link(data: PaymentLinkCreate, db: Session = Depends(get_db)):
    try:
        token = uuid.uuid4().hex
        with db.begin():
            payment_link = PaymentLink(
                token=token,
                order_id=data.order_id,
                email=data.email,
                amount=float(data.amount),
                created_at=datetime.now(),
                status="pending",
            )
            db.add(payment_link)
        db.refresh(payment_link)
        payment_url = f"/pay/{payment_link.token}"
        logger.info(f"Payment link created: {payment_link.token}")
        return {"payment_url": payment_url}
    except Exception as e:
        logger.exception("Error creating payment link.")
        raise HTTPException(status_code=500, detail=f"Error creating payment link: {e}")


# Render Payment Page and Update Expired Status if Needed
@app.get("/pay/{token}", response_class=HTMLResponse)
async def pay_page(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        payment_link = db.query(PaymentLink).filter(PaymentLink.token == token).first()
        if not payment_link:
            return HTMLResponse(
                content="<h3>Invalid payment link.</h3>", status_code=404
            )

        # Check expiration: If more than 5 minutes passed, mark as expired.
        if datetime.now() > payment_link.created_at + timedelta(minutes=5):
            if payment_link.status not in ("paid", "expired"):
                payment_link.status = "expired"
                db.commit()
                logger.info(f"Payment link expired: {payment_link.token}")
            return templates.TemplateResponse(
                "expired.html",
                {"request": request, "message": "This payment link has expired."},
            )

        if payment_link.status == "paid":
            return templates.TemplateResponse(
                "paid.html",
                {"request": request, "message": "Payment has already been made."},
            )
        if payment_link.status == "expired":
            return templates.TemplateResponse(
                "expired.html",
                {"request": request, "message": "This payment link has expired."},
            )

        context = {
            "request": request,
            "amount": payment_link.amount,
            "order_id": payment_link.order_id,
            "email": payment_link.email,
            "token": payment_link.token,
            "stripe_public_key": STRIPE_PUBLIC_KEY,
        }
        return templates.TemplateResponse("payment_page.html", context)
    except Exception as e:
        logger.exception("Error rendering payment page.")
        return HTMLResponse(content=f"<h3>Error: {e}</h3>", status_code=500)


# Create Stripe Checkout Session with Error Handling & Logging
@app.post("/create_checkout_session")
async def create_checkout_session(
    token: str = Form(...), db: Session = Depends(get_db)
):
    try:
        payment_link = db.query(PaymentLink).filter(PaymentLink.token == token).first()
        if not payment_link:
            raise HTTPException(status_code=404, detail="Invalid payment link.")
        if datetime.now() > payment_link.created_at + timedelta(minutes=5):
            raise HTTPException(status_code=400, detail="Payment link expired.")
        if payment_link.status == "paid":
            raise HTTPException(status_code=400, detail="Payment already completed.")

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": int(payment_link.amount * 100),
                            "product_data": {"name": f"Order {payment_link.order_id}"},
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                customer_email=payment_link.email,
                success_url=MY_DOMAIN
                + f"/payment_success?session_id={{CHECKOUT_SESSION_ID}}&token={payment_link.token}",
                cancel_url=MY_DOMAIN + f"/payment_cancelled?token={payment_link.token}",
                metadata={"payment_token": payment_link.token},
            )
        except Exception as stripe_error:
            logger.exception(f"Stripe error during session creation: {stripe_error}")
            raise HTTPException(
                status_code=500, detail="Stripe error: " + str(stripe_error)
            )

        return RedirectResponse(url=checkout_session.url, status_code=303)
    except Exception as e:
        logger.exception("Error creating checkout session.")
        raise HTTPException(
            status_code=500, detail=f"Error creating checkout session: {e}"
        )


# Payment Success: Verify Stripe Session and Update Status Accordingly
@app.get("/payment_success", response_class=HTMLResponse)
async def payment_success(
    request: Request, token: str, session_id: str, db: Session = Depends(get_db)
):
    try:
        payment_link = db.query(PaymentLink).filter(PaymentLink.token == token).first()
        if not payment_link:
            return HTMLResponse(
                content="<h3>Invalid payment link.</h3>", status_code=404
            )
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
        except Exception as stripe_error:
            logger.exception(f"Stripe error during session retrieval: {stripe_error}")
            return HTMLResponse(
                content="<h3>Error retrieving Stripe session.</h3>", status_code=500
            )

        if checkout_session.payment_status != "paid":
            logger.info(f"Stripe session not paid: {checkout_session.payment_status}")
            return HTMLResponse(
                content="<h3>Payment has not been completed.</h3>", status_code=400
            )
        if checkout_session.metadata.get("payment_token") != token:
            logger.warning("Stripe session metadata does not match payment token.")
            return HTMLResponse(
                content="<h3>Session mismatch: Payment token does not match.</h3>",
                status_code=400,
            )

        if payment_link.status != "paid":
            payment_link.status = "paid"
            db.commit()
            logger.info(f"Payment marked as paid for token: {payment_link.token}")

        return templates.TemplateResponse(
            "payment_success.html",
            {"request": request, "message": "Payment successful!"},
        )
    except Exception as e:
        logger.exception("Error in payment success endpoint.")
        return HTMLResponse(content=f"<h3>Error: {e}</h3>", status_code=500)


# Payment Cancelled Endpoint
@app.get("/payment_cancelled", response_class=HTMLResponse)
async def payment_cancelled(request: Request, token: str):
    return templates.TemplateResponse(
        "payment_cancelled.html",
        {"request": request, "message": "Payment was cancelled."},
    )


# Optional: Endpoint to Cleanup Expired Payment Links
@app.delete("/cleanup_expired")
async def cleanup_expired(db: Session = Depends(get_db)):
    try:
        expired_time = datetime.now() - timedelta(minutes=5)
        expired_links = (
            db.query(PaymentLink)
            .filter(
                PaymentLink.created_at < expired_time, PaymentLink.status == "pending"
            )
            .all()
        )
        count = len(expired_links)
        for link in expired_links:
            link.status = "expired"
        db.commit()
        logger.info(f"Cleaned up {count} expired payment links.")
        return {"cleaned": count}
    except Exception as e:
        logger.exception("Error cleaning up expired links.")
        raise HTTPException(
            status_code=500, detail=f"Error cleaning up expired links: {e}"
        )


@app.get("/payments")
async def list_payments(
    page: int = 1,
    per_page: int = 10,
    order_id: Optional[str] = None,
    email: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    try:
        query = db.query(PaymentLink)
        if order_id:
            query = query.filter(PaymentLink.order_id.like(f"%{order_id}%"))
        if email:
            query = query.filter(PaymentLink.email.like(f"%{email}%"))
        if status:
            query = query.filter(PaymentLink.status == status)

        total = query.count()
        payments = (
            query.order_by(PaymentLink.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        results = []
        for payment in payments:
            results.append(
                {
                    "id": payment.id,
                    "token": payment.token,
                    "order_id": payment.order_id,
                    "email": payment.email,
                    "amount": payment.amount,
                    "created_at": payment.created_at.isoformat(),
                    "status": payment.status,
                }
            )

        return {"page": page, "per_page": per_page, "total": total, "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving payments: {e}")


@app.get("/payments/export")
async def export_payments_csv(
    order_id: Optional[str] = None,
    email: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    try:
        import csv
        from io import StringIO

        query = db.query(PaymentLink)
        if order_id:
            query = query.filter(PaymentLink.order_id.like(f"%{order_id}%"))
        if email:
            query = query.filter(PaymentLink.email.like(f"%{email}%"))
        if status:
            query = query.filter(PaymentLink.status == status)

        payments = query.order_by(PaymentLink.created_at.desc()).all()

        si = StringIO()
        writer = csv.writer(si)

        writer.writerow(
            ["ID", "Token", "Order ID", "Email", "Amount", "Created At", "Status"]
        )

        for payment in payments:
            writer.writerow(
                [
                    payment.id,
                    payment.token,
                    payment.order_id,
                    payment.email,
                    payment.amount,
                    payment.created_at.isoformat(),
                    payment.status,
                ]
            )

        csv_content = si.getvalue()
        response = Response(content=csv_content, media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=payments.csv"
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exporting CSV: {e}")
