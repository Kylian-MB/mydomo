"""Where the speech service sits in ``compose.yaml``: read from the file, nothing started.

(The tests of ``tests/stack`` that resolve the file need Docker and carry the ``stack`` marker;
these only read it, so they run everywhere.)
"""

import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def networks_of(service: dict[str, Any]) -> set[str]:
    declared = service.get("networks", [])
    return set(declared)  # a list of names, or a mapping of names: both iterate over the names


def test_speech_is_reachable_by_the_gatekeeper_alone():
    services = load("compose.yaml")["services"]
    speech = networks_of(services["speech"])
    assert speech.isdisjoint(networks_of(services["core"]))
    assert "core_net" not in speech
    assert "lan" not in speech
    assert "speech_net" in speech
    assert "speech_net" in networks_of(services["gatekeeper"])
    on_speech_net = {name for name, item in services.items() if "speech_net" in networks_of(item)}
    assert on_speech_net == {"speech", "gatekeeper"}
    # The network the two share is closed: no way out of it, and no address on the host.
    assert load("compose.yaml")["networks"]["speech_net"]["internal"] is True
    # The only network the two share. Its way out, to fetch its models (ruling R14), is a
    # network of its own: never the gatekeeper's egress, where its listeners are.
    gatekeeper = networks_of(services["gatekeeper"])
    assert speech & gatekeeper == {"speech_net"}
    assert speech - {"speech_net"} == {"speech_egress"}
    assert load("compose.yaml")["networks"]["speech_egress"].get("internal") is not True
    others = {name for name, item in services.items() if "speech_egress" in networks_of(item)}
    assert others == {"speech"}


def test_speech_is_on_no_network_of_any_other_service_but_the_gatekeepers_speech_net():
    services = load("compose.yaml")["services"]
    mine = networks_of(services["speech"]) - {"speech_net"}
    for name, item in services.items():
        if name != "speech":
            assert mine.isdisjoint(networks_of(item)), name
    for name in ("core", "db", "sandbox", "edge"):
        assert "speech_net" not in networks_of(services[name]), name


def test_the_speech_network_is_one_the_gatekeeper_counts_as_its_own():
    """A program granted the house reach must not reach the speech service: its network has a
    fixed subnet, and the gatekeeper is told it is one of the assistant's own."""
    config = load("compose.yaml")
    [ipam] = config["networks"]["speech_net"]["ipam"]["config"]
    own = config["services"]["gatekeeper"]["environment"]["DOMO_OWN_NETWORKS"]
    assert ipam["subnet"] in own.split(",")
    assert "DOMO_SPEECH_NET_PREFIX" in ipam["subnet"]
    # Its way out too: an address on a dynamic, unlisted private subnet would be "house" to the
    # zone logic, so the project does not rely on the bridge's isolation alone.
    [out] = config["networks"]["speech_egress"]["ipam"]["config"]
    assert out["subnet"] in own.split(",")
    assert "DOMO_SPEECH_EGRESS_NET_PREFIX" in out["subnet"]
    assert out["subnet"] != ipam["subnet"]


def test_speech_publishes_no_port():
    services = load("compose.yaml")["services"]
    assert "ports" not in services["speech"]
    # Whatever the edge publishes is its own; and no service reaches the speech service by
    # sharing its network namespace.
    for item in services.values():
        assert not str(item.get("network_mode", "")).endswith("speech")


def test_speech_writes_only_in_its_models_volume():
    speech = load("compose.yaml")["services"]["speech"]
    assert speech["read_only"] is True
    assert speech["tmpfs"] == ["/tmp"]
    assert speech["volumes"] == ["speech-models:/models"]
    assert "speech-models" in load("compose.yaml")["volumes"]
    assert speech["environment"]["DOMO_SPEECH_MODEL_DIR"] == "/models"
    assert speech["cap_drop"] == ["ALL"]
    assert "cap_add" not in speech
    assert "no-new-privileges:true" in speech["security_opt"]
    assert speech["user"] == "10001:10001"


