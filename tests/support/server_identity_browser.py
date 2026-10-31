"""Build a fictional server identity application for PostgreSQL browser evidence."""

from __future__ import annotations

from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from impodo.adapters.identity.scrypt_passwords import ScryptPasswordHasher
from impodo.adapters.identity.secure_recovery_tokens import (
    HmacIdentifierDigester,
    SecureRecoveryTokenFactory,
)
from impodo.adapters.identity.secure_sessions import SecureSessionSecretFactory
from impodo.adapters.postgresql.identity_administration import (
    PsycopgIdentityAdministrationRepository,
)
from impodo.adapters.postgresql.platform_bootstrap import (
    PsycopgPlatformAdministratorBootstrapRepository,
)
from impodo.adapters.postgresql.platform_recovery import (
    PsycopgAccountRecoveryRepository,
)
from impodo.adapters.postgresql.platform_sessions import PsycopgServerSessionRepository
from impodo.adapters.postgresql.server_authentication import (
    PsycopgServerAuthenticationRepository,
)
from impodo.application.identity.administration_service import (
    IdentityAdministrationService,
)
from impodo.application.identity.bootstrap_service import (
    PlatformAdministratorBootstrapService,
)
from impodo.application.identity.recovery_service import AccountRecoveryService
from impodo.application.identity.server_authentication_service import (
    ServerAuthenticationService,
)
from impodo.application.identity.session_service import ServerSessionService
from impodo.domain.identity.bootstrap import PlatformAdministratorBootstrapCommand
from impodo.web.composition.server_administration import (
    ServerAdministrationDependencies,
    create_server_administration_router,
)
from impodo.web.composition.server_linux_administration import (
    _LocalTrialRecoveryDelivery,
)
from impodo.web.composition.server_session_http import ServerBrowserSecurityMiddleware

ORIGIN = "https://impodo.example.test"
PASSWORD = "Fictional trial password 2026!"
PLATFORM_EMAIL = "operator@example.test"
ROOT = Path(__file__).resolve().parents[2]


class ServerFormParser(HTMLParser):
    """Read server form values and the one-time textarea without printing secrets."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.forms: list[tuple[str, dict[str, str]]] = []
        self.current: dict[str, str] | None = None
        self.in_textarea = False
        self.link = ""
        self.feed(markup)

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.current = {}
            self.forms.append((attributes.get("action", ""), self.current))
        elif tag == "input" and self.current is not None and attributes.get("name"):
            self.current[attributes["name"]] = attributes.get("value", "")
        elif tag == "textarea":
            self.in_textarea = True

    def handle_endtag(self, tag) -> None:
        if tag == "form":
            self.current = None
        elif tag == "textarea":
            self.in_textarea = False

    def handle_data(self, data) -> None:
        if self.in_textarea:
            self.link += data

    def form(self, action: str, *, operation: str | None = None) -> dict[str, str]:
        for target, values in self.forms:
            if target == action and (
                operation is None or values.get("action") == operation
            ):
                return values.copy()
        raise AssertionError("Expected server form was not rendered")

    def token(self) -> str:
        fragment = urlsplit(self.link).fragment
        if not fragment or urlsplit(self.link).query:
            raise AssertionError("Expected a fragment-only one-time link")
        return fragment


def build_server_identity_application(dsn: str, metrics: list) -> FastAPI:
    """Bootstrap and bind the real identity services against a disposable store."""

    reference = PurePosixPath("/run/secrets/postgres-runtime-dsn")
    read_dsn = lambda _reference: dsn
    password_hasher = ScryptPasswordHasher()
    token_factory = SecureRecoveryTokenFactory()
    digester = HmacIdentifierDigester(b"fictional-browser-test-key" + b"x" * 32)
    PlatformAdministratorBootstrapService(
        PsycopgPlatformAdministratorBootstrapRepository(read_dsn, reference),
        password_hasher,
    ).bootstrap(
        PlatformAdministratorBootstrapCommand(
            login_email=PLATFORM_EMAIL,
            display_name="Fictional operator",
            password=PASSWORD,
        ),
        deployment_id="local-trial",
    )
    dependencies = ServerAdministrationDependencies(
        authenticator=ServerAuthenticationService(
            PsycopgServerAuthenticationRepository(
                read_dsn, reference, metrics_observer=metrics.append
            ),
            password_hasher,
            identifier_digester=digester,
        ),
        sessions=ServerSessionService(
            PsycopgServerSessionRepository(
                read_dsn, reference, metrics_observer=metrics.append
            ),
            SecureSessionSecretFactory(),
        ),
        organizations=IdentityAdministrationService(
            PsycopgIdentityAdministrationRepository(
                read_dsn, reference, metrics_observer=metrics.append
            ),
            token_factory,
            password_hasher,
        ),
        identifier_digester=digester,
        templates=Jinja2Templates(directory=ROOT / "src/impodo/web/templates"),
        deployment_id="local-trial",
        session_absolute_timeout=timedelta(hours=12),
        account_recovery=AccountRecoveryService(
            PsycopgAccountRecoveryRepository(
                read_dsn, reference, metrics_observer=metrics.append
            ),
            token_factory,
            digester,
            password_hasher,
            _LocalTrialRecoveryDelivery(),
        ),
    )
    app = FastAPI()
    app.include_router(create_server_administration_router(dependencies))
    app.add_middleware(ServerBrowserSecurityMiddleware, public_url=ORIGIN)
    return app
