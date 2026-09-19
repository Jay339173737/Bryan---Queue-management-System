from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()
password_hash = db.Column(db.String(255), nullable=True)

class AdminUser(db.Model):
    """Represents administrative users in the system"""
    __tablename__ = 'admin_users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False, index=True)
    password = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), default='admin')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<AdminUser {self.username}>'
    
    def verify_password(self, pwd):
        """Check if provided password matches"""
        return self.password == pwd


class QueueCustomer(db.Model):
    """Represents customers (students/guests) in the queue system"""
    __tablename__ = 'queue_customers'
    
    id = db.Column(db.Integer, primary_key=True)
    customer_type = db.Column(db.String(20), nullable=False) 
    student_number = db.Column(db.String(50), unique=True, nullable=True, index=True)
    firstname = db.Column(db.String(100), nullable=True)
    lastname = db.Column(db.String(100), nullable=True)
    fullname = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)
    customer_tickets = db.relationship('QueueTicket', backref='customer_ref', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<QueueCustomer {self.fullname}>'
    
    def get_display_name(self):
        """Return formatted customer name"""
        if self.firstname and self.lastname:
            return f"{self.firstname} {self.lastname}"
        return self.fullname

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def verify_password(self, raw_password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)


class QueueTicket(db.Model):
    """Represents individual queue tickets"""
    __tablename__ = 'queue_tickets'
    
    id = db.Column(db.Integer, primary_key=True)
    visit_reason = db.Column(db.Text, nullable=True)
    ticket_status = db.Column(db.String(20), default='waiting', index=True)  # waiting, serving, served, no_show
    customer_ref_id = db.Column(db.Integer, db.ForeignKey('queue_customers.id'), nullable=False)
    issue_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completion_time = db.Column(db.DateTime, nullable=True)

    
    # --- NEW: Track how many times the ticket was recalled ---
    recall_count = db.Column(db.Integer, default=0)
    payment_method = db.Column(db.String(20), default='cash')
    payment_status = db.Column(db.String(20), default='unpaid')
    
    def __repr__(self):
        return f'<QueueTicket #{self.id} [{self.ticket_status}]>'
    
    def serialize(self):
        """Convert ticket object to dictionary"""
        return {
            'ticket': self.id,
            'reason': self.visit_reason,
            'status': self.ticket_status,
            'created_at': self.issue_time.strftime('%Y-%m-%d %H:%M:%S') if self.issue_time else None,
            'served_at': self.completion_time.strftime('%Y-%m-%d %H:%M:%S') if self.completion_time else None,
            'name': self.customer_ref.fullname,
            'role': self.customer_ref.customer_type,
            'student_id': self.customer_ref.student_number,
            'customer_id': self.customer_ref.id,
            'recall_count': self.recall_count
        }
class SystemConfig(db.Model):
    """Stores system-wide configuration settings"""
    __tablename__ = 'system_config'
    
    id = db.Column(db.Integer, primary_key=True)
    config_key = db.Column(db.String(50), unique=True, nullable=False, index=True)
    config_value = db.Column(db.String(200))
    
    def __repr__(self):
        return f'<SystemConfig {self.config_key}={self.config_value}>'

class TicketAcknowledgement(db.Model):
    """Log when a student acknowledges a ticket notification (clicks OK)"""
    __tablename__ = 'ticket_acknowledgements'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('queue_tickets.id'), nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('queue_customers.id'), nullable=True, index=True)
    ack_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    info = db.Column(db.String(200), nullable=True)

    def __repr__(self):
        return f"<Ack ticket={self.ticket_id} customer={self.customer_id} at={self.ack_time}>"


class AdvancePayment(db.Model):
    """A document paid for before the student shows up for a ticket"""
    __tablename__ = 'advance_payments'
    
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('queue_customers.id'), nullable=False)
    document_name = db.Column(db.String(200), nullable=False)
    payment_status = db.Column(db.String(20), default='paid')
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)

    customer = db.relationship('QueueCustomer', backref='advance_payments')