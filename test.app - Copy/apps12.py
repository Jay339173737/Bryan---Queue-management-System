import os
from dotenv import load_dotenv
load_dotenv()

import sys, io
from datetime import datetime, timedelta,date
from flask import Flask, render_template, request, redirect, url_for, flash, session, make_response, jsonify
from models import db, AdminUser, QueueCustomer, QueueTicket, SystemConfig,TicketAcknowledgement,AdvancePayment
from flask_socketio import SocketIO, emit, join_room
from cli import register_cli_commands
import socket
import secrets
from markupsafe import Markup
from sqlalchemy.exc import IntegrityError
from error_handlers import register_error_handlers
from itsdangerous import URLSafeSerializer, BadSignature
import getpass
import hashlib
import uuid
from payments import create_checkout_session, verify_webhook, PAYMONGO_ENABLED, SERVICE_FEE_CENTAVOS
from services import (fetch_waiting_queue, fetch_active_service, count_todays_tickets,
                      check_ticket_limit, verify_duplicate_ticket, retrieve_or_create_customer,
                      push_queue_update, retrieve_config_value, update_config_value,
                      require_admin_access, price_for_document, DOCUMENT_PRICES, DOCUMENT_LABELS,
                      is_login_locked, record_failed_login, clear_failed_logins,)


# ---------- config ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

_env_secret = os.environ.get("SECRET_KEY")
if not _env_secret:
    _env_secret = secrets.token_hex(32)
    print("⚠️  SECRET_KEY not set in environment - using a random one for "
          "this process only. Set SECRET_KEY before deploying, or every "
          "restart will log everyone out.")
app.config['SECRET_KEY'] = _env_secret
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "queue.db")}'

# --- ADD THIS BLOCK TO FIX THE CRASH ---
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "connect_args": {"check_same_thread": False},
    "poolclass": None  
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")
register_error_handlers(app)
register_cli_commands(app)  
LAN_IP = socket.gethostbyname(socket.gethostname())

# --- ADD THIS BLOCK FOR RENDER DEPLOYMENT ---
with app.app_context():
    db.create_all()
    # Check if admin exists, if not create default
    if not AdminUser.query.filter_by(username="admin").first():
        _default_admin = AdminUser(username="admin", role="admin")
        _default_admin.set_password(os.environ.get("ADMIN_DEFAULT_PASSWORD", "admin123"))
        db.session.add(_default_admin)
        db.session.commit()
        print("✅ Default admin created on startup - CHANGE THIS PASSWORD "
              "before deploying (set ADMIN_DEFAULT_PASSWORD or create a "
              "new admin with 'flask create_admin').")
    # Check if config exists
    if not SystemConfig.query.filter_by(config_key="daily_ticket_limit").first():
        db.session.add(SystemConfig(config_key="daily_ticket_limit", config_value="300"))
        db.session.commit()
# --------------------------------------------


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


# ---------- IP / UA bound browser lock ----------
def _ser():
    return URLSafeSerializer(app.secret_key, salt='lock')

def _fingerprint():
    # simple fingerprint: IP + first 64 chars of UA
    return f"{request.remote_addr}|{request.headers.get('User-Agent', '')[:64]}"

def _set_lock(resp, tid):
    val = _ser().dumps({'tid': tid, 'fp': _fingerprint()})
    resp.set_cookie('lock', val, max_age=86400, httponly=True, samesite='Lax')

def _clear_lock(resp):
    resp.set_cookie('lock', '', expires=0)

def _get_lock():
    try:
        data = _ser().loads(request.cookies.get('lock', ''))
        if data.get('fp') != _fingerprint():
            return None          # wrong IP or UA
        return data['tid']
    except BadSignature:
        return None

# ========================================
# Real-time Communication Handlers
# ========================================

@socketio.on("join")
def handle_customer_join(data):
    """Process customer joining their notification room.

    The room id MUST come from the server-side session, never from the
    client payload - otherwise anyone can pass another customer's id and
    silently receive their "your turn" / recall notifications.
    """
    customer_id = session.get("user_id")
    if customer_id is not None:
        notification_room = str(customer_id)
        join_room(notification_room)
        print(f"✅ Customer ID {notification_room} connected to notification system")


# ========================================
# Public-Facing Routes
# ========================================

@app.route("/")
def landing_page():
    tid = _get_lock()
    if tid:
        if QueueTicket.query.filter(
            QueueTicket.id == tid,
            QueueTicket.ticket_status.in_(['waiting', 'serving'])
        ).first():
            session["user_id"] = QueueTicket.query.get(tid).customer_ref_id
            return redirect(url_for("customer_portal"))
            
       # --- ADDED: Fetch resume time for landing page ---
    resume_time = retrieve_config_value('resume_time_display', '')

    session_customer_name = None
    session_customer_id = None
    session_customer_firstname = None
    session_customer_lastname = None
    if session.get("authenticated"):
        customer = QueueCustomer.query.get(session["user_id"])
        if customer:
            session_customer_name = customer.get_display_name()
            session_customer_id = customer.student_number
            session_customer_firstname = customer.firstname
            session_customer_lastname = customer.lastname

    return render_template("home.html", resume_time=resume_time,
                            session_customer_name=session_customer_name,
                            session_customer_id=session_customer_id,
                            session_customer_firstname=session_customer_firstname,
                            session_customer_lastname=session_customer_lastname,
                            prices=DOCUMENT_PRICES, labels=DOCUMENT_LABELS)

