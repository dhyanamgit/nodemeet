"""Use your existing Postgres/MySQL via SQLAlchemy (pip install "nodemeet[postgres]")."""
from sqlalchemy.ext.asyncio import create_async_engine

from nodemeet import NodeMeet
from nodemeet.storage.sql import SQLAlchemyStorage

engine = create_async_engine("postgresql+asyncpg://app:secret@localhost/app", pool_size=10)
storage = SQLAlchemyStorage(engine=engine, table_prefix="nm_")  # tables: nm_rooms, nm_bookings...
meet = NodeMeet("change-me-to-a-long-random-secret", storage=storage)

# Tables are created on startup, or ahead of time with:
#   nodemeet db upgrade --db-url postgresql+asyncpg://app:secret@localhost/app
# Already on Alembic? In env.py:
#   from nodemeet.storage.sql import metadata as nodemeet_metadata
#   target_metadata = [Base.metadata, nodemeet_metadata]
# and pass SQLAlchemyStorage(..., create_tables=False).

if __name__ == "__main__":
    meet.run(port=8080)
