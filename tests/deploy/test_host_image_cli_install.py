"""Regression tests for managed host image CLI availability."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_install_pinned_kiro_cli(dockerfile: Path) -> None:
    """Managed host images must preinstall a *pinned* Kiro CLI binary.

    The public npm package named ``kiro-cli`` is unrelated and exposes no
    ``kiro-cli`` binary. Kiro's ``curl …/install`` script has no version flag
    (it always fetches ``latest``), so the images instead pull the immutable,
    versioned per-arch zip from the CDN, verify its sha256, and copy the binary
    onto the global PATH (see the pinning rationale in the Dockerfiles). This
    guards both that the pin stays in place and that the old unpinned installer
    never creeps back.
    """
    text = dockerfile.read_text()

    # Pinned to an explicit version, fetched from the immutable versioned CDN
    # path — not the unpinned ``cli.kiro.dev/install`` script, not ``…/latest/``.
    assert "ARG KIRO_CLI_VERSION=" in text
    assert "https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/" in text
    assert "https://cli.kiro.dev/install" not in text
    # Integrity-checked, then copied onto the global PATH for all sandbox users.
    assert "sha256sum -c" in text
    assert "install -m 0755 /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli" in text
    # The installer's per-user copies are removed in the same layer; leaving
    # them doubled the image by ~850MB.
    assert "/root/.local/bin/kiro-cli*" in text
    # kiro-cli is not an npm package, so it must not appear in the npm install list.
    assert "      kiro-cli \\" not in text


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_drop_sdk_bundled_claude(dockerfile: Path) -> None:
    """The host venv omits claude_agent_sdk's bundled CLI; the server venv keeps it.

    The host stage installs the npm ``claude`` the claude-sdk executor always
    passes as ``cli_path``, so the bundled copy is dead weight there. The trim
    is gated on ``claude`` being baked, so the SDK keeps a fallback otherwise.
    """
    text = dockerfile.read_text()
    host_builder = text.split("AS host-builder", 1)[1].split("\nFROM ", 1)[0]
    assert "claude_agent_sdk/_bundled" in host_builder
    assert "claude) rm -rf" in host_builder
    host = text.split("AS host\n", 1)[1].split("\nFROM ", 1)[0]
    assert "COPY --from=host-builder /opt/venv /opt/venv" in host
    assert "@anthropic-ai/claude-code" in host
    runtime = text.split("AS runtime", 1)[1]
    assert "COPY --from=host-builder" not in runtime


@pytest.mark.parametrize(
    ("dockerfile", "default_set"),
    [
        (_ROOT / "deploy/docker/Dockerfile", "claude codex pi kiro agy"),
        (_ROOT / "deploy/docker/Dockerfile.ubi", "claude codex pi kiro"),
    ],
)
def test_host_harnesses_default_bakes_full_set(dockerfile: Path, default_set: str) -> None:
    """``HOST_HARNESSES`` defaults to every baked CLI and gates each install.

    Trimming is opt-in, so the published image keeps its CLI set; each
    vendor-installer step must skip cleanly when its name is dropped.
    """
    text = dockerfile.read_text()
    assert f'ARG HOST_HARNESSES="{default_set}"' in text
    assert "kiro-cli skipped (not in HOST_HARNESSES)" in text
    if "agy" in default_set.split():
        assert "agy skipped (not in HOST_HARNESSES)" in text
    assert "unknown HOST_HARNESSES entry" in text


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_include_kiro_installer_dependency(dockerfile: Path) -> None:
    """Kiro's installer needs ``unzip`` on Linux."""
    text = dockerfile.read_text()
    assert "unzip" in text


def test_extra_cli_rows_match_harness_install_table() -> None:
    """install-harness-cli.sh's npm + goose rows stay in sync with _HARNESS_INSTALL.

    The script resolves ``EXTRA_HARNESS_CLIS`` names to the same npm package
    and default pin the runtime installs via ``omnigent setup``, behind a
    "keep in sync" comment. A drift would bake a package or pin the runtime
    then rejects (opencode's runtime gate bounds 1.18.x), so assert the two
    tables agree instead of trusting the comment.
    """
    from omnigent.onboarding import harness_install as hi

    script = (_ROOT / "deploy/docker/install-harness-cli.sh").read_text()

    opencode = hi._HARNESS_INSTALL[hi.OPENCODE_KEY]
    assert opencode.package == "opencode-ai@~1.18.0"
    pkg, _, pin = opencode.package.rpartition("@")
    assert f"{pkg}@${{version:-{pin}}}" in script

    qwen = hi._HARNESS_INSTALL[hi.QWEN_KEY]
    assert qwen.package == "@qwen-code/qwen-code"
    assert f"{qwen.package}${{version:+@$version}}" in script

    # goose's default pin mirrors the runtime's minimum supported goose.
    assert f"${{1:-{hi._GOOSE_MIN_VERSION}}}" in script