@app.route("/get_ticket", methods=["POST"])
def process_ticket_request():
    """
    Create a new queue ticket for a student or guest.
    Prevents duplicate tickets while one is already active.
    """
    customer_type = request.form.get("user_type", "").strip().lower()
    bypass_warning = request.form.get("force", "false").lower() in ("1", "true", "yes")

    try:
        # ---------- identity: from account if logged in, else from form ----------
        if session.get("authenticated"):
            customer_record = QueueCustomer.query.get(session["user_id"])
            if not customer_record:
                flash("Account error. Please log in again.", "danger")
                return redirect(url_for("account_login"))
            customer_type = "student"
            student_id = customer_record.student_number
            given_name = customer_record.firstname
            family_name = customer_record.lastname
        else:
            customer_type = request.form.get("user_type", "").strip().lower()
            student_id    = (request.form.get("student_id") or "").strip() or None
            given_name    = (request.form.get("first_name")  or "").strip() or None
            family_name   = (request.form.get("last_name")   or "").strip() or None

        bypass_warning = request.form.get("force", "false").lower() in ("1", "true", "yes")
        visit_purpose = request.form.get("reason", "").strip()
        

        # ---------- basic validation ----------
        if customer_type not in ("student", "guest"):
            flash("Invalid customer type specified.", "danger")
            return redirect(url_for("landing_page"))

        # ---------- build final purpose ----------
        amount_due = 0
        if visit_purpose == "Document Request":
            docs = request.form.getlist("doc_list")          # list of check-boxes
            if not docs:
                flash("Please select at least one document.", "warning")
                return redirect(url_for("landing_page"))

            # Never trust a price submitted by the client - look each
            # document up in the server-side catalog, same as pay_advance.
            for doc_name in docs:
                price = price_for_document(doc_name)
                if price is None:
                    flash(f"'{doc_name}' is not a recognized document.", "danger")
                    return redirect(url_for("landing_page"))
                amount_due += price

            visit_purpose = "Documents - " + ", ".join(docs)

        elif visit_purpose in ("Other", "Other documents"):
            visit_purpose = request.form.get("reason_other", "").strip()
            if not visit_purpose:
                flash("Please provide a reason for your visit.", "warning")
                return redirect(url_for("landing_page"))
            
            # ---------- payment method (NEW) ----------
        payment_method = (request.form.get("payment_method") or "cash").strip().lower()

        if payment_method not in ("cash", "online"):
            flash("Invalid payment method selected.", "danger")
            return redirect(url_for("landing_page"))

        if payment_method == "online":
            if not session.get("authenticated"):
                signup_url = url_for("account_signup")
                msg = Markup(f'Online payment needs a free account. <a href="{signup_url}" style="color: inherit; text-decoration: underline; font-weight: bold;">Sign up here</a> — it only takes a minute!')
                flash(msg, "warning")
                return redirect(url_for("landing_page"))
            payment_status = "paid"
        else:
            payment_status = "unpaid"

        # ---------- duplicate-name warning (soft check) ----------
        if not bypass_warning and given_name and family_name and verify_duplicate_ticket(given_name, family_name):
            return render_template(
                "name_warning.html",
                first=given_name, last=family_name, sid=student_id,
                reason=visit_purpose, user_type=customer_type
            )

        # ---------- daily ticket limit ----------
        if check_ticket_limit():
            session["ticket_message"] = "⚠️  Today's ticket quota has been reached. Please return tomorrow."
            return redirect(url_for("customer_portal"))

       
        # ---------- get or create customer (SKIP if already logged in) ----------
        if not session.get("authenticated"):
            try:
                customer_record = retrieve_or_create_customer(customer_type, student_id, given_name, family_name)
            except IntegrityError:
                flash("An account with that student number already exists. If this is an error, please contact the office.", "danger")
                return redirect(url_for("landing_page"))

            if not customer_record:
                flash("Unable to register or find customer record.", "danger")
                return redirect(url_for("landing_page"))
    
        existing_ticket = (QueueTicket.query
                           .filter(QueueTicket.customer_ref_id == customer_record.id,
                                   QueueTicket.ticket_status.in_(["waiting", "serving"]))
                           .first())

        ticket_to_lock = None
        if existing_ticket:
            # 1. Build the recovery URL
            recover_url = url_for('recover_ticket')
            
      
            msg = Markup(f"❌ You already have an active ticket (#{existing_ticket.id}). "
                         f"<a href='{recover_url}' style='color: inherit; text-decoration: underline; font-weight: bold;'>Click here to recover it.</a>")
            
            flash(msg, "warning")
            return redirect(url_for("landing_page"))
        
        else:
            if payment_method == "online" and PAYMONGO_ENABLED and amount_due > 0:
                batch_id = str(uuid.uuid4())
                checkout = create_checkout_session(
                    line_items=[{"name": visit_purpose[:60],
                                 "amount": amount_due * 100,
                                 "quantity": 1}],
                    batch_id=batch_id,
                    description=f"QFlow ticket fee — {visit_purpose[:60]}",
                    success_url=url_for("payment_return", batch_id=batch_id,
                                        _external=True, kind="ticket"),
                    cancel_url=url_for("landing_page", _external=True),
                )
                db.session.add(AdvancePayment(
                    customer_id=customer_record.id,
                    document_name=visit_purpose[:200],
                    price=amount_due,
                    payment_status="pending",
                    payment_batch_id=batch_id,
                    paymongo_checkout_id=checkout["checkout_id"],
                    paymongo_payment_intent=checkout["payment_intent_id"],
                    flow_kind="ticket",
                ))
                db.session.commit()
                session["pending_ticket_reason"] = visit_purpose

                # Popup flow needs the checkout URL as JSON instead of a redirect
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return {"checkout_url": checkout["checkout_url"]}, 200
                return redirect(checkout["checkout_url"])

            new_ticket = QueueTicket(
                visit_reason=visit_purpose,
                customer_ref_id=customer_record.id,
                payment_method=payment_method,
                payment_status=payment_status,
                amount_due=amount_due
            )
            db.session.add(new_ticket)

            try:
                # 1. Commit first to generate the Ticket ID (e.g., 8)
                db.session.commit()
                
                # --- NEW: Auto-Generate Guest ID ---
                # Format: #8barreto
                if customer_type == 'guest' and family_name:
                    # Create ID: '#' + TicketID + LastName (lowercase, no spaces)
                    clean_lastname = family_name.lower().replace(" ", "")
                    generated_id = f"#{new_ticket.id}{clean_lastname}"
                    
                    # Update the customer record
                    customer_record.student_number = generated_id
                    db.session.commit()
                    
                    # Show special message with the ID
                    session["ticket_message"] = (f"✅ Ticket #{new_ticket.id} issued! "
                                                 f"Your Guest Recovery ID is: {generated_id}")
                else:
                    # Standard message for Students
                    session["ticket_message"] = f"✅ Ticket #{new_ticket.id} has been issued successfully!"

            except IntegrityError:
                db.session.rollback()
                flash("Database error while issuing your ticket. Please try again.", "danger")
                return redirect(url_for("landing_page"))

            ticket_to_lock = new_ticket
            push_queue_update(socketio)

        # ---------- session + browser lock ----------
        session["user_id"] = customer_record.id
        resp = make_response(redirect(url_for("customer_portal")))
        _set_lock(resp, ticket_to_lock.id)
        return resp
    
    except Exception as error:
        db.session.rollback()
        print(f"❌  Ticket request failed: {error}")
        import traceback
        traceback.print_exc()
        session["ticket_message"] = "System error occurred. Please try again."
        return redirect(url_for("landing_page"))
    

