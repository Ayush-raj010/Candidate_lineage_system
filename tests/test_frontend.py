"""Checks that the dashboard is served correctly and stays in step with the API.

The JavaScript unit tests live in tests/frontend/ and run under Node's built-in
test runner; test_javascript_unit_tests runs them from pytest when Node is installed.
"""

import re
import shutil
import subprocess
import typing
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.ai_summary import SUMMARY_SCHEMA
from app.db import BASE_DIR
from app.main import STATIC_DIR, create_app
from app.models import SortOrder, SummaryOut
from tests.helpers import signed_in_client


@pytest.fixture
def client(db_path):
    with signed_in_client(db_path) as test_client:
        yield test_client


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for key in ("src", "href"):
            if attrs.get(key, "").startswith("/static/"):
                self.assets.append(attrs[key])


def parse_index(page="index.html"):
    parser = _AssetParser()
    parser.feed((STATIC_DIR / page).read_text(encoding="utf-8"))
    return parser


def read_js(name):
    return (STATIC_DIR / "js" / name).read_text(encoding="utf-8")


# --- Serving ----------------------------------------------------------------

def test_dashboard_is_served_at_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>GCCX Candidate Triage</title>" in response.text


def test_every_asset_in_index_is_served_with_the_right_type(client):
    assets = parse_index().assets

    assert "/static/js/app.js" in assets and "/static/css/styles.css" in assets
    for path in assets:
        response = client.get(path)
        assert response.status_code == 200, path
        expected = "text/javascript" if path.endswith(".js") else "text/css"
        assert response.headers["content-type"].startswith(expected), (path, response.headers["content-type"])


def test_page_and_assets_are_revalidated_so_updates_are_not_served_stale(client):
    for path in ["/", *parse_index().assets]:
        response = client.get(path)
        assert response.headers["cache-control"] == "no-cache", path
        if path != "/":
            etag = response.headers["etag"]
            assert client.get(path, headers={"If-None-Match": etag}).status_code == 304, path


def test_module_imports_resolve(client):
    for name in ("app.js", "api.js", "utils.js", "login.js"):
        for target in re.findall(r'from "\./([\w.]+)"', read_js(name)):
            assert client.get(f"/static/js/{target}").status_code == 200, (name, target)


def test_element_ids_used_by_the_script_exist_in_the_page():
    ids = parse_index().ids
    script = read_js("app.js")
    # Ids the script creates itself are not in index.html.
    created = set(re.findall(r'id: "([\w-]+)"', script))
    used = set(re.findall(r'\$\("#([\w-]+)"\)', script)) | set(re.findall(r'\$\(`#([\w-]+)`\)', script))

    assert used - created - ids == set()


def test_element_ids_used_by_the_sign_in_script_exist_in_its_page():
    ids = parse_index("login.html").ids
    used = set(re.findall(r'\$\("#([\w-]+)"\)', read_js("login.js")))

    assert used and used - ids == set()


def test_sign_in_page_assets_are_served(client):
    for path in parse_index("login.html").assets:
        assert client.get(path).status_code == 200, path


def test_api_routes_are_unchanged_by_the_static_mount(client):
    assert client.get("/api/nope").json() == {"detail": "Not Found"}
    assert client.get("/static/nope.js").status_code == 404
    assert client.get("/api/health").status_code == 200


def test_static_files_cannot_escape_the_static_folder(client):
    for path in ("/static/../app/main.py", "/static/%2e%2e/app/main.py", "/static/..%2fapp%2fmain.py"):
        assert client.get(path).status_code == 404, path


def test_page_uses_no_external_resources():
    html = "".join((STATIC_DIR / page).read_text(encoding="utf-8") for page in ("index.html", "login.html"))
    css = (STATIC_DIR / "css" / "styles.css").read_text(encoding="utf-8")

    assert "http://" not in html + css and "https://" not in html + css


def test_script_never_injects_html():
    """API data (names, notes, AI output) must reach the page as text, never as markup."""
    html_sinks = re.compile(r"\.(?:inner|outer)HTML\s*[+]?=|insertAdjacentHTML\s*\(|document\.write\s*\(")
    for name in ("app.js", "api.js", "utils.js", "login.js"):
        assert not html_sinks.search(read_js(name)), name


def test_no_inline_styles_or_scripts_that_the_csp_would_block():
    for page in ("index.html", "login.html"):
        html = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert " style=" not in html and re.search(r"<script(?![^>]*\bsrc=)", html) is None, page
        assert not re.search(r"\son[a-z]+=", html), page
    for name in ("app.js", "login.js"):
        assert not re.search(r'\bstyle: |setAttribute\("style"', read_js(name)), name