def test_speech_listens_on_every_address_of_its_container_and_gets_time_to_prepare():
    speech = load("compose.yaml")["services"]["speech"]
    assert speech["environment"]["DOMO_SPEECH_HOST"] == "0.0.0.0"
    assert speech["healthcheck"]["start_period"] == "600s"
    # One copy of the check: a script shipped in the image, which compose and the Dockerfile run.
    assert speech["healthcheck"]["test"] == ["CMD", "python", "/app/healthcheck.py"]
    dockerfile = (ROOT / "packages/domo-speech/Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile and "/app/healthcheck.py" in dockerfile
    assert "COPY packages/domo-speech/healthcheck.py /app/healthcheck.py" in dockerfile


def test_nobody_waits_for_the_speech_service_to_start():
    services = load("compose.yaml")["services"]
    for name, item in services.items():
        assert "speech" not in item.get("depends_on", {}), name


def test_the_graphics_card_is_an_override_of_the_speech_service_alone():
    override = load("compose.gpu.yaml")
    assert set(override["services"]) == {"speech"}
    (device,) = override["services"]["speech"]["deploy"]["resources"]["reservations"]["devices"]
    assert device["driver"] == "nvidia"
    assert device["capabilities"] == ["gpu"]


def test_the_graphics_card_libraries_are_an_extra_the_lock_file_covers():
    project = tomllib.loads((ROOT / "packages/domo-speech/pyproject.toml").read_text("utf-8"))
    gpu = project["project"]["optional-dependencies"]["gpu"]
    assert any(name.startswith("nvidia-cudnn-cu12") for name in gpu)
    assert any(name.startswith("nvidia-cublas-cu12") for name in gpu)
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'provides-extras = ["models", "gpu"]' in lock
    dockerfile = (ROOT / "packages/domo-speech/Dockerfile").read_text(encoding="utf-8")
    assert "uv pip install" not in dockerfile
    assert "--frozen" in dockerfile
    assert "--extra gpu" in " ".join(
        load("compose.gpu.yaml")["services"]["speech"]["build"]["args"].values()
    )


def test_the_image_carries_the_notice_and_the_checks_dependency_is_declared():
    dockerfile = (ROOT / "packages/domo-speech/Dockerfile").read_text(encoding="utf-8")
    assert "packages/domo-speech/NOTICE" in dockerfile
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(item.lower().startswith("pyyaml") for item in root["dependency-groups"]["dev"])


def test_the_dockerfile_is_made_of_real_instructions():
    """Each continuation is a real newline (a literal backslash-n once made the HEALTHCHECK
    unreadable and the build fail), and the HEALTHCHECK parses as options then a CMD."""
    text = (ROOT / "packages/domo-speech/Dockerfile").read_text(encoding="utf-8")
    assert "\\n" not in text
    instructions: list[str] = []
    current = ""
    for line in text.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.rstrip().endswith("\\"):
            current += line.rstrip()[:-1] + " "
            continue
        instructions.append((current + line).strip())
        current = ""
    assert current == ""
    keywords = {line.split()[0].upper() for line in instructions}
    assert keywords <= {
        "FROM",
        "COPY",
        "ENV",
        "ARG",
        "WORKDIR",
        "RUN",
        "USER",
        "EXPOSE",
        "HEALTHCHECK",
        "CMD",
    }
    (health,) = [line for line in instructions if line.startswith("HEALTHCHECK")]
    options, _, command = health.removeprefix("HEALTHCHECK").partition("CMD")
    assert all(word.startswith("--") and "=" in word for word in options.split())
    assert command.strip() == '["python", "/app/healthcheck.py"]'


# Every setting of the speech service an owner may write in .env reaches it (final fix wave B2).
SPEECH_SETTINGS = {
    "DOMO_SPEECH_DEVICE": "${DOMO_SPEECH_DEVICE:-auto}",
    "DOMO_SPEECH_RESTRICTED_MODELS": "${DOMO_SPEECH_RESTRICTED_MODELS:-yes}",
    "DOMO_SPEECH_AT_ONCE": "${DOMO_SPEECH_AT_ONCE:-}",
    "DOMO_SPEECH_VOICE_PROGRAM": "${DOMO_SPEECH_VOICE_PROGRAM:-}",
}


def test_the_owners_speech_settings_reach_the_speech_service():
    environment = load("compose.yaml")["services"]["speech"]["environment"]
    assert {name: environment.get(name) for name in SPEECH_SETTINGS} == SPEECH_SETTINGS


def test_every_speech_setting_is_in_the_example_settings():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in SPEECH_SETTINGS:
        assert f"# {name}=" in example, name
