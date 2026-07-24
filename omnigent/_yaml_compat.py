"""libyaml-backed YAML loading, with a pure-Python fallback.

PyYAML ships two implementations of the same safe loader: the pure-Python
``SafeLoader`` and ``CSafeLoader``, which wraps libyaml. Both accept the
same grammar, share the same constructor and resolver classes, and build
identical Python objects — the C one is just far faster. Parsing the 19.2 KB
``examples/polly/config.yaml`` takes 3.14 ms pure-Python and 0.18 ms through
libyaml.

Spec and config parsing sits on the runner's hot path (every sub-agent
fan-out re-reads the bundle), so those loaders build on the C parser when
it is available. PyYAML wheels bundle libyaml, but a source install built
without the libyaml headers exposes no ``CSafeLoader`` at all — hence the
fallback.
"""

from __future__ import annotations

from typing import IO, TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    # ``CSafeLoader`` is not a subclass of ``SafeLoader`` — the two are
    # siblings sharing ``SafeConstructor`` and ``Resolver``. mypy also
    # cannot subclass a value chosen at runtime, so the pure-Python
    # loader stands in as the static base for both.
    SafeLoaderBase = yaml.SafeLoader
else:
    SafeLoaderBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# True when the libyaml-backed parser is in use. Exposed for tests that
# need to report which base they exercised.
USING_LIBYAML = SafeLoaderBase is not yaml.SafeLoader


# YAML documents are open-ended trees whose shape is only known after the
# caller's isinstance checks, so the return type is the same ``Any`` that
# ``yaml.safe_load`` itself returns.
def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:  # type: ignore[explicit-any]
    """Drop-in ``yaml.safe_load`` that prefers the libyaml parser.

    Resolver and constructor behaviour match ``yaml.safe_load`` exactly,
    including YAML 1.1 booleans (``on``/``off``/``yes``/``no``). Loaders
    that need YAML 1.2 boolean semantics subclass :data:`SafeLoaderBase`
    and narrow the bool resolver themselves.

    :param stream: YAML text, bytes, or an open file object.
    :returns: The parsed document, or ``None`` for an empty stream.
    """
    return yaml.load(stream, Loader=SafeLoaderBase)
