"""Preflight must flag any DocuSeal route outside the guarded handle blocks."""

import pytest

from deploy import preflight

CADDYFILE = (preflight.ROOT / "deploy" / "PublicCaddyfile").read_text()
FINAL = "\thandle {\n\t\trespond 404\n\t}\n"

UNGUARDED = [
    # inline path matcher on handle
    "\thandle /api/* {\n\t\treverse_proxy docuseal:3000\n\t}\n",
    # handle_path and route blocks
    "\thandle_path /sign_in* {\n\t\treverse_proxy docuseal:3000\n\t}\n",
    "\troute /setup* {\n\t\treverse_proxy docuseal:3000\n\t}\n",
    # site-level proxy with an inline matcher
    "\treverse_proxy /api/* docuseal:3000\n",
]


@pytest.mark.parametrize("snippet", UNGUARDED)
def test_preflight_flags_any_unguarded_docuseal_route(snippet: str) -> None:
    assert FINAL in CADDYFILE
    weakened = CADDYFILE.replace(FINAL, snippet + "\n" + FINAL, 1)
    assert preflight.public_caddyfile_problems(weakened) != []
