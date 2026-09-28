"""CAS adapter for the shared moderation worker."""

from app.auth import admin_user, current_user
from app.database import connect
from nethub_moderation.routes import make_router
from nethub_moderation.site import Site

site = Site(connect, "cas")
router = make_router(site, admin_user, current_user)
