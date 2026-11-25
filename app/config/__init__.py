"""Active assay config. Switching loads a different config file."""

from __future__ import annotations

import importlib
from pathlib import Path

_ACTIVE = Path(__file__).with_name("active_name.txt")
_FILES = ("test_config_1", "test_config_2")


def module_name() -> str:
    if _ACTIVE.is_file():
        name = _ACTIVE.read_text(encoding="utf-8").strip()
        if name in _FILES:
            return name
    return "test_config_1"


def current():
    return importlib.import_module(f"app.config.{module_name()}")


def __getattr__(name: str):
    return getattr(current(), name)


def summary() -> dict:
    loaded = current()
    data = loaded.summary()
    data["source"] = f"{module_name()}.py"
    return data


def choices() -> list[dict]:
    loaded = module_name()
    options = []
    for name in _FILES:
        if name == loaded:
            continue
        module = importlib.import_module(f"app.config.{name}")
        options.append(
            {
                "module": name,
                "version": module.CONFIG_VERSION,
                "change": module.CHANGE,
                "source": f"{name}.py",
            }
        )
    return options


def describe(name: str) -> dict:
    if name not in _FILES:
        raise ValueError("Unknown config file.")
    module = importlib.import_module(f"app.config.{name}")
    return {
        "module": name,
        "version": module.CONFIG_VERSION,
        "change": module.CHANGE,
        "source": f"{name}.py",
    }


def activate(name: str) -> dict:
    chosen = describe(name)
    _ACTIVE.write_text(name + "\n", encoding="utf-8")
    return chosen
