from fastapi import FastAPI

from . import healths, v1


def init_routers(app: FastAPI):
    app.include_router(v1.router)
    app.include_router(healths.router)


__all__ = ["init_routers"]