@app.route("/recover_ticket", methods=["GET", "POST"])
def recover_ticket():
    """
    Allows a user to re-login to their active ticket using their details.
    Updates the browser lock so they stay logged in.
    """
    if request.method == "POST":
        student_id  = (request.form.get("student_id") or "").strip()
        given_name  = (request.form.get("first_name") or "").strip()
        family_name = (request.form.get("last_name")  or "").strip()

        # Base query for active tickets (waiting or serving)
        query = (QueueTicket.query
                 .join(QueueCustomer)
                 .filter(QueueTicket.ticket_status.in_(['waiting', 'serving']))
                 .order_by(QueueTicket.issue_time.desc()))

        # Filter Logic:
        # If Student ID is provided, match ID + Names
        if student_id:
             latest_ticket = query.filter(
                 QueueCustomer.student_number == student_id,
                 QueueCustomer.firstname.ilike(given_name),
                 QueueCustomer.lastname.ilike(family_name)
             ).first()
        # If NO Student ID (Guest), match Names only
        else:
             latest_ticket = query.filter(
                 QueueCustomer.student_number.is_(None), # Ensure it's a guest/no-id account
                 QueueCustomer.firstname.ilike(given_name),
                 QueueCustomer.lastname.ilike(family_name)
             ).first()

        if latest_ticket:
            # 1. Restore Session
            session["user_id"] = latest_ticket.customer_ref_id
            session["ticket_message"] = f"✅ Ticket #{latest_ticket.id} recovered successfully!"
            
            # 2. Create Response & Restore Browser Lock (Critical Step)
            resp = make_response(redirect(url_for("customer_portal")))
            _set_lock(resp, latest_ticket.id)
            
            return resp

        flash("No active ticket found with those details.", "danger")
        return redirect(url_for("recover_ticket"))

    return render_template("recover_ticket.html")
# ========================================
# Customer-Facing Routes
# ========================================

