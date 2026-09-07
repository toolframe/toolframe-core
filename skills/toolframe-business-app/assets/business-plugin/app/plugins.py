"""Assemble business services; package installation and discovery happen at startup."""

from domains.stockroom.model.plugin import PLUGIN

PLUGINS = [PLUGIN]
DOMAIN_PLUGINS = {"stockroom": (PLUGIN.id,)}
# The local domain's schema.json is discovered by the host. A packaged service
# without a local domain manifest would instead declare SCHEMAS = {PLUGIN.id: schema}.
SCHEMAS = {}
