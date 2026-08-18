"""Single source of truth for the package version.

Kept in its own module so :mod:`satchecker_client.client` can build its
User-Agent from it without importing the package ``__init__``, which imports
``client`` in turn.
"""

__version__ = "0.1.0"
