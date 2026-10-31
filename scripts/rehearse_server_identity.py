"""Exercise Phase 4 through the loopback TLS browser with fictional accounts.

Run with the checkout uv runner, ``--group quality --with playwright python``,
then this script. Supply ``--operator-email`` and ``--password-file`` for the
existing fictional operator. The script retains its fictional Organization and
audit history, prints no passwords or account links, and captures only pages
that contain no token material. It uses the installed Edge headless browser.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import urlsplit


def main() -> int:
    """Qualify browser form navigation without opening a remote destination."""

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument("--operator-email", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    origin = urlsplit(arguments.base_url)
    if origin.scheme != "https" or origin.hostname not in {"localhost", "127.0.0.1"}:
        parser.error("The rehearsal accepts only a loopback HTTPS origin")
    base_url = arguments.base_url.rstrip("/")
    operator_password = arguments.password_file.read_text(encoding="utf-8").rstrip(
        "\r\n"
    )
    if not 16 <= len(operator_password) <= 1024:
        parser.error("The operator password file is invalid")
    output = arguments.output or Path(mkdtemp(prefix="impodo-phase4-browser-"))
    output.mkdir(parents=True, exist_ok=True)
    identifier = "phase4-browser-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    admin_email = f"administrator@{identifier}.example.test"
    user_email = f"person@{identifier}.example.test"
    password = "Fictional browser trial password 2026!"
    secrets: list[str] = []
    requests: list[str] = []
    responses: list[tuple[str, int]] = []
    post_origins: list[str | None] = []
    step = "start browser"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            contexts = []

            def page_in_new_browser():
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1024},
                    ignore_https_errors=True,
                    locale="en-GB",
                )
                contexts.append(context)
                page = context.new_page()
                page.on("request", lambda request: requests.append(request.url))
                page.on(
                    "response",
                    lambda response: responses.append(
                        (urlsplit(response.url).path, response.status)
                    ),
                )
                page.on(
                    "request",
                    lambda request: (
                        post_origins.append(request.header_value("origin"))
                        if request.method == "POST"
                        else None
                    ),
                )
                return page

            def sign_in(page, email, candidate):
                page.goto(base_url + "/login")
                page.get_by_label("Login email", exact=True).fill(email)
                page.get_by_label("Password", exact=True).fill(candidate)
                page.get_by_role("button", name="Sign in", exact=True).click()

            def action_form(page, action):
                return page.locator(f'form:has(input[name="action"][value="{action}"])')

            def one_time_link(page):
                link = page.locator("textarea").input_value()
                if urlsplit(link).query or not urlsplit(link).fragment:
                    raise RuntimeError(
                        "A one-time link was not protected by a fragment"
                    )
                secrets.append(urlsplit(link).fragment)
                return link

            def consume(page, link, candidate):
                page.goto(link)
                expect(page.locator("#account-token")).not_to_have_value("")
                if urlsplit(page.url).fragment:
                    raise RuntimeError(
                        "The token fragment was not removed from history"
                    )
                page.get_by_label("New password", exact=True).fill(candidate)
                page.get_by_label("Repeat new password", exact=True).fill(candidate)
                page.locator('button[type="submit"]').click()
                expect(
                    page.get_by_role("link", name="Sign in", exact=True)
                ).to_be_visible()

            step = "platform sign-in and Organization creation"
            platform = page_in_new_browser()
            sign_in(platform, arguments.operator_email, operator_password)
            expect(
                platform.get_by_role("heading", name="Organizations", exact=True)
            ).to_be_visible()
            form = platform.locator('form[action="/admin/organizations"]')
            form.locator('[name="organization_id"]').fill(identifier)
            form.locator('[name="display_name"]').fill(
                f"Phase 4 fictional browser trial {identifier}"
            )
            form.get_by_role("button", name="Create Organization", exact=True).click()
            platform.get_by_role(
                "link", name=f"Phase 4 fictional browser trial {identifier}", exact=True
            ).click()
            step = "administrator assignment and activation"
            form = action_form(platform, "assign-administrator")
            form.get_by_label("Administrator display name").fill(
                "Fictional Organization Administrator"
            )
            form.get_by_label("Administrator email").fill(admin_email)
            form.locator('[name="confirmation"]').check()
            form.get_by_role("button").click()
            admin_link = one_time_link(platform)
            administrator = page_in_new_browser()
            consume(administrator, admin_link, password)
            sign_in(administrator, admin_email, password)
            expect(
                administrator.get_by_role("heading", name="Users", exact=True)
            ).to_be_visible()

            step = "ordinary user creation and activation"
            form = administrator.locator('form[action="/admin/users"][method="post"]')
            form.get_by_label("Display name", exact=True).fill(
                "Fictional migration user"
            )
            form.get_by_label("Login email", exact=True).fill(user_email)
            form.get_by_role("button").click()
            user_link = one_time_link(administrator)
            back_url = administrator.get_by_role(
                "link", name="Return to administration"
            ).get_attribute("href")
            user = page_in_new_browser()
            consume(user, user_link, password)
            sign_in(user, user_email, password)
            expect(
                user.get_by_role("heading", name="My account", exact=True)
            ).to_be_visible()
            administrator.goto(base_url + back_url)
            expect(
                administrator.get_by_role(
                    "heading", name="Fictional migration user", exact=True
                )
            ).to_be_visible()
            administrator.screenshot(
                path=str(output / "user-detail.png"), full_page=True
            )
            administrator.goto(base_url + "/admin/users")
            administrator.screenshot(path=str(output / "users.png"), full_page=True)
            administrator.set_viewport_size({"width": 390, "height": 844})
            administrator.screenshot(
                path=str(output / "users-narrow.png"), full_page=True
            )
            administrator.set_viewport_size({"width": 1440, "height": 1024})

            step = "password reset and old-session revocation"
            administrator.goto(base_url + back_url)
            form = action_form(administrator, "reset")
            form.locator('[name="confirmation"]').check()
            form.get_by_role("button").click()
            reset_link = one_time_link(administrator)
            recovery = page_in_new_browser()
            consume(recovery, reset_link, password + " changed")
            user.goto(base_url + "/account")
            expect(
                user.get_by_role("heading", name="Sign in to Impodo", exact=True)
            ).to_be_visible()
            sign_in(user, user_email, password + " changed")
            expect(
                user.get_by_role("heading", name="My account", exact=True)
            ).to_be_visible()

            step = "suspension and reactivation"
            administrator.goto(base_url + back_url)
            form = action_form(administrator, "suspend")
            form.locator('[name="confirmation"]').check()
            form.get_by_role("button").click()
            user.goto(base_url + "/account")
            expect(
                user.get_by_role("heading", name="Sign in to Impodo", exact=True)
            ).to_be_visible()
            form = action_form(administrator, "reactivate")
            form.locator('[name="confirmation"]').check()
            form.get_by_role("button").click()
            sign_in(user, user_email, password + " changed")
            expect(
                user.get_by_role("heading", name="My account", exact=True)
            ).to_be_visible()

            step = "session revocation and history"
            user_id = back_url.rsplit("/", 1)[-1]
            administrator.goto(base_url + "/admin/sessions?user_id=" + user_id)
            administrator.screenshot(path=str(output / "sessions.png"), full_page=True)
            form = administrator.locator('form[action$="/revoke"]').first
            form.locator('[name="confirmation"]').check()
            form.get_by_role("button").click()
            user.goto(base_url + "/account")
            expect(
                user.get_by_role("heading", name="Sign in to Impodo", exact=True)
            ).to_be_visible()
            administrator.goto(base_url + "/admin/history?user_id=" + user_id)
            expect(
                administrator.get_by_role(
                    "heading", name="Security history", exact=True
                )
            ).to_be_visible()
            administrator.screenshot(path=str(output / "history.png"), full_page=True)
            if any(secret in url for secret in secrets for url in requests):
                raise RuntimeError("An account token appeared in a network request URL")
            step = "administrator and operator logout"
            for page in (administrator, platform):
                page.goto(base_url + "/")
                page.locator('form[action="/logout"]').get_by_role(
                    "button", name="Sign out"
                ).click()
                expect(
                    page.get_by_role("heading", name="Sign in to Impodo", exact=True)
                ).to_be_visible()
            for context in contexts:
                context.close()
            browser.close()
        print(f"Phase 4 browser rehearsal passed. Fictional Organization: {identifier}")
        print(f"Screenshots without token material: {output}")
        print("Token-bearing request URLs: 0")
        return 0
    except (PlaywrightError, AssertionError, RuntimeError, ValueError) as error:
        # Playwright exception text can include the attempted fragment URL.
        # Report only the fixed step and exception class, never its text.
        print(
            f"Phase 4 browser rehearsal failed at {step} ({type(error).__name__}).",
            file=sys.stderr,
        )
        print(
            f"Recent response paths and status codes: {responses[-8:]}", file=sys.stderr
        )
        print(f"Recent POST Origin headers: {post_origins[-3:]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
