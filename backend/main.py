from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import admin, auth, hazards, live_detect, navigation, risk_events
from config import settings
from database import Base, engine
from models import HazardReport, RiskEvent, User  # noqa: F401  (registers tables)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)   # swap for Alembic migrations once the schema stabilizes
    yield


app = FastAPI(title="RoadGuard", version="0.3.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(hazards.router)
app.include_router(admin.router)
app.include_router(navigation.router)
app.include_router(live_detect.router)
app.include_router(risk_events.router)
app.include_router(risk_events.admin_router)

app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
# Frontend last, so /api and /uploads take priority. Visit / and /admin.html
app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