def test_security_headers(client):
    for path in ("/", "/api/health", "/static/js/app.js"):
        headers = client.get(path).headers
        assert "frame-ancestors 'none'" in headers["content-security-policy"], path
        assert "script-src 'self'" in headers["content-security-policy"], path
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["x-frame-options"] == "DENY", path


def test_sign_in_page_is_served_to_signed_out_visitors(db_path):
    with TestClient(create_app(db_path)) as anonymous:
        response = anonymous.get("/login")

    assert response.status_code == 200
    assert "Sign in" in response.text and 'autocomplete="current-password"' in response.text
    assert "register" not in response.text.lower() and "sign up" not in response.text.lower()


def test_forms_with_passwords_never_submit_them_in_the_url():
    """Found in browser testing: submitting before the script loaded put the password in the URL."""
    for page in ("index.html", "login.html"):
        html = (STATIC_DIR / page).read_text(encoding="utf-8")
        for form in re.findall(r"<form\b[^>]*>.*?</form>", html, re.DOTALL):
            if 'type="password"' in form:
                assert re.search(r'<form\b[^>]*\bmethod="post"', form), (page, form[:80])


def test_sign_in_button_waits_for_the_script():
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")

    assert re.search(r'<button[^>]*id="login-submit"[^>]*\bdisabled\b', html)
    assert "submit.disabled = false" in read_js("login.js")


def test_posting_the_sign_in_page_itself_is_refused(db_path):
    with TestClient(create_app(db_path)) as anonymous:
        response = anonymous.post("/login", data={"email": "a@b.co", "password": "secret"})

    assert response.status_code == 405 and "secret" not in response.text


def test_session_storage_is_never_used_for_credentials():
    for name in ("app.js", "api.js", "login.js"):
        script = read_js(name)
        assert "localStorage" not in script and "sessionStorage" not in script and "document.cookie" not in script, name


# --- Contract with the API ----------------------------------------------------

def test_every_api_call_matches_a_documented_route(client):
    spec = client.app.openapi()["paths"]
    calls = re.findall(r'request\("(GET|POST|PUT|PATCH|DELETE)", [`"](/api[^`"]*)[`"]', read_js("api.js"))

    assert len(calls) == 21  # 20 endpoint functions + the /api/auth/me call that refreshes a stale CSRF token
    for method, path in calls:
        template = re.sub(r"\$\{[^}]+\}", "{}", path)
        matches = [p for p in spec if re.sub(r"\{[^}]+\}", "{}", p) == template and method.lower() in spec[p]]
        assert matches, f"{method} {path} has no matching API route"


def test_every_documented_route_is_used_by_the_dashboard(client):
    spec = client.app.openapi()["paths"]
    js = read_js("api.js")
    used = {(m, re.sub(r"\$\{[^}]+\}", "{}", p)) for m, p in re.findall(r'request\("(\w+)", [`"](/api[^`"]*)[`"]', js)}

    for path, operations in spec.items():
        for method in operations:
            assert (method.upper(), re.sub(r"\{[^}]+\}", "{}", path)) in used, f"{method.upper()} {path} unused"


def test_sort_options_match_the_api_allowlist():
    js_values = re.findall(r'\{ value: "(-?\w+)", label', read_js("utils.js").split("SHORTLIST_OPTIONS")[0])

    assert sorted(js_values) == sorted(order.value for order in SortOrder)


def test_fit_labels_cover_every_fit_value():
    block = read_js("utils.js").split("FIT_LABELS")[1].split("});")[0]
    js_fits = set(re.findall(r"^\s*(\w+):", block, re.MULTILINE))
    api_fits = set(typing.get_args(SummaryOut.model_fields["fit"].annotation))

    assert js_fits == api_fits == set(SUMMARY_SCHEMA["properties"]["fit"]["enum"])


def test_page_sizes_are_within_the_api_limit():
    sizes = [int(n) for n in re.search(r"PAGE_SIZES = Object.freeze\(\[([\d, ]+)\]\)", read_js("utils.js")).group(1).split(",")]

    assert sizes and all(1 <= n <= 200 for n in sizes)


# --- JavaScript unit tests ------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_unit_tests():
    result = subprocess.run(
        ["node", "--test", "--test-reporter=tap", "tests/frontend/utils.test.js", "tests/frontend/api.test.js"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^# fail 0$", result.stdout, re.MULTILINE), result.stdout
    assert int(re.search(r"^# pass (\d+)$", result.stdout, re.MULTILINE).group(1)) > 0
