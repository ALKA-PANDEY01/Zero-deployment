"""Load and validate the user-facing ZeroDown YAML configuration."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_STAGES = (10, 25, 50, 100)
DEFAULT_MAX_ERROR_RATE = 5.0
DEFAULT_MAX_LATENCY_MS = 1000.0
DEFAULT_MAX_CPU_PERCENT = 90.0
DEFAULT_MAX_MEMORY_PERCENT = 90.0
DEFAULT_MAX_RISK_PERCENT = 65.0


@dataclass(frozen=True)
class AppConfig:
    name: str
    port: int
    build_context: str = "./app"
    dockerfile: str = "Dockerfile"


@dataclass(frozen=True)
class HealthConfig:
    endpoint: str
    smoke_endpoints: tuple[str, ...] = ()
    canary_endpoint: str | None = None


@dataclass(frozen=True)
class RolloutConfig:
    stages: tuple[int, ...] = DEFAULT_STAGES
    stage_seconds: float = 3.0
    observation_seconds: int = 15


@dataclass(frozen=True)
class LimitsConfig:
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE
    max_latency_ms: float = DEFAULT_MAX_LATENCY_MS
    max_cpu_percent: float = DEFAULT_MAX_CPU_PERCENT
    max_memory_percent: float = DEFAULT_MAX_MEMORY_PERCENT
    max_risk_percent: float = DEFAULT_MAX_RISK_PERCENT


@dataclass(frozen=True)
class ZeroDownConfig:
    app: AppConfig
    health: HealthConfig
    rollout: RolloutConfig
    limits: LimitsConfig


def _mapping(
    value: Any,
    name: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a YAML mapping")

    optional_keys = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional_keys
    if missing:
        raise ValueError(f"{name} is missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _endpoint(value: Any, name: str) -> str:
    endpoint = _text(value, name)
    if not endpoint.startswith("/") or endpoint.startswith("//"):
        raise ValueError(f"{name} must start with a single '/'")
    return endpoint


def validate_rollout_stages(stages: Any) -> tuple[int, ...]:
    if (
        not isinstance(stages, (list, tuple))
        or not stages
        or any(
            isinstance(stage, bool)
            or not isinstance(stage, int)
            or not 1 <= stage <= 100
            for stage in stages
        )
        or stages[-1] != 100
        or any(left >= right for left, right in zip(stages, stages[1:]))
    ):
        raise ValueError(
            "rollout.stages must be increasing integer percentages ending in 100"
        )
    return tuple(stages)


def parse_config(raw: Any) -> ZeroDownConfig:
    root = _mapping(raw, "config", {"app", "health"}, {"rollout", "limits"})

    app_data = _mapping(
        root["app"],
        "app",
        {"name", "port"},
        {"build_context", "dockerfile"},
    )
    port = app_data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("app.port must be an integer between 1 and 65535")

    health_data = _mapping(
        root["health"],
        "health",
        {"endpoint"},
        {"smoke_endpoints", "canary_endpoint"},
    )
    smoke_endpoints = health_data.get("smoke_endpoints", [])
    if not isinstance(smoke_endpoints, list):
        raise ValueError("health.smoke_endpoints must be a list of URL paths")
    parsed_smoke_endpoints = tuple(
        _endpoint(endpoint, f"health.smoke_endpoints[{index}]")
        for index, endpoint in enumerate(smoke_endpoints)
    )

    rollout_data = _mapping(
        root.get("rollout", {}),
        "rollout",
        set(),
        {"stages", "stage_seconds", "observation_seconds"},
    )
    stages = validate_rollout_stages(
        rollout_data.get("stages", list(DEFAULT_STAGES))
    )
    stage_seconds = _number(
        rollout_data.get("stage_seconds", 3),
        "rollout.stage_seconds",
    )
    if stage_seconds <= 0:
        raise ValueError("rollout.stage_seconds must be greater than 0")
    observation_seconds = rollout_data.get("observation_seconds", 15)
    if (
        isinstance(observation_seconds, bool)
        or not isinstance(observation_seconds, int)
        or not 1 <= observation_seconds <= 300
    ):
        raise ValueError(
            "rollout.observation_seconds must be an integer between 1 and 300"
        )

    limits_data = _mapping(
        root.get("limits", {}),
        "limits",
        set(),
        {
            "max_error_rate",
            "max_latency_ms",
            "max_cpu_percent",
            "max_memory_percent",
            "max_risk_percent",
        },
    )
    max_error_rate = _number(
        limits_data.get("max_error_rate", DEFAULT_MAX_ERROR_RATE),
        "limits.max_error_rate",
    )
    if not 0 <= max_error_rate <= 100:
        raise ValueError("limits.max_error_rate must be between 0 and 100 percent")

    max_latency_ms = _number(
        limits_data.get("max_latency_ms", DEFAULT_MAX_LATENCY_MS),
        "limits.max_latency_ms",
    )
    if max_latency_ms <= 0:
        raise ValueError("limits.max_latency_ms must be greater than 0")

    resource_limits = {}
    for key in ("max_cpu_percent", "max_memory_percent", "max_risk_percent"):
        value = _number(
            limits_data.get(
                key,
                {
                    "max_cpu_percent": DEFAULT_MAX_CPU_PERCENT,
                    "max_memory_percent": DEFAULT_MAX_MEMORY_PERCENT,
                    "max_risk_percent": DEFAULT_MAX_RISK_PERCENT,
                }[key],
            ),
            f"limits.{key}",
        )
        if not 0 <= value <= 100:
            raise ValueError(f"limits.{key} must be between 0 and 100 percent")
        resource_limits[key] = value

    return ZeroDownConfig(
        app=AppConfig(
            name=_text(app_data["name"], "app.name"),
            port=port,
            build_context=_text(
                app_data.get("build_context", "./app"),
                "app.build_context",
            ),
            dockerfile=_text(
                app_data.get("dockerfile", "Dockerfile"),
                "app.dockerfile",
            ),
        ),
        health=HealthConfig(
            endpoint=_endpoint(health_data["endpoint"], "health.endpoint"),
            smoke_endpoints=parsed_smoke_endpoints,
            canary_endpoint=_endpoint(
                health_data.get(
                    "canary_endpoint",
                    parsed_smoke_endpoints[0]
                    if parsed_smoke_endpoints
                    else health_data["endpoint"],
                ),
                "health.canary_endpoint",
            ),
        ),
        rollout=RolloutConfig(
            stages=stages,
            stage_seconds=stage_seconds,
            observation_seconds=observation_seconds,
        ),
        limits=LimitsConfig(
            max_error_rate=max_error_rate,
            max_latency_ms=max_latency_ms,
            max_cpu_percent=resource_limits["max_cpu_percent"],
            max_memory_percent=resource_limits["max_memory_percent"],
            max_risk_percent=resource_limits["max_risk_percent"],
        ),
    )


def default_config_path() -> Path:
    configured_path = os.getenv("ZERODOWN_CONFIG")
    if configured_path:
        return Path(configured_path)
    return Path(__file__).resolve().parents[2] / "config" / "zerodown.yaml"


def load_config(path: Path | None = None) -> ZeroDownConfig:
    config_path = path or default_config_path()
    try:
        with config_path.open(encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
    except OSError as error:
        raise ValueError(f"Cannot read ZeroDown config at {config_path}: {error}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in ZeroDown config at {config_path}: {error}") from error
    return parse_config(raw)
