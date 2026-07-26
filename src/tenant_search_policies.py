from gainr_search_policy import GainrSearchPolicy
from search_policy import DEFAULT_SEARCH_POLICY, SearchPolicy


_POLICY_FACTORIES = {
    "default": lambda: DEFAULT_SEARCH_POLICY,
    "gainr": GainrSearchPolicy,
}


def supported_search_policies() -> tuple[str, ...]:
    return tuple(sorted(_POLICY_FACTORIES))


def build_search_policy(name: str) -> SearchPolicy:
    try:
        factory = _POLICY_FACTORIES[name]
    except KeyError as exc:
        supported = ", ".join(supported_search_policies())
        raise ValueError(
            f"Unsupported search policy {name!r}; expected one of: {supported}"
        ) from exc
    return factory()
