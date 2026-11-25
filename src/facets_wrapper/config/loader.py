"""Load project configs (YAML) from disk, validating structure and semantics."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import ProjectConfig
from .validate import validate_project

CONFIG_NAMES = ("mapping.yml", "mapping.yaml")


class ConfigError(Exception):
    def __init__(self, errors: list[str], where: str = ""):
        self.errors = errors
        self.where = where
        head = f"invalid config {where}".strip()
        super().__init__(head + ":\n  - " + "\n  - ".join(errors))


def read_document(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_config(doc: Any, where: str = "") -> ProjectConfig:
    if not isinstance(doc, dict):
        raise ConfigError(["top level must be a mapping"], where)
    try:
        cfg = ProjectConfig.model_validate(doc)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError(msgs, where) from None
    problems = validate_project(cfg)
    if problems:
        raise ConfigError(problems, where)
    return cfg


def load_project_file(path: str | Path) -> ProjectConfig:
    p = Path(path)
    return parse_config(read_document(p), str(p))


def find_config_files(config_dir: str | Path) -> list[Path]:
    root = Path(config_dir)
    found: list[Path] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if child.is_dir():
            for name in CONFIG_NAMES:
                if (child / name).is_file():
                    found.append(child / name)
                    break
    return found


def load_config_dir(config_dir: str | Path) -> dict[str, ProjectConfig]:
    """Load every ``<config_dir>/<project>/mapping.(yml|yaml)``. Any invalid file fails all."""
    projects: dict[str, ProjectConfig] = {}
    errors: list[str] = []
    for f in find_config_files(config_dir):
        try:
            cfg = load_project_file(f)
        except ConfigError as e:
            errors.extend(f"{f}: {m}" for m in e.errors)
            continue
        if cfg.project in projects:
            errors.append(f"{f}: duplicate project name {cfg.project!r}")
            continue
        projects[cfg.project] = cfg
    if errors:
        raise ConfigError(errors, str(config_dir))
    return projects


def load_plugins(modules: str | None) -> list[str]:
    """Import plugin modules (comma separated) so they can register custom transforms."""
    loaded: list[str] = []
    for name in [m.strip() for m in (modules or "").split(",") if m.strip()]:
        importlib.import_module(name)
        loaded.append(name)
    return loaded