@app.route("/student/dashboard", methods=["GET", "POST"])
def customer_portal():
    """Customer dashboard displaying ticket status"""
    if not session.get("user_id"):
        tid = _get_lock()
        if tid:
            tk = QueueTicket.query.filter(
                QueueTicket.id == tid,
                QueueTicket.ticket_status.in_(["waiting", "serving"])
            ).first()
            if tk:
                session["user_id"] = tk.customer_ref_id
                return redirect(url_for("customer_portal"))

    notification = session.pop("ticket_message", "")
    active_ticket_id = None

    if request.method == "POST":
        return redirect(url_for("process_ticket_request"))

    customer_id = session.get("user_id")
    if customer_id:
        pending_ticket = QueueTicket.query.filter(
            QueueTicket.customer_ref_id == customer_id,
            QueueTicket.ticket_status.in_(['waiting', 'serving']) 
        ).first()
        if pending_ticket:
            active_ticket_id = pending_ticket.id

    if active_ticket_id is None:
        tid = _get_lock()
        if tid:
            tk = QueueTicket.query.filter(
                QueueTicket.id == tid,
                QueueTicket.ticket_status.in_(["waiting", "serving"])
            ).first()
            if tk:
                active_ticket_id = tk.id

    current_queue = fetch_waiting_queue()
    
    active_service = fetch_active_service()
    if active_service:
        current_queue.insert(0, active_service)

    resume_time = retrieve_config_value('resume_time_display', '')

    return render_template("customers.html", 
                           message=notification, 
                           ticket_id=active_ticket_id, 
                           queue=current_queue,
                           resume_time=resume_time) 

# ========================================
# Admin-Facing Routes
# ========================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Administrator authentication endpoint (manual validation)"""
    if request.method == "POST":
        entered_username = (request.form.get("username") or "").strip()
        entered_password = (request.form.get("password") or "").strip()

        if not entered_username or not entered_password:
            flash("Please enter username and password.", "warning")
            return redirect(url_for("admin_login"))

        # Throttle by IP + username so a script can't brute-force the
        # password with unlimited attempts.
        lock_key = f"{request.remote_addr}:{entered_username.lower()}"
        if is_login_locked(lock_key):
            flash(f"Too many failed attempts. Please wait a few minutes and try again.", "danger")
            return redirect(url_for("admin_login"))

        admin_account = AdminUser.query.filter_by(username=entered_username, role='admin').first()
        if admin_account and admin_account.verify_password(entered_password):
            clear_failed_logins(lock_key)
            session["username"] = admin_account.username
            session["role"] = admin_account.role
            flash("Authentication successful. Welcome to admin panel.", "success")
            return redirect(url_for("admin_control_panel"))

        record_failed_login(lock_key)
        flash("Authentication failed. Please verify your credentials.", "danger")
        return redirect(url_for("admin_login"))

    return render_template("admin_login.html")


@app.route("/admin/dashboard", methods=["GET", "POST"])
@require_admin_access
def admin_control_panel():
    """Main administrative control panel"""
    if request.method == "POST":
        updated_limit = (request.form.get("daily_limit") or "").strip()
        if updated_limit.isdigit() and 1 <= int(updated_limit) <= 1000:
            try:
                update_config_value('daily_ticket_limit', int(updated_limit))
                flash("Daily ticket limit has been updated.", "success")
            except Exception as e:
                db.session.rollback()
                flash("Failed to update daily limit.", "danger")

    flow_status = retrieve_config_value('queue_flow_status', 'active')
    
    resume_time_display = retrieve_config_value('resume_time_display', '')

    tickets_today = count_todays_tickets()
    max_daily_tickets = int(retrieve_config_value('daily_ticket_limit', 300))

    pending_queue = fetch_waiting_queue()
    queue_length = len(pending_queue)

    currently_serving = fetch_active_service()
    next_in_line = pending_queue[0] if pending_queue else None
    queue_active = bool(currently_serving or pending_queue)

    return render_template(
        "admin.html",
        queue=pending_queue,
        total_in_queue=queue_length,
        today_count=tickets_today,
        daily_limit=max_daily_tickets,
        current_ticket=currently_serving,
        next_ticket=next_in_line,
        has_queue=queue_active,
        flow_status=flow_status,
        resume_time=resume_time_display # Pass to template
    )

