"""Retain the provider of an owned SQL store across configuration and restarts."""

from toolframe_sdk import PluginError


class RetainedStores:
    def __init__(self, state, bindings):
        self.state = state
        self.bindings = bindings

    @classmethod
    async def load(cls, state):
        bindings = await state.meta("store_bindings")
        if bindings is None:
            # Earlier hosts allowed one active storage provider. Adopt the last
            # recorded composition, never the newly edited configuration file.
            previous = await state.meta("last_valid_composition", {})
            preferences = previous.get("bindings", {})
            definitions = {}
            for row in await state.plugins():
                if row["current_candidate"]:
                    candidate = await state.candidate(row["current_candidate"])
                    definitions[row["id"]] = candidate["definition"]
            providers = {
                name
                for name, definition in definitions.items()
                if definition.get("provider_kind") == "sql"
            }
            active = {
                name
                for name in providers
                if previous.get("plugins", {}).get(name, {}).get("enabled")
            }
            bindings = {}
            for owner, definition in definitions.items():
                if not definition.get("store"):
                    continue
                choices = {
                    preferences.get(f"{owner}/{alias}")
                    or preferences.get("toolframe.sql")
                    for alias, requirement in definition.get("requires", {}).items()
                    if requirement["service"] == "toolframe.sql"
                } - {None}
                choices = choices or active or providers
                if choices:
                    bindings[owner] = next(iter(choices)) if len(choices) == 1 else None
            await state.set_meta("store_bindings", bindings)
        return cls(state, bindings)

    def check(self, owner, provider):
        if owner in self.bindings and self.bindings[owner] != provider:
            raise PluginError(
                "store_rebind_required",
                "Retained stores cannot change provider through bindings; restore "
                "the original binding. Cross-provider data migration is not supported.",
                {
                    "owner": owner,
                    "retained_provider": self.bindings[owner],
                    "requested_provider": provider,
                },
            )

    async def remember(self, owner, provider):
        self.check(owner, provider)
        if owner not in self.bindings:
            updated = {**self.bindings, owner: provider}
            await self.state.set_meta("store_bindings", updated)
            self.bindings = updated

    async def forget_empty(self, owner):
        updated = {key: value for key, value in self.bindings.items() if key != owner}
        await self.state.set_meta("store_bindings", updated)
        self.bindings = updated

    def describe(self, graph):
        owners = set(self.bindings) | {
            name for name, definition in graph.definitions.items() if definition.store
        }
        return {
            owner: {
                "provider": graph.stores.get(owner),
                "retained_provider": self.bindings.get(owner),
                "rebind_required": owner in self.bindings
                and graph.stores.get(owner) is not None
                and self.bindings[owner] != graph.stores[owner],
            }
            for owner in sorted(owners)
        }
