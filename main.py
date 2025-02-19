import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Form, Depends, Response
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware


from pydantic import BaseModel, EmailStr, condecimal, validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

import stripe

# Load environment variables from .env file
load_dotenv()
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
DOMAIN = "http://localhost:8000"
EXPIRATION_MINUTES = 5

if not STRIPE_PUBLIC_KEY or not STRIPE_SECRET_KEY:
    raise Exception("Stripe keys must be set in .env file.")

stripe.api_key = STRIPE_SECRET_KEY

# Database setup (using SQLite)
DATABASE_URL = "sqlite:///./payments.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# Define models
class PaymentLink(Base):
    __tablename__ = "payment_links"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True)
    order_id = Column(String, index=True)
    email = Column(String, index=True)
    amount = Column(Float)  # amount in dollars
    created_at = Column(DateTime, default=datetime.now)
    status = Column(String, default="pending")  # pending, paid


# Create tables
Base.metadata.create_all(bind=engine)


# Pydantic models
class PaymentLinkCreate(BaseModel):
    order_id: str
    email: EmailStr
    amount: condecimal(gt=0)  # amount must be > 0

    @validator("order_id")
    def order_id_not_empty(cls, v):
        if not v.strip():
            raise ValueError("order_id cannot be empty")
        return v


# DB dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app = FastAPI()

# Lists for allowed sources
allowed_script_sources = [
    "'self'",
    "https://js.stripe.com",
    "https://cdn.jsdelivr.net",
    "https://code.jquery.com",
]

allowed_style_sources = [
    "'self'",
    "'unsafe-inline'",
    "https://cdn.jsdelivr.net",
    "https://fonts.googleapis.com",
]

allowed_font_sources = [
    "'self'",
    "https://js.stripe.com",
    "https://cdn.jsdelivr.net",
    "https://fonts.gstatic.com",
    "data:",
]

allowed_img_sources = ["'self'", "data:"]


class ContentSecurityPolicyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Join the allowed sources from the lists into strings
        script_src = " ".join(allowed_script_sources)
        style_src = " ".join(allowed_style_sources)
        font_src = " ".join(allowed_font_sources)
        img_src = " ".join(allowed_img_sources)

        # Construct the CSP header value
        csp = (
            f"default-src 'self'; "
            f"script-src {script_src}; "
            f"style-src {style_src}; "
            f"font-src {font_src}; "
            f"img-src {img_src};"
        )
        response.headers["Content-Security-Policy"] = csp
        return response


app.add_middleware(ContentSecurityPolicyMiddleware)


app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.post("/create_payment_link")
async def create_payment_link(data: PaymentLinkCreate, db: Session = Depends(get_db)):
    try:
        token = uuid.uuid4().hex
        payment_link = PaymentLink(
            token=token,
            order_id=data.order_id,
            email=data.email,
            amount=float(data.amount),
            created_at=datetime.now(),
            status="pending",
        )
        db.add(payment_link)
        db.commit()
        db.refresh(payment_link)
        payment_url = f"/pay/{payment_link.token}"
        return {"payment_url": payment_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating payment link: {e}")


@app.get("/pay/{token}", response_class=HTMLResponse)
async def pay_page(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        payment_link = db.query(PaymentLink).filter(PaymentLink.token == token).first()
        if not payment_link:
            return HTMLResponse(
                content="<h3>Invalid payment link.</h3>", status_code=404
            )

        if datetime.now() > payment_link.created_at + timedelta(
            minutes=EXPIRATION_MINUTES
        ):
            return templates.TemplateResponse(
                "expired.html",
                {"request": request, "message": "This payment link has expired."},
            )

        if payment_link.status == "paid":
            return templates.TemplateResponse(
                "paid.html",
                {"request": request, "message": "Payment has already been made."},
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
        return HTMLResponse(content=f"<h3>Error: {e}</h3>", status_code=500)


# create a Stripe Checkout session
@app.post("/create_checkout_session")
async def create_checkout_session(
    token: str = Form(...), db: Session = Depends(get_db)
):
    try:
        payment_link = db.query(PaymentLink).filter(PaymentLink.token == token).first()
        if not payment_link:
            raise HTTPException(status_code=404, detail="Invalid payment link.")

        if datetime.utcnow() > payment_link.created_at + timedelta(minutes=5):
            raise HTTPException(status_code=400, detail="Payment link expired.")

        if payment_link.status == "paid":
            raise HTTPException(status_code=400, detail="Payment already completed.")

        # {CHECKOUT_SESSION_ID} placeholder will be replaced by Stripe
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(
                            payment_link.amount * 100
                        ),  # amount in cents
                        "product_data": {"name": f"Order {payment_link.order_id}"},
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            customer_email=payment_link.email,
            success_url=DOMAIN
            + f"/payment_success?session_id={{CHECKOUT_SESSION_ID}}&token={payment_link.token}",
            cancel_url=DOMAIN + f"/payment_cancelled?token={payment_link.token}",
            metadata={
                "payment_token": payment_link.token
            },  # bind session to payment token
        )

        return RedirectResponse(url=checkout_session.url, status_code=303)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error creating checkout session: {e}"
        )


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

        checkout_session = stripe.checkout.Session.retrieve(session_id)
        if checkout_session.payment_status != "paid":
            return HTMLResponse(
                content="<h3>Payment has not been completed.</h3>", status_code=400
            )

        if checkout_session.metadata.get("payment_token") != token:
            return HTMLResponse(
                content="<h3>Session mismatch: Payment token does not match.</h3>",
                status_code=400,
            )

        if payment_link.status != "paid":
            payment_link.status = "paid"
            db.commit()

        return templates.TemplateResponse(
            "payment_success.html",
            {"request": request, "message": "Payment successful!"},
        )
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error: {e}</h3>", status_code=500)


@app.get("/payment_cancelled", response_class=HTMLResponse)
async def payment_cancelled(request: Request, token: str):
    return templates.TemplateResponse(
        "payment_cancelled.html",
        {"request": request, "message": "Payment was cancelled."},
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
