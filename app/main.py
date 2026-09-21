# app/main.py
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app import core
from app.routes import init_routers
from app.middleware import init_middleware
from app.lifecycles import lifespan

app = FastAPI(lifespan=lifespan, **core.FASTAPI_CONFIGS)

Instrumentator().instrument(app).expose(app)

init_routers(app)
init_middleware(app)