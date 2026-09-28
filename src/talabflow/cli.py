"""Command-line interface: schema setup, staff management, running the bot and the worker."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import repository
from .bot import BotRunner
from .config import Settings, get_settings
from .db import build_engine, build_session_factory, create_all, session_scope
from .logging_setup import configure_logging
from .models import OrderStatus, StaffRole
from .outbox import OutboxWorker
from .repository import DuplicateUserError, InvalidTransitionError
from .security import PasswordPolicyError
from .transports import build_transport

app = typer.Typer(
    add_completion=False,
    help="Turn chat conversations into tracked orders.",
    no_args_is_help=True,
)
console = Console()


def _bootstrap(settings: Settings | None = None) -> tuple[Settings, object]:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, json_output=False)
    engine = build_engine(resolved)
    return resolved, build_session_factory(engine)


@app.command("init-db")
def init_db() -> None:
    """Create the database schema from the models."""
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    engine = build_engine(settings)
    create_all(engine)
    console.print(f"[green]schema ready[/] at {settings.database_url}")


@app.command("create-staff")
def create_staff(
    username: Annotated[str, typer.Argument(help="Login name.")],
    admin: Annotated[bool, typer.Option("--admin", help="Grant the admin role.")] = False,
    password: Annotated[
        str | None,
        typer.Option(
            help="Password. Omit to be prompted without echoing it to the terminal or shell "
            "history.",
        ),
    ] = None,
) -> None:
    """Create a staff user for the admin API."""
    settings, factory = _bootstrap()
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        with session_scope(factory) as session:  # type: ignore[arg-type]
            user = repository.create_staff_user(
                session,
                username=username,
                password=password,
                role=StaffRole.ADMIN if admin else StaffRole.STAFF,
            )
            console.print(f"[green]created[/] {user.username} with role {user.role.value}")
    except DuplicateUserError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    except PasswordPolicyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


@app.command("list-staff")
def list_staff() -> None:
    """List staff users."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:  # type: ignore[arg-type]
        users = repository.list_staff_users(session)
        if not users:
            console.print("[yellow]No staff users yet. Run `talabflow create-staff <name>`.[/]")
            return
        table = Table(title="Staff users")
        for column in ("id", "username", "role", "active"):
            table.add_column(column)
        for user in users:
            table.add_row(str(user.id), user.username, user.role.value, "yes" if user.is_active else "no")
        console.print(table)


