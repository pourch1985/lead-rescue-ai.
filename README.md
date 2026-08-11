# Lead Rescue AI — Unified SaaS v6

Lead Rescue AI is now structured as one customer-facing application. Customers log into one workspace and use the Control Center, Leads, Conversations, Automations, Appointments, Integrations, Billing, and Settings from the same navigation.

## What customers see
- Control Center with recovered revenue, due follow-ups, appointments, recent conversations, newest leads, and connection readiness
- Leads pipeline with scoring, status, consent, source, value, follow-up scheduling, and activity history
- Unified Conversations inbox with AI-assisted follow-up generation and direct Email/SMS sending
- Automations center for due-lead processing and recovery sequences
- Appointments with Google Calendar connection and ICS export
- Integrations center for Meta Lead Ads and Google Calendar
- Billing center for Starter / Growth / Pro subscriptions
- Account security, email verification, password reset, onboarding, and business settings

## What works behind the scenes
- OpenAI: AI reply generation
- Resend: outbound email
- Twilio: outbound SMS
- Meta: Facebook/Instagram lead capture
- Google Calendar: appointment sync
- Stripe: subscriptions and billing state
- PostgreSQL: production customer/business data

Customers do not need to use those services as separate apps. They connect them once in Lead Rescue AI and operate from the Lead Rescue AI workspace.

## Local run
1. Copy `.env.example` to `.env` and fill in the secrets you want to use.
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python app.py`
4. Open `http://localhost:5000`

## Production
Use PostgreSQL by setting `DATABASE_URL`. Set a strong `SECRET_KEY`, `ENCRYPTION_KEY`, `APP_BASE_URL`, `COOKIE_SECURE=1`, Stripe webhook secret, and provider credentials. The included Dockerfile, docker-compose.yml, and Procfile support common deployment platforms.

## Required credentials for full live functionality
- `OPENAI_API_KEY`
- `RESEND_API_KEY`, sender email/name
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- `STRIPE_SECRET_KEY`, price IDs, webhook secret
- `META_APP_ID`, `META_APP_SECRET`, webhook verify token/app secret
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `DATABASE_URL` for PostgreSQL

## Verification status
`app.py` and `production.py` pass Python compilation. A full Flask runtime test could not be run in the build workspace because Flask is not installed there; install `requirements.txt` in the deployment environment before starting the server.