@app.route("/admin/serve_next", methods=["POST"])
@require_admin_access
def advance_to_next_customer():
    try:
        # Check Flow Status
        flow_status = retrieve_config_value('queue_flow_status', 'active')
        
        current_service = QueueTicket.query.filter_by(ticket_status='serving').order_by(QueueTicket.issue_time).first()
        next_waiting = QueueTicket.query.filter_by(ticket_status='waiting').order_by(QueueTicket.issue_time).first()

        # --- SCENARIO 1: PAUSED MODE (Break Mode) ---
        # Just finish the current person. Do NOT call the next.
        if flow_status == 'paused':
            if current_service:
                current_service.ticket_status = 'served'
                current_service.completion_time = datetime.utcnow()
                db.session.commit()

                # Notify the person leaving
                socketio.emit("force_logout", {"message": "✅ Your service is complete. Thank you!"}, room=str(current_service.customer_ref_id))
                socketio.emit("announcement", {"message": f"Served: #{current_service.id} — Service Paused."})
                
                flash(f"✅ Finished #{current_service.id}. Queue is PAUSED.", "warning")
            else:
                flash("Queue is paused and nobody is currently serving.", "info")
            
            resp = make_response(redirect(url_for("admin_control_panel")))
            _clear_lock(resp)
            push_queue_update(socketio)
            return resp

        # --- SCENARIO 2: ACTIVE MODE (Normal Flow) ---
        # (This is your existing logic)
        if current_service and next_waiting:
            # Finish Current
            current_service.ticket_status = 'served'
            current_service.completion_time = datetime.utcnow()
            # Start Next
            next_waiting.ticket_status = 'serving'
            db.session.commit()
            
            # Notifications...
            socketio.emit("force_logout", {"message": "✅ Service complete."}, room=str(current_service.customer_ref_id))
            socketio.emit("ticket_status", {"message": f"🎯 Ticket #{next_waiting.id} — It's your turn!"}, room=str(next_waiting.customer_ref_id))
            socketio.emit("announcement", {"message": f"Now serving: #{next_waiting.id} — {next_waiting.customer_ref.fullname}"})
            
            flash(f"✅ Served #{current_service.id} → Now serving #{next_waiting.id}.", "success")

        elif current_service and not next_waiting:
            # Finish current, no one else waiting
            current_service.ticket_status = 'served'
            current_service.completion_time = datetime.utcnow()
            db.session.commit()
            socketio.emit("force_logout", {"message": "✅ Service complete."}, room=str(current_service.customer_ref_id))
            flash(f"✅ Ticket #{current_service.id} served. Queue empty.", "info")

        elif not current_service and next_waiting:
            # Nobody was serving, start the first person
            next_waiting.ticket_status = 'serving'
            db.session.commit()
            socketio.emit("ticket_status", {"message": f"🎯 Ticket #{next_waiting.id} — It's your turn!"}, room=str(next_waiting.customer_ref_id))
            socketio.emit("announcement", {"message": f"Now serving: #{next_waiting.id}"})
            flash(f"🎟️ Now serving: Ticket #{next_waiting.id}", "success")

        resp = make_response(redirect(url_for("admin_control_panel")))
        _clear_lock(resp)
        push_queue_update(socketio)
        return resp

    except Exception as error:
        db.session.rollback()
        print(f"❌ Error: {error}")
        flash("Error occurred while advancing queue.", "danger")
        return redirect(url_for("admin_control_panel"))
    
@app.route("/admin/no_show_next", methods=["POST"])
@require_admin_access
def mark_customer_absent():
    """Mark ONLY the CURRENT serving customer as no-show"""
    try:
        # STRICT: Only look for the person currently being served
        target_ticket = QueueTicket.query.filter_by(ticket_status='serving').first()

        if not target_ticket:
            # If nobody is being served, stop immediately.
            flash("⚠️ You must START SERVING a ticket before marking them as No-Show.", "warning")
            return redirect(url_for("admin_control_panel"))

        # Proceed to mark as no-show
        ticket_id = target_ticket.id
        target_ticket.ticket_status = 'no_show'
        target_ticket.completion_time = datetime.utcnow()
        db.session.commit()

        # Notify customer
        socketio.emit(
            "force_logout",
            {"message": "⚠️ You have been marked absent. Please contact office if needed."},
            room=str(target_ticket.customer_ref_id)
        )
        
        # Notify Admin & Screen
        flash(f"⚠️ Ticket #{ticket_id} marked as No-Show.", "warning")
        socketio.emit("announcement", {"message": f"Ticket #{ticket_id} marked as No-Show."})
        
        push_queue_update(socketio)

        resp = make_response(redirect(url_for("admin_control_panel")))
        _clear_lock(resp)
        return resp

    except Exception as error:
        db.session.rollback()
        print(f"❌ No-show marking failed: {error}")
        flash("Error occurred while marking no-show.", "danger")
        return redirect(url_for("admin_control_panel"))
    
@app.route("/admin/recall", methods=["POST"])
@require_admin_access
def recall_current_customer():
    """Re-broadcasts the call for the current customer with a 3x limit."""
    try:
       
        current_service = QueueTicket.query.filter_by(ticket_status='serving').first()

        if current_service:
            if current_service.recall_count >= 3:
                flash(f"⚠️ Max recall limit reached (3/3) for Ticket #{current_service.id}.", "warning")
                return redirect(url_for("admin_control_panel"))

          
            current_service.recall_count += 1
            db.session.commit()

            socketio.emit(
                "ticket_status",
                {"message": f"📢 RECALL ({current_service.recall_count}/3): Ticket #{current_service.id}, please proceed to the counter!"},
                room=str(current_service.customer_ref_id)
            )

            socketio.emit(
                "announcement",
                {"message": f"📢 Calling again: Ticket #{current_service.id} — {current_service.customer_ref.fullname}"}
            )
            
            
            push_queue_update(socketio)

            flash(f"📢 Recalled Ticket #{current_service.id} ({current_service.recall_count}/3).", "info")
        else:
            flash("No active ticket to recall.", "warning")

        return redirect(url_for("admin_control_panel"))

    except Exception as e:
        db.session.rollback()
        print(f"Recall error: {e}")
        flash("Error sending recall notification.", "danger")
        return redirect(url_for("admin_control_panel"))
    
@app.route("/admin/toggle_flow", methods=["POST"])
@require_admin_access
def toggle_queue_flow():
    try:
        current_status = retrieve_config_value('queue_flow_status', 'active')
        new_status = 'paused' if current_status == 'active' else 'active'
        update_config_value('queue_flow_status', new_status)
    
        socketio.emit('flow_status_update', {'status': new_status})
        
        return {"status": "success", "new_state": new_status}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
    
    
