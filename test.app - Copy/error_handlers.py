from flask import render_template, flash, redirect, url_for
from models import db

def register_error_handlers(app):
    @app.errorhandler(404)
    def page_not_found(_):
        return render_template("404.html"), 404

    @app.errorhandler(403)
    def access_forbidden(_):
        flash("Access denied. Insufficient permissions.", "danger")
        return redirect(url_for("landing_page"))

    @app.errorhandler(500)
    def internal_error(_):
        db.session.rollback()
        flash("An unexpected error occurred. Please try again.", "danger")
        return redirect(url_for("landing_page"))