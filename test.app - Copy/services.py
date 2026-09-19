"""
Business-logic layer for the queue system.
"""
from datetime import date, datetime
from functools import wraps
from typing import Optional, List, Dict, Any

from sqlalchemy.exc import IntegrityError
from flask import session, flash, redirect, url_for
from flask_socketio import SocketIO

from models import db, QueueTicket, QueueCustomer, SystemConfig, AdminUser


# --------------------------------------------------
# Queue helpers
# --------------------------------------------------
# In services.py

def fetch_waiting_queue() -> List[Dict[str, Any]]:
    pending = (QueueTicket.query
               .filter(QueueTicket.ticket_status == 'waiting')  # <--- CHANGE THIS
               .order_by(QueueTicket.issue_time)
               .all())
    
    return [t.serialize() for t in pending]


def fetch_active_service() -> Optional[Dict[str, Any]]:
    """Return serialised ticket currently being served."""
    active = (QueueTicket.query
              .filter_by(ticket_status='serving')
              .order_by(QueueTicket.issue_time)
              .first())
    return active.serialize() if active else None


def count_todays_tickets() -> int:
    """How many tickets were issued today (UTC date)."""
    today = date.today()
    return (QueueTicket.query
            .filter(db.func.DATE(QueueTicket.issue_time) == today)
            .count())


def check_ticket_limit() -> bool:
    """True if daily quota exceeded."""
    max_tickets = int(retrieve_config_value('daily_ticket_limit', 300))
    return count_todays_tickets() >= max_tickets


def verify_duplicate_ticket(fname: str, lname: str) -> bool:
    """True if customer fname+lname already has a waiting/serving ticket."""
    dup = (QueueTicket.query
           .join(QueueCustomer)
           .filter(QueueCustomer.firstname.ilike(fname.strip()),
                   QueueCustomer.lastname.ilike(lname.strip()),
                   QueueTicket.ticket_status.in_(['waiting', 'serving']))
           .first())
    return dup is not None


# --------------------------------------------------
# Customer CRUD
# --------------------------------------------------
def retrieve_or_create_customer(cust_type: str,
                                student_num: Optional[str] = None,
                                fname: Optional[str] = None,
                                lname: Optional[str] = None) -> QueueCustomer:
    """
    Find or create a QueueCustomer.
    Caller must commit the session after creating the ticket.
    """
    student_num = (student_num or "").strip() or None
    fname = (fname or "").strip() or None
    lname = (lname or "").strip() or None

    display = f"{fname} {lname}" if fname and lname else (fname or lname or
                                                          ("Student" if cust_type == "student" else "Guest"))
    # Search existing
    existing = None
    if cust_type == "student" and student_num:
        existing = QueueCustomer.query.filter_by(customer_type="student",
                                                 student_number=student_num).first()
    elif cust_type == "guest" and fname and lname:
        existing = (QueueCustomer.query
                    .filter(QueueCustomer.customer_type == "guest",
                            QueueCustomer.firstname.ilike(fname),
                            QueueCustomer.lastname.ilike(lname))
                    .first())

    if existing:
        existing.fullname = display
        if fname:
            existing.firstname = fname
        if lname:
            existing.lastname = lname
        return existing

    # Create new
    try:
        new_cust = QueueCustomer(customer_type=cust_type,
                                 student_number=student_num,
                                 firstname=fname,
                                 lastname=lname,
                                 fullname=display)
        db.session.add(new_cust)
        db.session.flush()          # may raise IntegrityError
        return new_cust
    except IntegrityError:
        db.session.rollback()
        if student_num:
            existing = QueueCustomer.query.filter_by(student_number=student_num).first()
            if existing:
                existing.fullname = display
                if fname:
                    existing.firstname = fname
                if lname:
                    existing.lastname = lname
                db.session.commit()
                return existing
        raise


# --------------------------------------------------
# Config helpers
# --------------------------------------------------
def retrieve_config_value(key: str, fallback: Optional[str] = None) -> Optional[str]:
    row = SystemConfig.query.filter_by(config_key=key).first()
    return row.config_value if row else fallback


def update_config_value(key: str, value: Any) -> None:
    row = SystemConfig.query.filter_by(config_key=key).first()
    if row:
        row.config_value = str(value)
    else:
        row = SystemConfig(config_key=key, config_value=str(value))
        db.session.add(row)
    db.session.commit()


# --------------------------------------------------
# Real-time broadcast
# --------------------------------------------------
# In services.py

def push_queue_update(socketio: SocketIO) -> None:
    """Emit queue state to all connected clients."""
    current_queue = fetch_waiting_queue()

    now_serving_obj = (QueueTicket.query
                       .filter_by(ticket_status='serving')
                       .order_by(QueueTicket.issue_time)
                       .first())
    
    next_waiting_obj = (QueueTicket.query
                        .filter_by(ticket_status='waiting')
                        .order_by(QueueTicket.issue_time)
                        .first())

    # --- FIX START: Serialize active ticket and insert it into the queue list ---
    active_serialized = now_serving_obj.serialize() if now_serving_obj else None
    
    if active_serialized:
        # Determine if the serving ticket should be visually part of the queue list
        # This matches the logic in apps12.py customer_portal route
        current_queue.insert(0, active_serialized)
    # --- FIX END ---

    payload = {
        "queue": current_queue,
        "has_queue": bool(now_serving_obj or current_queue),
        "current_ticket": active_serialized,
        "next_ticket": next_waiting_obj.serialize() if next_waiting_obj else None,
    }
    socketio.emit("queue_update", payload)
# --------------------------------------------------
# Auth decorator
# --------------------------------------------------
def require_admin_access(func):
    """Enforce admin role on Flask routes."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Administrative privileges required to access this resource.", "danger")
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapped