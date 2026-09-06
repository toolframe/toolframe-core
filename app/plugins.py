"""Optional plugin catalog. Loaded once per process, before serving requests.

Add Python package dependencies to app/pyproject.toml and restart after source
changes. Define Plugin objects here or import them from installed packages.
See docs/plugins.md for setup, cleanup, configuration and service access.
"""

PLUGINS = []

# Domains retain these services while their tools/widgets execute. Example:
# DOMAIN_PLUGINS = {"entry_register": ("my_service",)}
DOMAIN_PLUGINS = {}

# Optional mapping of installed plugin names to toolframe_migrations.Schema.
# Schema targets prepare before dependent setup; stopping a plugin retains data.
SCHEMAS = {}
