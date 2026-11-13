"""Electron 패키징용 백엔드와 하위 프로세스 명령의 회귀 테스트."""

from pathlib import Path
import sys

import desktop_backend
import tutorial_runtime
import tutorial_service


def test_desktop_migration_head_matches_current_schema():
    assert desktop_backend.migration_directory().is_dir()
    assert desktop_backend._migration_head() == "0004"


def test_desktop_bootstrap_keeps_empty_operational_database(app):
    from models import Floor, Setting

    desktop_backend.bootstrap_database(app)
    assert Setting.query.count() == 11
    assert Floor.query.count() == 0


def test_source_process_commands_use_python_scripts(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    server = tutorial_runtime.server_command()
    seed = tutorial_service.demo_seed_command(tmp_path / "tutorial.db")
    assert server == [sys.executable, str(tutorial_runtime.APP_SCRIPT)]
    assert seed[:3] == [sys.executable, str(tutorial_service.DEMO_SCRIPT), "seed"]
    assert seed[-1] == "--tutorial-ready"


def test_frozen_process_commands_reenter_packaged_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    server = tutorial_runtime.server_command()
    seed = tutorial_service.demo_seed_command(tmp_path / "tutorial.db")
    assert server == [sys.executable, "serve"]
    assert seed[:2] == [sys.executable, "seed-demo"]
    assert seed[-1] == "--tutorial-ready"


def test_desktop_parser_accepts_explicit_loopback_server():
    parsed = desktop_backend.build_parser().parse_args(
        ["serve", "--host", "127.0.0.1", "--port", "53123"]
    )
    assert parsed.command == "serve"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 53123


def test_packaged_resource_paths_are_repository_relative():
    assert desktop_backend.resource_root() == Path(desktop_backend.__file__).resolve().parent
