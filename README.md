# Payment Link System

A FastAPI-based application that integrates with Stripe to create and manage payment links. Users can generate payment links, process payments through Stripe Checkout, and track the payment status.

## Features
- Generate unique payment links.
- Stripe Checkout integration for secure payments.
- Payment links expire after 5 minutes.
- View payment statuses (Pending, Paid, Expired).
- Export payment data to CSV.

## Screenshots
#### Home Page
<img src="screenshots/endpoints_page.png" alt="Home Page" width="600"/>

#### Payment Link
<img src="screenshots/payment_link.png" alt="Payment Link" width="600"/>

#### Payment Page
<img src="screenshots/payment_page.png" alt="Payment Page" width="600"/>

#### Success Page
<img src="screenshots/success_page.png" alt="Success Page" width="400"/>

#### Cancelled Page
<img src="screenshots/cancelled_page.png" alt="Cancelled Page" width="400"/>

#### Expired Page
<img src="screenshots/expired_page.png" alt="Expired Page" width="400"/>


## Configuration
- Set your Stripe API keys in the `.env` file:
  ```
  STRIPE_PUBLIC_KEY=<your_public_key>
  STRIPE_SECRET_KEY=<your_secret_key>
  ```
## Run
`uvicorn main:app --reload`