"""Django: put this in your project's asgi.py and run with uvicorn/daphne.

    uvicorn myproject.asgi:application
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django_app = get_asgi_application()

from nodemeet import NodeMeet  # noqa: E402
from nodemeet.asgi import route  # noqa: E402
from nodemeet.storage.sql import SQLAlchemyStorage  # noqa: E402

meet = NodeMeet(os.environ["NODEMEET_SECRET"], base_url="https://example.com/meet",
                storage=SQLAlchemyStorage(os.environ["NODEMEET_DB_URL"]))  # same Postgres as Django

application = route({"/meet": meet.asgi()}, default=django_app)

# In a Django view, after request.user is authenticated:
#   token = meet.create_token(f"project-{p.id}", str(request.user.id), "host", name=request.user.get_full_name())
#   return render(request, "call.html", {"embed": mark_safe(meet.embed_room(f"project-{p.id}", token))})