@app.command("list-orders")
def list_orders(
    order_status: Annotated[str | None, typer.Option("--status", help="Filter by status.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
) -> None:
    """List recent orders."""
    _, factory = _bootstrap()
    parsed = None
    if order_status:
        try:
            parsed = OrderStatus(order_status)
        except ValueError as exc:
            valid = ", ".join(item.value for item in OrderStatus)
            console.print(f"[red]unknown status {order_status!r}. Valid values: {valid}[/]")
            raise typer.Exit(code=1) from exc

    with session_scope(factory) as session:  # type: ignore[arg-type]
        page = repository.list_orders(session, status=parsed, limit=limit)
        if not page.items:
            console.print("[yellow]No orders yet.[/]")
            return
        table = Table(title=f"Orders ({page.total} total)")
        for column in ("reference", "status", "service", "phone", "created"):
            table.add_column(column)
        for order in page.items:
            table.add_row(
                order.reference,
                order.status.value,
                order.service_type,
                order.contact_phone,
                order.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        console.print(table)


@app.command("set-status")
def set_status(
    reference: Annotated[str, typer.Argument(help="Order reference.")],
    to_status: Annotated[str, typer.Argument(help="Target status.")],
    note: Annotated[str | None, typer.Option(help="Optional note for the audit trail.")] = None,
    actor: Annotated[str, typer.Option(help="Who is making the change.")] = "cli",
) -> None:
    """Move an order to a new status, queueing the customer notification."""
    settings, factory = _bootstrap()
    try:
        target = OrderStatus(to_status)
    except ValueError as exc:
        valid = ", ".join(item.value for item in OrderStatus)
        console.print(f"[red]unknown status {to_status!r}. Valid values: {valid}[/]")
        raise typer.Exit(code=1) from exc

    with session_scope(factory) as session:  # type: ignore[arg-type]
        order = repository.get_order_by_reference(session, reference.strip().upper())
        if order is None:
            console.print(f"[red]no order with reference {reference!r}[/]")
            raise typer.Exit(code=1)
        try:
            repository.change_order_status(
                session,
                order=order,
                to_status=target,
                actor=actor,
                note=note,
                language=settings.default_language,
            )
        except InvalidTransitionError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=3) from exc
        console.print(
            f"[green]{order.reference}[/] is now [cyan]{target.value}[/]; "
            "customer notification queued"
        )


@app.command("run-bot")
def run_bot(
    max_polls: Annotated[
        int | None, typer.Option(help="Stop after this many polls (for demos and tests).")
    ] = None,
) -> None:
    """Run the chat bot, polling for customer messages."""
    settings, factory = _bootstrap()
    transport = build_transport(settings)
    runner = BotRunner(settings=settings, transport=transport, session_factory=factory)  # type: ignore[arg-type]
    runner.install_signal_handlers()
    console.print(f"[cyan]bot running[/] on transport={transport.name}  (Ctrl-C to stop)")
    try:
        handled = runner.run_forever(max_iterations=max_polls)
    finally:
        transport.close()
    console.print(f"handled {handled} message(s)")


@app.command("run-worker")
def run_worker(
    max_batches: Annotated[
        int | None, typer.Option(help="Stop after this many batches (for demos and tests).")
    ] = None,
) -> None:
    """Run the notification outbox worker."""
    settings, factory = _bootstrap()
    transport = build_transport(settings)
    worker = OutboxWorker(settings=settings, transport=transport, session_factory=factory)  # type: ignore[arg-type]
    console.print(f"[cyan]outbox worker running[/] on transport={transport.name}")
    try:
        sent, failed = worker.run_forever(max_iterations=max_batches)
    finally:
        transport.close()
    console.print(f"sent {sent}, failed {failed}")


@app.command("export")
def export(
    output: Annotated[Path, typer.Argument(help="Destination .xlsx or .csv file.")],
    limit: Annotated[int, typer.Option(min=1, max=5000)] = 1000,
) -> None:
    """Export orders to a spreadsheet."""
    from .exports import orders_to_csv, orders_to_xlsx

    _, factory = _bootstrap()
    suffix = output.suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        console.print("[red]output must end in .xlsx or .csv[/]")
        raise typer.Exit(code=1)

    with session_scope(factory) as session:  # type: ignore[arg-type]
        page = repository.list_orders(session, limit=limit)
        payload = orders_to_csv(page.items) if suffix == ".csv" else orders_to_xlsx(page.items)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    console.print(f"[green]wrote[/] {len(page.items)} order(s) to {output} ({len(payload)} bytes)")


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (development).")] = False,
) -> None:
    """Run the admin API.

    Binds 127.0.0.1 by default: exposing a staff API on every interface should be a deliberate
    decision, not the result of copying a command.
    """
    import uvicorn

    settings = get_settings()
    console.print(
        f"[cyan]talabflow admin API[/] on http://{host}:{port}  "
        f"(environment={settings.environment})"
    )
    uvicorn.run("talabflow.api:create_app", host=host, port=port, reload=reload, factory=True)


@app.command("config")
def show_config() -> None:
    """Print the effective configuration, with secrets redacted."""
    try:
        settings = get_settings()
    except Exception as exc:
        # Broad on purpose: this command exists to explain why configuration is invalid.
        console.print(f"[red]configuration is invalid:[/] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Effective configuration")
    table.add_column("setting")
    table.add_column("value")
    redacted = {"jwt_secret", "telegram_bot_token"}
    for name, value in settings.model_dump().items():
        if name in redacted:
            table.add_row(name, "<set, redacted>" if value else "<unset>")
        else:
            table.add_row(name, str(value))
    console.print(table)


def main() -> None:  # pragma: no cover - console entry point
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
