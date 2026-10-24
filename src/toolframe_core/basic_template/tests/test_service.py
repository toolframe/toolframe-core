import asyncio
from types import SimpleNamespace

from toolframe_basic.plugin import plugin
from toolframe_basic.service import Echo
from toolframe_sdk import validate


def test_echo_contract_and_behavior():
    method = plugin.services["basic.echo"].methods["echo"]
    params = {"message": "Hello"}
    validate(method.input_schema, params)
    result = asyncio.run(
        Echo(SimpleNamespace(log=None)).invoke("basic.echo", "echo", params)
    )
    validate(method.output_schema, result)
    assert result == params