@app.route("/admin/announce_resume", methods=["POST"])
@require_admin_access
def announce_resume():
    try:
        
        action = request.form.get("action", "post")
        resume_time = request.form.get("resume_time", "").strip()
        
        if action == "clear" or not resume_time:
            update_config_value('resume_time_display', '')
            socketio.emit('break_announcement', {'active': False})
            flash("Announcement banner cleared.", "info")
        else:
            update_config_value('resume_time_display', resume_time)
            socketio.emit('break_announcement', {'active': True, 'time': resume_time})
            flash(f"📢 Banner Posted: Resuming at {resume_time}", "success")

        return redirect(url_for("admin_control_panel"))

    except Exception as e:
        print(f"Announcement error: {e}")
        flash("Error setting announcement.", "danger")
        return redirect(url_for("admin_control_panel"))
    
@app.route("/ack_ticket", methods=["POST"])
def ack_ticket_notification():
    """Records a customer acknowledging their call/recall notification and notifies admin."""
    customer_id = session.get("user_id")
    ticket_id = request.form.get("ticket_id", type=int)
    info = request.form.get("info") # 'INITIAL_CALL_ACK' or 'RECALL_ACK'

    if customer_id and ticket_id:
        try:
            # 1. Save data to database
            new_ack = TicketAcknowledgement(
                ticket_id=ticket_id,
                customer_id=customer_id,
                info=info
            )
            db.session.add(new_ack)
            db.session.commit()
            
           
            ticket = QueueTicket.query.get(ticket_id)
            student_name = ticket.customer_ref.fullname if ticket else "Unknown Student"
            
            socketio.emit('ack_log', {
                'ticket': ticket_id, 
                'name': student_name,
                'info': info,
                'time': datetime.utcnow().strftime('%H:%M:%S')
            })
            
            return {"status": "success"}, 200
        except Exception as e:
            db.session.rollback()
            print(f"Error recording ACK: {e}")
            return {"status": "error", "message": str(e)}, 500
    
    return {"status": "error", "message": "Missing ID"}, 400

@app.route("/admin/history")
@require_admin_access
def view_ticket_history():
    """Display historical ticket records"""
    completed_tickets = QueueTicket.query.filter_by(ticket_status='served').order_by(QueueTicket.completion_time.desc()).all()
    absent_tickets = QueueTicket.query.filter_by(ticket_status='no_show').order_by(QueueTicket.completion_time.desc()).all()

    completed_list = [ticket.serialize() for ticket in completed_tickets]
    absent_list = [ticket.serialize() for ticket in absent_tickets]

    return render_template("history.html", served=completed_list, no_show=absent_list)


@app.route("/admin/logout")
def admin_logout():
    """Terminate administrative session"""
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for("admin_login"))

@app.route("/account/login", methods=["GET", "POST"])
def account_login():
    if request.method == "POST":
        student_number = (request.form.get("student_id") or "").strip()
        password = request.form.get("password") or ""

        customer = QueueCustomer.query.filter_by(student_number=student_number).first()
        if not customer or not customer.verify_password(password):
            flash("Invalid student number or password.", "danger")
            return redirect(url_for("account_login"))

        session["user_id"] = customer.id
        session["authenticated"] = True
        flash(f"Welcome back, {customer.get_display_name()}!", "success")
        return redirect(url_for("landing_page"))

    return render_template("account_login.html")


@app.route("/account/signup", methods=["GET", "POST"])
def account_signup():
    if request.method == "POST":
        student_number = (request.form.get("student_id") or "").strip()
        given_name = (request.form.get("first_name") or "").strip()
        family_name = (request.form.get("last_name") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not student_number or not password:
            flash("Student number and password are required.", "warning")
            return redirect(url_for("account_signup"))

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("account_signup"))

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "warning")
            return redirect(url_for("account_signup"))

        existing = QueueCustomer.query.filter_by(student_number=student_number).first()
        if existing and existing.password_hash:
            flash("An account already exists for that student number. Please log in.", "warning")
            return redirect(url_for("account_login"))

        # A record can already exist with no password (e.g. front-desk
        # created it when this student took a ticket). Anyone who knows
        # the student number could otherwise attach any name/password to
        # it and take over that identity. Require the name to match what
        # is already on file before letting a signup claim it.
        if existing and (existing.firstname or existing.lastname):
            on_file_first = (existing.firstname or "").strip().lower()
            on_file_last = (existing.lastname or "").strip().lower()
            if on_file_first != given_name.strip().lower() or on_file_last != family_name.strip().lower():
                flash("That student number already has records under a different name. "
                      "Please visit the office to verify your identity.", "danger")
                return redirect(url_for("account_signup"))

        customer = existing or QueueCustomer(
            customer_type="student",
            student_number=student_number,
        )
        customer.firstname = given_name or customer.firstname
        customer.lastname = family_name or customer.lastname
        customer.fullname = f"{customer.firstname} {customer.lastname}".strip() or customer.fullname
        customer.set_password(password)

        if not existing:
            db.session.add(customer)
        db.session.commit()

        session["user_id"] = customer.id
        session["authenticated"] = True
        flash("Account created successfully.", "success")
        return redirect(url_for("landing_page"))

    return render_template("account_signup.html")

