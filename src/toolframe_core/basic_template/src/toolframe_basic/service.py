"""Replace this echo operation with the plugin's business behavior."""

from toolframe_sdk import Plugin


class Echo(Plugin):
    async def invoke(self, service, method, params):
        if method == "echo":
            return {"message": params["message"]}
        return await super().invoke(service, method, params)
