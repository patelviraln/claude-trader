from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.strategies.base import Strategy

_registry: dict[str, type] = {}


def register(name: str):
    """Decorator to register a strategy class by name."""
    def decorator(cls):
        _registry[name] = cls
        return cls
    return decorator


def get(name: str) -> type:
    if name not in _registry:
        raise KeyError(f"Strategy '{name}' not found. Available: {list(_registry.keys())}")
    return _registry[name]


def list_strategies() -> list[str]:
    return list(_registry.keys())
