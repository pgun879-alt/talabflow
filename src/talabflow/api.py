"""The staff admin API.

Authentication is a short-lived JWT obtained from ``POST /v1/auth/token``. Authorisation is
role-based: ``staff`` can read orders and move their status; ``admin`` additionally manages staff
accounts. The role check is a dependency, so a route cannot accidentally be left unguarded --
forgetting the dependency makes the route unreachable rather than public.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import repository
from .config import MIN_SECRET_LENGTH, PLACEHOLDER_SECRET, Settings, get_settings
from .db import build_engine, build_session_factory, create_all
from .exports import orders_to_csv, orders_to_xlsx
from .logging_setup import configure_logging, safe_extra
from .models import ALLOWED_TRANSITIONS, OrderStatus, StaffRole
from .repository import (
    DuplicateUserError,
    InvalidTransitionError,
    LastAdminError,
    StaffNotFoundError,
)
from .security import (
    PasswordPolicyError,
    SlidingWindowRateLimiter,
    TokenClaims,
    TokenError,
    create_access_token,
    decode_access_token,
    verify_password,
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- schemas


class TokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    # S105 flags any string assigned to a name containing "token". This is the OAuth token
    # *type* -- a protocol constant from RFC 6750, not a secret.
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in_seconds: int
    role: str


class CustomerSummary(BaseModel):
    id: int
    channel: str
    display_name: str | None
    phone: str | None


class OrderSummary(BaseModel):
    reference: str
    status: str
    service_type: str
    details: str
    contact_phone: str
    #: True only when the customer shared their own contact through the messaging app.
    contact_phone_verified: bool
    address: str
    created_at: datetime
    updated_at: datetime
    customer: CustomerSummary


class OrderEventModel(BaseModel):
    from_status: str | None
    to_status: str
    actor: str
    note: str | None
    created_at: datetime


class OrderDetail(OrderSummary):
    events: list[OrderEventModel]
    allowed_next_statuses: list[str]


class OrderListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[OrderSummary]


class StatusChangeRequest(BaseModel):
    to_status: OrderStatus
    note: str | None = Field(default=None, max_length=500)


class CreateStaffRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=10, max_length=256)
    role: StaffRole = StaffRole.STAFF


class UpdateStaffRequest(BaseModel):
    """Every field is optional; only the ones supplied are changed."""

    is_active: bool | None = None
    role: StaffRole | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class StaffSummary(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool


class StatsResponse(BaseModel):
    orders_by_status: dict[str, int]
    total_orders: int


class HealthResponse(BaseModel):
    status: str
    transport: str
    environment: str


def _to_summary(order: object) -> OrderSummary:
    from .models import Order  # local import keeps the module import graph acyclic

    assert isinstance(order, Order)
    customer = order.customer
    return OrderSummary(
        reference=order.reference,
        status=order.status.value,
        service_type=order.service_type,
        details=order.details,
        contact_phone=order.contact_phone,
        contact_phone_verified=order.contact_phone_verified,
        address=order.address,
        created_at=order.created_at,
        updated_at=order.updated_at,
        customer=CustomerSummary(
            id=customer.id,
            channel=customer.channel,
            display_name=customer.display_name,
            phone=customer.phone,
        ),
    )


# ----------------------------------------------------------------------- dependencies
#
# Module scope, not factory scope: with ``from __future__ import annotations`` FastAPI resolves
# route annotations against the module namespace, so an Annotated alias defined inside the app
# factory is unresolvable and every route silently treats its dependency as a body field.


def get_session(request: Request) -> Iterator[Session]:
    """Yield a request-scoped session that commits on success and rolls back on error."""
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    session: Session,
) -> TokenClaims:
    """Verify the bearer token, then re-check the account behind it against the database.

    A valid signature is not sufficient. Tokens live for up to an hour, so trusting the claims
    alone means a staff member who is deactivated -- or demoted from admin -- keeps their old
    access until the token happens to expire. Revocation that takes effect "within an hour" is not
    revocation.

    So the role used for authorisation is read from the row, not from the token, and a token whose
    account has been deleted or deactivated is rejected immediately. The cost is one indexed
    lookup per request, which is the right trade for a staff API.
    """
    settings: Settings = request.app.state.settings
    limiter: SlidingWindowRateLimiter = request.app.state.limiter

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    allowed, retry_after = limiter.check(f"user:{claims.subject}")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
            headers={"Retry-After": str(max(int(retry_after), 1))},
        )

    user = repository.get_staff_user(session, claims.subject)
    if user is None or not user.is_active:
        logger.warning(
            "token presented for an account that is gone or deactivated",
            extra={"subject": claims.subject, "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="this account is no longer active",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Authorise on the stored role, not the token's copy of it, so a demotion takes effect on the
    # next request rather than whenever the token happens to expire.
    return replace(claims, role=user.role.value)


def require_staff(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> TokenClaims:
    """Any authenticated, still-active staff member."""
    return _authenticate(request, credentials, session)


def require_admin(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> TokenClaims:
    """Admins only."""
    claims = _authenticate(request, credentials, session)
    if claims.role != StaffRole.ADMIN.value:
        logger.warning(
            "role check failed",
            extra={"subject": claims.subject, "role": claims.role, "path": request.url.path},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this action requires the admin role"
        )
    return claims


Db = Annotated[Session, Depends(get_session)]
StaffClaims = Annotated[TokenClaims, Depends(require_staff)]
AdminClaims = Annotated[TokenClaims, Depends(require_admin)]


# --------------------------------------------------------------------------- app


def _require_signing_secret(settings: Settings) -> None:
    """Refuse to serve the API with a guessable token-signing secret, in *any* environment.

    ``Settings`` only enforces this when ``environment`` is ``production`` -- but ``development``
    is the default, so a deployment that never set it would run with the placeholder printed in
    ``.env.example``. Anyone who knows that string and one admin username can sign their own admin
    token; no password is involved at all.

    The check lives here rather than in ``Settings`` because only the API signs and verifies
    tokens. The bot, the worker and the offline demo never touch the secret and must keep working
    without one.
    """
    secret = settings.jwt_secret
    if secret == PLACEHOLDER_SECRET or len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            "TALABFLOW_JWT_SECRET is the placeholder or is shorter than "
            f"{MIN_SECRET_LENGTH} characters, so the admin API will not start. Generate one: "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )


def create_app(settings: Settings | None = None, *, create_schema: bool = False) -> FastAPI:
    """Build the ASGI application."""
    resolved = settings or get_settings()
    _require_signing_secret(resolved)
    configure_logging(resolved.log_level)
    engine = build_engine(resolved)
    if create_schema:
        create_all(engine)
    session_factory = build_session_factory(engine)
    limiter = SlidingWindowRateLimiter(limit=resolved.api_rate_limit_per_minute)
    # Login gets its own, much tighter, limiters. `limiter` above is keyed on a verified token,
    # so it cannot protect the endpoint that issues tokens. The per-host ceiling is higher than
    # the per-account one because several members of staff may share one office address or sit
    # behind one reverse proxy.
    login_limiter = SlidingWindowRateLimiter(limit=resolved.login_attempts_per_minute)
    login_host_limiter = SlidingWindowRateLimiter(limit=resolved.login_attempts_per_minute * 5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "talabflow admin api ready",
            extra={"environment": resolved.environment, "transport": resolved.transport},
        )
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="talabflow admin API",
        version="0.1.0",
        summary="Staff API for orders captured through chat.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.session_factory = session_factory
    app.state.limiter = limiter

    if resolved.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    # ----------------------------------------------------------------- health

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok", transport=resolved.transport, environment=resolved.environment
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["health"])
    def readyz(session: Db, response: Response) -> HealthResponse:
        from sqlalchemy import text as sql_text

        try:
            session.execute(sql_text("SELECT 1"))
        except Exception:
            logger.exception("readiness check failed")
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(
                status="database-unavailable",
                transport=resolved.transport,
                environment=resolved.environment,
            )
        return HealthResponse(
            status="ok", transport=resolved.transport, environment=resolved.environment
        )

    # ----------------------------------------------------------------- auth

    @app.post("/v1/auth/token", response_model=TokenResponse, tags=["auth"])
    def issue_token(body: TokenRequest, request: Request, session: Db) -> TokenResponse:
        # Throttle before any password hashing. Every attempt counts, successful or not: counting
        # only failures would mean reading the outcome first, and the point is to refuse the
        # guess without evaluating it. scrypt is deliberately expensive, so this also stops the
        # endpoint being used to burn CPU.
        #
        # The trade-off, stated plainly: someone who knows a username can keep that account from
        # signing in for as long as they keep sending requests. Tokens already issued keep
        # working, and the alternative -- unlimited guesses -- is worse.
        account = body.username.strip().lower()
        host = request.client.host if request.client else "unknown"
        for throttle, identity in (
            (login_limiter, f"login-account:{account}"),
            (login_host_limiter, f"login-host:{host}"),
        ):
            allowed, retry_after = throttle.check(identity)
            if not allowed:
                logger.warning("login throttled", extra={"username": account, "host": host})
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="too many login attempts; try again later",
                    headers={"Retry-After": str(max(int(retry_after), 1))},
                )

        user = repository.get_staff_user(session, body.username)
        # One identical error for "no such user", "wrong password" and "deactivated": telling
        # them apart lets an attacker enumerate valid usernames.
        invalid = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )
        if user is None or not user.is_active:
            # Still hash-compare against a dummy so a missing user does not return measurably
            # faster than a wrong password.
            verify_password(body.password, "scrypt$16384$8$1$AAAA$AAAA")
            raise invalid
        if not verify_password(body.password, user.password_hash):
            logger.warning("failed login", extra={"username": user.username})
            raise invalid

        token = create_access_token(
            subject=user.username,
            role=user.role.value,
            secret=resolved.jwt_secret,
            algorithm=resolved.jwt_algorithm,
            ttl_minutes=resolved.access_token_ttl_minutes,
        )
        logger.info("login", extra={"username": user.username, "role": user.role.value})
        return TokenResponse(
            access_token=token,
            expires_in_seconds=resolved.access_token_ttl_minutes * 60,
            role=user.role.value,
        )

    # ----------------------------------------------------------------- orders

    @app.get("/v1/orders", response_model=OrderListResponse, tags=["orders"])
    def list_orders(
        session: Db,
        claims: StaffClaims,
        order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
        search: Annotated[str | None, Query(max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> OrderListResponse:
        page = repository.list_orders(
            session, status=order_status, search=search, limit=limit, offset=offset
        )
        return OrderListResponse(
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            items=[_to_summary(order) for order in page.items],
        )

    @app.get("/v1/orders/{reference}", response_model=OrderDetail, tags=["orders"])
    def get_order(reference: str, session: Db, claims: StaffClaims) -> OrderDetail:
        order = repository.get_order_by_reference(session, reference.strip().upper())
        if order is None:
            raise HTTPException(status_code=404, detail=f"no order with reference {reference!r}")
        events = repository.load_order_events(session, order.id)
        summary = _to_summary(order)
        return OrderDetail(
            **summary.model_dump(),
            events=[
                OrderEventModel(
                    from_status=event.from_status.value if event.from_status else None,
                    to_status=event.to_status.value,
                    actor=event.actor,
                    note=event.note,
                    created_at=event.created_at,
                )
                for event in events
            ],
            allowed_next_statuses=sorted(
                item.value for item in ALLOWED_TRANSITIONS.get(order.status, frozenset())
            ),
        )

    @app.post("/v1/orders/{reference}/status", response_model=OrderDetail, tags=["orders"])
    def change_status(
        reference: str, body: StatusChangeRequest, session: Db, claims: StaffClaims
    ) -> OrderDetail:
        order = repository.get_order_by_reference(session, reference.strip().upper())
        if order is None:
            raise HTTPException(status_code=404, detail=f"no order with reference {reference!r}")
        try:
            repository.change_order_status(
                session,
                order=order,
                to_status=body.to_status,
                actor=claims.subject,
                note=body.note,
                language=resolved.default_language,
            )
        except InvalidTransitionError as exc:
            # 409, not 400: the request is well-formed, it conflicts with current state.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        session.flush()
        return get_order(order.reference, session, claims)

    @app.get("/v1/orders-export", tags=["orders"])
    def export_orders(
        session: Db,
        claims: StaffClaims,
        export_format: Annotated[Literal["xlsx", "csv"], Query(alias="format")] = "xlsx",
        order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> Response:
        page = repository.list_orders(session, status=order_status, limit=limit, offset=0)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        if export_format == "csv":
            return Response(
                content=orders_to_csv(page.items),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="orders-{stamp}.csv"'},
            )
        return Response(
            content=orders_to_xlsx(page.items),
            media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            headers={"Content-Disposition": f'attachment; filename="orders-{stamp}.xlsx"'},
        )

    @app.get("/v1/stats", response_model=StatsResponse, tags=["orders"])
    def stats(session: Db, claims: StaffClaims) -> StatsResponse:
        counts = repository.count_orders_by_status(session)
        return StatsResponse(orders_by_status=counts, total_orders=sum(counts.values()))

    # ----------------------------------------------------------------- staff

    @app.get("/v1/staff", response_model=list[StaffSummary], tags=["staff"])
    def list_staff(session: Db, claims: AdminClaims) -> list[StaffSummary]:
        return [
            StaffSummary(
                id=user.id, username=user.username, role=user.role.value, is_active=user.is_active
            )
            for user in repository.list_staff_users(session)
        ]

    @app.post("/v1/staff", response_model=StaffSummary, status_code=201, tags=["staff"])
    def create_staff(body: CreateStaffRequest, session: Db, claims: AdminClaims) -> StaffSummary:
        try:
            user = repository.create_staff_user(
                session, username=body.username, password=body.password, role=body.role
            )
        except DuplicateUserError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PasswordPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info(
            "staff user created",
            extra=safe_extra(created_user=user.username, by=claims.subject),
        )
        return StaffSummary(
            id=user.id, username=user.username, role=user.role.value, is_active=user.is_active
        )

    @app.patch("/v1/staff/{username}", response_model=StaffSummary, tags=["staff"])
    def update_staff(
        username: str, body: UpdateStaffRequest, session: Db, claims: AdminClaims
    ) -> StaffSummary:
        """Deactivate or reactivate an account, change its role, or set a new password.

        Deactivation and demotion take effect on that user's next request. A new password does
        not cancel tokens already issued; deactivate the account to cut access immediately.
        """
        changed = sorted(body.model_dump(exclude_none=True))
        if not changed:
            raise HTTPException(status_code=422, detail="nothing to change")
        try:
            user = repository.update_staff_user(
                session,
                username,
                is_active=body.is_active,
                role=body.role,
                password=body.password,
            )
        except StaffNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastAdminError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PasswordPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The names of the fields that changed are logged; a new password's value never is.
        logger.info(
            "staff user updated",
            extra=safe_extra(target_user=user.username, by=claims.subject, changed=changed),
        )
        return StaffSummary(
            id=user.id, username=user.username, role=user.role.value, is_active=user.is_active
        )

    @app.exception_handler(500)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app
