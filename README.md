# [QFlow] – Queue Management System

A web-based queue system for the registrar/document-processing office. Members request documents, get a queue ticket on their own phone, and are notified in real time when they are called.

Features
- Member accounts (student number + password)
- Ticket generation with a daily ticket limit
- Cash option (pay at the window) or online payment (GCash/Maya via PayMongo)
- Pay-in-advance: pay for multiple documents in one payment, then activate the ticket on arrival
- Real-time queue updates and notifications (Flask-SocketIO)
- Admin dashboard: call tickets, view history and customers, change settings
- Ticket recovery if the browser session is lost

Tech Stack
| Part | Tool | Why |
|---|---|---|
| Backend | Python, Flask | [your reason] |
| Database | SQLite + SQLAlchemy | [your reason] |
| Real-time | Flask-SocketIO | live queue updates without refreshing |
| Payments | PayMongo API (sandbox) | supports GCash/Maya |
| Frontend | HTML, CSS, Jinja templates | [your reason] |

## Security
| Layer | Method | Where |
|---|---|---|
| Passwords | Salted hashing (Werkzeug PBKDF2/scrypt) | `models.py` |
| Stored names | Fernet (AES) field-level encryption | `crypto_fields.py`, `models.py` |
| Cookies | HMAC-signed (itsdangerous), bound to IP + User-Agent | `apps12.py` |
| Payment webhooks | HMAC-SHA256 signature verification | `payments.py` |
| Secrets | Environment variables, never committed (`.env` is gitignored) | `apps12.py` |
| Transport | HTTPS (TLS) when deployed on [Render] | deployment |



Generate a Fernet key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run:
```bash
python apps12.py
```
Open `http://localhost:5001`. Create more admins with `flask create_admin`.

## Project Structure
- `apps12.py` – routes, SocketIO handlers, app setup
- `models.py` – database models and password hashing
- `services.py` – queue and ticket logic
- `payments.py` – PayMongo integration and webhook verification
- `crypto_fields.py` – encrypted column type
- `cli.py`, `error_handlers.py` – admin commands and error pages
- `templates/`, `static/` – frontend

## Known Limitations
- PayMongo runs in sandbox/test mode
