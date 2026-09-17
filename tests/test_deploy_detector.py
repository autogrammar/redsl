"""Tests for redsl.execution.deploy_detector script detection."""

import stat
from pathlib import Path

from redsl.execution.deploy_detector import _detect_from_scripts


def _make_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\ntrue\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TestDetectFromScripts:
    def test_detects_push_and_publish_scripts(self, tmp_path) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        _make_exec(scripts / "push.sh")
        _make_exec(scripts / "release.sh")

        push, publish = _detect_from_scripts(tmp_path)

        assert push.method == "script"
        assert push.command == ["bash", str(scripts / "push.sh")]
        assert push.label == "scripts/push.sh"
        assert publish.method == "script"
        assert publish.label == "scripts/release.sh"

    def test_no_scripts_returns_empty_actions(self, tmp_path) -> None:
        push, publish = _detect_from_scripts(tmp_path)
        assert push.method == "none"
        assert publish.method == "none"

    def test_executable_without_sh_extension(self, tmp_path) -> None:
        _make_exec(tmp_path / "git-push")
        push, _ = _detect_from_scripts(tmp_path)
        assert push.method == "script"
        assert push.command == [str(tmp_path / "git-push")]