@app.route("/account/logout")
def account_logout():
    session.pop("authenticated", None)
    session.pop("user_id", None)
    resp = make_response(redirect(url_for("landing_page")))
    _clear_lock(resp)
    return resp

@app.route("/account/dashboard")
def account_dashboard():
    if not session.get("authenticated"):
        flash("Please log in.", "warning")
        return redirect(url_for("account_login"))

    unused_rows = AdvancePayment.query.filter_by(
        customer_id=session["user_id"], is_used=False
    ).order_by(AdvancePayment.created_at.desc()).all()

    batches = {}
    for row in unused_rows:
        if row.payment_batch_id not in batches:
            batches[row.payment_batch_id] = {
                "batch_id": row.payment_batch_id,
                "documents": [],
                "created_at": row.created_at,
            }
        batches[row.payment_batch_id]["documents"].append(row.document_name)

    payment_batches = list(batches.values())

    return render_template("account_dashboard.html", payment_batches=payment_batches)

@app.route("/account/pay-advance", methods=["GET", "POST"])
def pay_advance():
    if not session.get("authenticated"):
        flash("Please log in to pay in advance.", "warning")
        return redirect(url_for("account_login"))

    if request.method == "POST":
        selected_documents = request.form.getlist("document_names")
        if not selected_documents:
            flash("Please select at least one service.", "warning")
            return redirect(url_for("pay_advance"))

        # Server-side price lookup — client totals are never trusted
        priced_documents = []
        for doc_name in selected_documents:
            price = price_for_document(doc_name)
            if price is None:
                flash(f"'{doc_name}' is not a recognized document.", "danger")
                return redirect(url_for("pay_advance"))
            priced_documents.append((doc_name, price))

        batch_id = str(uuid.uuid4())
        line_items = [{"name": DOCUMENT_LABELS.get(n, n),
                       "amount": p * 100 + SERVICE_FEE_CENTAVOS, 
                       "quantity": 1} for n, p in priced_documents]

        checkout = None
        if PAYMONGO_ENABLED:
            try:
                checkout = create_checkout_session(
                    line_items=line_items,
                    batch_id=batch_id,
                    description="QFlow advance payment",
                    success_url=url_for("payment_return", batch_id=batch_id,
                                        _external=True, kind="advance"),
                    cancel_url=url_for("pay_advance", _external=True),
                )
            except RuntimeError as e:
                print(f"❌ PayMongo checkout failed: {e}")
                flash("Payment gateway is unavailable. Please try again.", "danger")
                return redirect(url_for("pay_advance"))

        # Create records as PENDING — flipped to "paid" only by the webhook
        for doc_name, price in priced_documents:
            db.session.add(AdvancePayment(
                customer_id=session["user_id"],
                document_name=doc_name,
                price=price,
                payment_status="pending" if PAYMONGO_ENABLED else "paid",
                payment_batch_id=batch_id,
                paymongo_checkout_id=checkout["checkout_id"] if checkout else None,
                paymongo_payment_intent=checkout["payment_intent_id"] if checkout else None,
                flow_kind="advance",
            ))
        db.session.commit()

        if checkout:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return {"checkout_url": checkout["checkout_url"]}, 200
            return redirect(checkout["checkout_url"])   # → PayMongo hosted page

        flash("Payment recorded (gateway not configured).", "success")
        return redirect(url_for("account_dashboard"))

    return render_template("pay_advance.html", prices=DOCUMENT_PRICES, labels=DOCUMENT_LABELS)


@app.route("/account/activate-batch/<batch_id>", methods=["POST"])
def activate_payment(batch_id):
    if not session.get("authenticated"):
        flash("Please log in.", "warning")
        return redirect(url_for("account_login"))

    existing_ticket = (QueueTicket.query
                       .filter(QueueTicket.customer_ref_id == session["user_id"],
                               QueueTicket.ticket_status.in_(["waiting", "serving"]))
                       .first())
    if existing_ticket:
        flash(f"You already have an active ticket (#{existing_ticket.id}). Please wait for it to be served before activating another.", "warning")
        return redirect(url_for("customer_portal"))

    batch_rows = AdvancePayment.query.filter_by(
        payment_batch_id=batch_id,
        customer_id=session["user_id"],
        is_used=False,
    ).all()

    if not batch_rows:
        flash("Payment batch not found or already used.", "danger")
        return redirect(url_for("account_dashboard"))

    # Now that pay_advance can leave a batch "pending" until PayMongo's
    # webhook confirms it, activation must check that too - otherwise a
    # student can hit this route directly for an unpaid batch and get
    # the service for free.
    if any(row.payment_status != "paid" for row in batch_rows):
        flash("Payment not confirmed yet. Please complete checkout first.", "warning")
        return redirect(url_for("account_dashboard"))

    document_names = [row.document_name for row in batch_rows]
    combined_reason = ", ".join(document_names)

    for row in batch_rows:
        row.is_used = True
        row.used_at = datetime.utcnow()
    db.session.commit()

    customer = QueueCustomer.query.get(session["user_id"])
    new_ticket = QueueTicket(
        visit_reason=combined_reason,
        customer_ref_id=customer.id,
        payment_method="advance",
        payment_status="paid"
    )
    db.session.add(new_ticket)
    db.session.commit()

    session["ticket_message"] = f"✅ Ticket #{new_ticket.id} issued for: {combined_reason}"
    session["user_id"] = customer.id
    resp = make_response(redirect(url_for("customer_portal")))
    _set_lock(resp, new_ticket.id)
    push_queue_update(socketio)
    return resp

