"""连接器动作的边界测试:不启动 Docker,只验证参数和固定动作分发。"""
import json
import subprocess
import sys
from types import SimpleNamespace

from app.repair.connector import connector


def test_connector_declares_extended_actions_without_shell():
    assert {x["action"] for x in connector.CAPABILITIES} == set(connector.ACTIONS)
    assert "recreate_service" in connector.WRITE_ACTIONS
    assert "service_logs" in connector.READ_ACTIONS


def test_read_actions_reject_unbounded_parameters():
    assert connector.action_service_logs({"service": "ad", "tail": 201})["status"] == "denied"
    assert connector.action_service_events({"service": "ad", "limit": 0})["status"] == "denied"
    assert connector.action_service_resources({"service": "bad/name"})["status"] == "denied"


def test_write_actions_require_both_server_tokens():
    result = connector.execute({"tool": "recreate_service", "args": {"service": "ad"}})
    assert result["status"] == "denied"
    result = connector.execute({"tool": "recreate_service", "args": {"service": "ad"},
                                 "plan_id": "pl_x"})
    assert result["status"] == "denied"


def test_recreate_rejects_non_compose_container(monkeypatch):
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))
    calls = []

    def fake_docker(args, timeout=10.0):
        calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stdout='{"com.docker.compose.project":"p"}', stderr="")

    monkeypatch.setattr(connector, "_docker", fake_docker)
    result = connector.action_recreate_service({"service": "ad"})
    assert result["status"] == "denied"
    assert len(calls) == 1
    assert calls[0][0][0] == "inspect"


def test_recreate_uses_fixed_compose_arguments(monkeypatch, tmp_path):
    config = tmp_path / "compose.yml"
    config.write_text("services: {}", encoding="utf-8")
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))
    calls = []

    def fake_docker(args, timeout=10.0):
        calls.append((args, timeout))
        if args[0] == "inspect":
            labels = {"com.docker.compose.project": "demo",
                      "com.docker.compose.project.working_dir": str(tmp_path),
                      "com.docker.compose.project.config_files": str(config),
                      "com.docker.compose.service": "web"}
            return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(labels), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(connector, "_docker", fake_docker)
    result = connector.action_recreate_service({"service": "web"})
    assert result["status"] == "success"
    command = calls[1][0]
    assert command[:2] == ["compose", "--project-directory"]
    assert "--force-recreate" in command
    assert "web" == command[-1]
    assert all(isinstance(arg, str) for arg in command)


def test_execute_maps_timeout_to_unknown(monkeypatch):
    def boom(args, timeout=60.0):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))
    monkeypatch.setattr(connector, "_docker", boom)
    result = connector.execute({"tool": "restart_service", "args": {"service": "ad"},
                                "plan_id": "pl_x", "approval_id": "ap_x"})
    assert result["status"] == "unknown"


def test_docker_capped_keeps_tail_with_bounded_memory(monkeypatch):
    src = "import sys\nsys.stdout.write('x' * 300000 + '\\n' + 'tail-line\\n')\n"
    monkeypatch.setattr(connector, "_DOCKER", [sys.executable])
    code, text, dropped = connector._docker_capped(["-c", src], keep_bytes=4096, timeout=10)
    assert code == 0 and dropped is True
    assert text.endswith("tail-line\n") and len(text) <= 4096


def test_docker_capped_timeout_reports_failure(monkeypatch):
    monkeypatch.setattr(connector, "_DOCKER", [sys.executable])
    code, _text, dropped = connector._docker_capped(
        ["-c", "import time; time.sleep(3)"], keep_bytes=1024, timeout=1)
    assert code != 0 and dropped is True


def test_service_logs_slices_tail_and_flags_truncation(monkeypatch):
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))

    def fake_capped(args, keep_bytes, timeout=15.0):
        assert args[0] == "logs" and keep_bytes == 20 * 1024
        return 0, "L" * 30000, True

    monkeypatch.setattr(connector, "_docker_capped", fake_capped)
    r = connector.action_service_logs({"service": "ad", "tail": 100})
    assert r["status"] == "success"
    assert len(r["data"]["logs"]) == 20000 and r["data"]["truncated"] is True

    monkeypatch.setattr(connector, "_docker_capped",
                        lambda a, keep_bytes, timeout=15.0: (1, "boom", False))
    r = connector.action_service_logs({"service": "ad", "tail": 100})
    assert r["status"] == "failed" and "boom" in r["message"]


def test_recreate_rejects_ambiguous_compose_targets(monkeypatch, tmp_path):
    config = tmp_path / "compose.yml"
    config.write_text("services: {}", encoding="utf-8")
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))
    base = {"com.docker.compose.project": "demo",
            "com.docker.compose.project.working_dir": str(tmp_path),
            "com.docker.compose.service": "web"}
    cases = [
        {**base, "com.docker.compose.project.config_files": f"{config},{config}"},
        {**base, "com.docker.compose.project.config_files": str(tmp_path / "nope.yml")},
        {**base, "com.docker.compose.project.config_files": str(config),
         "com.docker.compose.project.working_dir": "relative/dir"},
    ]
    for labels in cases:
        def fake_docker(args, timeout=10.0, _labels=labels):
            assert args[0] == "inspect"
            return SimpleNamespace(returncode=0, stdout=json.dumps(_labels), stderr="")
        monkeypatch.setattr(connector, "_docker", fake_docker)
        result = connector.action_recreate_service({"service": "web"})
        assert result["status"] == "denied", labels


def test_recreate_reports_compose_failure(monkeypatch, tmp_path):
    config = tmp_path / "compose.yml"
    config.write_text("services: {}", encoding="utf-8")
    monkeypatch.setattr(connector, "_docker_ready", lambda: (True, ""))
    labels = {"com.docker.compose.project": "demo",
              "com.docker.compose.project.working_dir": str(tmp_path),
              "com.docker.compose.project.config_files": str(config),
              "com.docker.compose.service": "web"}

    def fake_docker(args, timeout=10.0):
        if args[0] == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps(labels), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="compose boom")

    monkeypatch.setattr(connector, "_docker", fake_docker)
    result = connector.action_recreate_service({"service": "web"})
    assert result["status"] == "failed" and "compose boom" in result["message"]
