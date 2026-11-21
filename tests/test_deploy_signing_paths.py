"""Review proof: escaped dot-segments must not pass the signing matchers to other routes.

Caddy's path matcher lowercases r.URL.Path (percent-decoded) and runs path.Clean on it
(caddyhttp.CleanPath, see caddy#4407) before matching; reverse_proxy then forwards the
original escaped path unless the handle rewrites it. The webhook handles rewrite to a
fixed target for exactly this reason; the signing handles do not. Rails/Journey matches the
raw (still-escaped) path, and its default segment pattern [^/.?]+ accepts %2E/%2F.
"""

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from deploy import preflight

CADDYFILE = (Path(__file__).resolve().parents[1] / "deploy" / "PublicCaddyfile").read_text()


def _block(name: str) -> str:
    m = re.search(rf"handle @{name} \{{(.*?)\n\t\}}", CADDYFILE, re.S)
    assert m
    return m.group(1)


def _patterns(name: str) -> list[str]:
    m = re.search(rf"@{name} \{{.*?\bpath ([^\n]+)", CADDYFILE, re.S)
    assert m
    return m.group(1).split()


def _caddy_matches(raw_path: str, patterns: list[str]) -> bool:
    cleaned = posixpath.normpath(re.sub("/+", "/", unquote(raw_path).lower()))
    return any(
        cleaned.startswith(p[:-1]) if p.endswith("*") else cleaned == p for p in patterns
    )


# Devise's POST /sign_in(.:format): format segment = [^/.?]+ against the escaped path.
RAILS_SIGN_IN = re.compile(r"^/sign_in(?:\.([^/.?]+))?$")
ATTACK = "/sign_in.x%2F%2E%2E%2Fs%2Fx"


def test_escaped_dot_segments_do_not_reach_sign_in() -> None:
    assert RAILS_SIGN_IN.match(ATTACK)  # what DocuSeal would route the raw path to
    if _caddy_matches(ATTACK, _patterns("signing_form")):
        block = _block("signing_form")
        assert "rewrite" in block or "uri" in block, (
            f"{ATTACK} matches @signing_form after Caddy cleaning and is proxied unchanged "
            "to DocuSeal, which routes it to POST /sign_in"
        )


# What the gateway routes, modelled on its matchers: a request reaches DocuSeal when the
# cleaned path matches a matcher's patterns and its raw URI passes that matcher's guard.
ROUTES = ("signing_form", "signing_asset", "signing_disk")
SIGNER_SEGMENTS = {
    "s",
    "d",
    "p",
    "submit_form_email_2fa",
    "start_form_email_2fa_send",
    "resubmit_form",
    "packs",
    "preview",
    "blobs_proxy",
    "disk",
    "fonts",
    "favicon.ico",
    "favicon.svg",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "favicon-96x96.png",
    "apple-icon-180x180.png",
}


def _guard(name: str) -> re.Pattern[str]:
    m = re.search(
        rf"@{name} \{{[^`]*?not vars_regexp \{{http\.request\.orig_uri\}} `([^`]+)`",
        CADDYFILE,
    )
    assert m, f"@{name} lost its raw-URI guard"
    return re.compile(m.group(1))


def _forwarded(raw: str) -> str | None:
    """The path DocuSeal receives for a raw request URI, or None when the gateway 404s."""
    path = raw.split("?", 1)[0]
    for name in ROUTES:
        if _caddy_matches(path, _patterns(name)) and not _guard(name).search(raw):
            # Form and asset handlers rewrite to the cleaned path; disk forwards raw.
            return path if name == "signing_disk" else unquote(path)
    return None


ATTACKS = [
    ATTACK,
    "/s/x/%2e%2e/%2e%2e/sign_in",
    "/s/x/..%2F..%2Fsign_in",
    "/s/x%2F..%2F..%2Fapi%2Ftemplates",
    "/sign_in.x%2f%2e%2e%2fpacks%2fx",
    "/api%2F..%2Fs%2Fx",
    "/s/x/../../api/templates",
    "/s/./x",
    "/s//x",
    "/s/x%5C..%5Capi",
    "/s/x\\..\\api",
    "/s/%252e%252e/x",
    "/s/x?_method=delete",
    "/s/x?a=1&_METHOD=DELETE",
    "/s/x?%5fmethod=delete",
    "/disk/k%2F..%2F..%2Fsign_in",
    "/disk/k%2F%2Fx",
    "/disk/%2e%2e/sign_in",
    "/disk/../sign_in",
    "/sign_in.x%2F..%2Fdisk%2Fx",
    "/file/x/y%2F..%2F..%2F..%2Fdisk%2Fz",
    "/settings/api",
    "/setup",
    "/api/templates",
]
SIGNER_REQUESTS = [
    "/s/Ab12Cd34",
    "/s/Ab12Cd34?t=SDFsdf&lang=en",
    "/s/Ab12Cd34/values?field_uuid=1f2e",
    "/s/Ab12Cd34/completed",
    "/d/Tm9vbg/completed?submitter_slug=x",
    "/p/Ab12Cd34?f=1f2e",
    "/submit_form_email_2fa",
    "/packs/js/form-dd3c3b94e2c159ef2ec1.js",
    "/preview/eyJfcmFpbHMiOnt9fQ--abc123/0.jpg",
    "/blobs_proxy/eyJfcmFpbHMiOnt9fQ--abc123/Quote%20v2.pdf",
    "/disk/eyJfcmFpbHMiOn%2Bt9fQ%2F%2Bx%3D%3D--abc123/0.png",
]


@pytest.mark.parametrize("raw", ATTACKS)
def test_gateway_refuses_escaped_and_dot_segment_requests(raw: str) -> None:
    assert _forwarded(raw) is None


@pytest.mark.parametrize("raw", SIGNER_REQUESTS)
def test_gateway_forwards_signer_requests_on_the_path_that_matched(raw: str) -> None:
    forwarded = _forwarded(raw)
    assert forwarded is not None
    segments = forwarded.split("/")
    assert segments[1] in SIGNER_SEGMENTS
    assert ".." not in segments and "." not in segments and "" not in segments[1:]


def test_every_docuseal_route_keeps_its_guards() -> None:
    assert preflight.public_caddyfile_problems(CADDYFILE) == []
    unguarded = CADDYFILE.replace("\t\tnot header X-HTTP-Method-Override *\n", "", 1)
    assert preflight.public_caddyfile_problems(unguarded) == [
        "PublicCaddyfile route signing_form must keep the signing host, raw-URI and "
        "method-override guards"
    ]
    unrewritten = CADDYFILE.replace("\t\turi query -_method\n", "", 1)
    assert preflight.public_caddyfile_problems(unrewritten) != []
