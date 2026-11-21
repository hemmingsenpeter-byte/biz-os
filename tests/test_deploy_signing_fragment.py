"""Round-2 review proof: a raw '#' ends the guard's path scan but not Caddy's path.

Go's url.ParseRequestURI keeps '#' in URL.Path ("assumed not to have a #fragment"), so
Caddy cleans '/sign_in#/../s/x' to '/s/x' and the path matcher matches /s/*. The guard's
`^[^?#]*` stops at '#', so it never sees the dot-segment. Upstream then receives
'/sign_in%23/../s/x' (EscapedPath escapes '#'), not the path that matched.
"""

import pytest

from tests.test_deploy_signing_paths import _forwarded

FRAGMENT_ATTACKS = [
    "/sign_in#/../s/x",
    "/api/templates#/../../s/x",
    "/sign_in.x#%2F..%2Fdisk%2Fk/y",
]


@pytest.mark.parametrize("raw", FRAGMENT_ATTACKS)
def test_raw_hash_does_not_hide_dot_segments(raw: str) -> None:
    assert _forwarded(raw) is None, f"{raw} passes the guard and is forwarded"
