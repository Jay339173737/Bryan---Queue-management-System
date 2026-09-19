import click
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
from models import db, AdminUser, SystemConfig, QueueTicket

#$env:FLASK_APP = "apps12.py"

def register_cli_commands(app):
    """Attach all custom click commands to the app."""

    @app.cli.command("initdb")
    def initdb():
        """Create tables + default admin + daily limit."""
        db.create_all()

        if not AdminUser.query.filter_by(username="admin").first():
            db.session.add(AdminUser(username="admin", password="admin123", role="admin"))
            print("✅ Default administrator created (username: admin, password: admin123)")

        if not SystemConfig.query.filter_by(config_key="daily_ticket_limit").first():
            db.session.add(SystemConfig(config_key="daily_ticket_limit", config_value="300"))
            print("✅ Default daily ticket limit set to 300")

        db.session.commit()
        print("✅ Database initialised.")

    @app.cli.command("create_admin")
    @click.option("--username", prompt=True)
    @click.password_option("--password", confirmation_prompt=True)
    def create_admin(username, password):
        if AdminUser.query.filter_by(username=username).first():
            print("❌ Username already exists."); return
        try:
            db.session.add(AdminUser(username=username, password=password, role="admin"))
            db.session.commit()
            print(f"✅ Administrator '{username}' created.")
        except IntegrityError:
            db.session.rollback()
            print("❌ Failed to create admin.")

    @app.cli.command("reset_queue")
    def reset_queue():
        n = QueueTicket.query.filter(
            QueueTicket.ticket_status.in_(["serving", "served", "no_show"])
        ).update({"ticket_status": "waiting"})
        db.session.commit()
        print(f"✅ Reset {n} tickets to waiting.")

    @app.cli.command("cleanup_old")
    @click.argument("age_days", type=int, default=30)
    def cleanup_old(age_days):
        cutoff = datetime.utcnow() - timedelta(days=age_days)
        removed = QueueTicket.query.filter(QueueTicket.issue_time < cutoff).delete()
        db.session.commit()
        print(f"✅ Removed {removed} tickets older than {age_days} days.")