@app.route("/account/edit-name", methods=["GET", "POST"])
def edit_name():
    if not session.get("authenticated"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(success=False, message="Not logged in."), 401
        flash("Please log in.", "warning")
        return redirect(url_for("account_login"))

    customer = QueueCustomer.query.get(session["user_id"])
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if request.method == "POST":
        given_name = (request.form.get("first_name") or "").strip()
        family_name = (request.form.get("last_name") or "").strip()

        if not given_name or not family_name:
            if is_ajax:
                return jsonify(success=False, message="Both first and last name are required.")
            flash("Both first and last name are required.", "warning")
            return redirect(url_for("edit_name"))

        customer.firstname = given_name
        customer.lastname = family_name
        customer.fullname = f"{given_name} {family_name}"
        db.session.commit()

        if is_ajax:
            return jsonify(success=True, full_name=customer.get_display_name())

        flash("Name updated successfully.", "success")
        return redirect(url_for("landing_page"))

    return redirect(url_for("landing_page"))

@app.route("/paymongo/webhook", methods=["POST"])
def paymongo_webhook():
    raw = request.get_data()
    if not verify_webhook(raw, request.headers.get("Paymongo-Signature", "")):
        return {"error": "invalid signature"}, 400

    event = request.json.get("data", {})
    event_type = event.get("attributes", {}).get("type", "")

    if event_type == "checkout_session.payment.paid":
        sess = event["attributes"]["data"]
        ref = sess["attributes"].get("reference_number") \
              or sess["attributes"].get("metadata", {}).get("batch_id")
        if ref:
            rows = AdvancePayment.query.filter_by(payment_batch_id=ref,
                                                  payment_status="pending").all()
            for row in rows:
                row.payment_status = "paid"
            if rows:
                db.session.commit()
                print(f"✅ PayMongo paid: batch {ref} ({len(rows)} row(s))")
    return {"received": True}, 200


@app.route("/payment/return")
def payment_return():
    batch_id = request.args.get("batch_id", "")
    kind = request.args.get("kind", "advance")
    row = AdvancePayment.query.filter_by(payment_batch_id=batch_id).first()
    if not row:
        flash("Payment record not found.", "danger")
        return redirect(url_for("landing_page"))

    if row.payment_status == "paid":
        return _finish_paid_flow(row, kind)
    if row.payment_status in ("failed", "expired"):
        flash("Payment was not completed. You have not been charged.", "warning")
        return redirect(url_for("pay_advance") if kind == "advance"
                        else url_for("landing_page"))

    # Still pending → webhook hasn't arrived yet → show progress page (polls below)
    return render_template("payment_processing.html",
                           batch_id=batch_id, kind=kind,
                           amount=row.price,
                           label=DOCUMENT_LABELS.get(row.document_name, row.document_name))


@app.route("/payment/status/<batch_id>")
def payment_status(batch_id):
    row = AdvancePayment.query.filter_by(payment_batch_id=batch_id).first()
    if not row:
        return {"status": "unknown"}, 404
    return {"status": row.payment_status}


def _finish_paid_flow(row, kind):
    """Called once payment is confirmed. Issues the ticket for 'ticket' flow."""
    if kind == "ticket":
        existing = QueueTicket.query.filter(
            QueueTicket.customer_ref_id == row.customer_id,
            QueueTicket.ticket_status.in_(["waiting", "serving"])).first()
        if not existing:
            new_ticket = QueueTicket(
                visit_reason=row.document_name,
                customer_ref_id=row.customer_id,
                payment_method="online",
                payment_status="paid",
                amount_due=row.price,
            )
            db.session.add(new_ticket)
            db.session.commit()
            session["user_id"] = row.customer_id
            session["ticket_message"] = f"✅ Payment confirmed! Ticket #{new_ticket.id} issued."
            resp = make_response(redirect(url_for("customer_portal")))
            _set_lock(resp, new_ticket.id)
            push_queue_update(socketio)
            return resp
        session["ticket_message"] = f"⚠️ You already have an active ticket (#{existing.id})."
        return redirect(url_for("customer_portal"))

    # advance flow → back to dashboard with the paid batch ready to activate
    flash("Payment confirmed! Your service is ready to activate.", "success")
    return redirect(url_for("account_dashboard"))

# ========================================
# Application Entry Point
# ========================================

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    import getpass
    import hashlib

    
    try:
        import socket
        lan_ip = socket.gethostbyname(socket.gethostname())
        print(f"Local Development: http://{lan_ip}:5001")
    except:
        pass
        
    print(" Server Starting...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5001)