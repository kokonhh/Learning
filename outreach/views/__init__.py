from flask import Blueprint

bp = Blueprint("main", __name__)
public_bp = Blueprint("public", __name__)

from . import contacts, outreach, sales, public  # noqa: E402,F401
