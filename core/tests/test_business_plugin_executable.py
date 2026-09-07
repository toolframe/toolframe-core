"""Run the exported business app using the packaged executable, on both providers.

Set TOOLFRAME_TEST_EXECUTABLE and TOOLFRAME_TEST_POSTGRES_DSN. Optional browser
interactions use TOOLFRAME_TEST_BROWSER=1 and installed Chrome/Playwright.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from test_database_providers import provider as provider  # noqa: PLC0414


def production_files(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and not {"evals", "__pycache__"}.intersection(path.relative_to(root).parts)
        and path.suffix != ".pyc"
    }


def test_packaged_business_app_and_preserved_skill(provider, tmp_path):
    binary = os.environ.get("TOOLFRAME_TEST_EXECUTABLE")
    if not binary:
        pytest.skip("set TOOLFRAME_TEST_EXECUTABLE to a newly packaged binary")
    repository = Path(__file__).resolve().parents[2]
    executable = tmp_path / "toolframe"
    shutil.copy2(binary, executable)
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TOOLFRAME_") and key not in {"PYTHONPATH", "VIRTUAL_ENV"}
    }
    exported = tmp_path / "exported-skill"
    export = subprocess.run(
        [str(executable), "--offline", "export-skill", str(exported)],
        cwd=tmp_path,
        env=inherited,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert export.returncode == 0, export.stderr.decode()
    source = production_files(repository / "skills/toolframe-business-app")
    assert production_files(exported) == source
    assert not (exported / "evals").exists()
    assert not any(
        (tmp_path / directory).exists()
        for directory in ("app", "domains", ".agents", ".toolframe", "data")
    )
    asset = exported / "assets/business-plugin"
    shutil.copytree(asset / "app", tmp_path / "app")
    # Catalog imports must work when the directory is not literally "domains".
    shutil.copytree(asset / "domains", tmp_path / "capabilities")
    for name in ("APP_CONTEXT.md", "DOMAIN_MODEL.md"):
        shutil.copy2(asset / name, tmp_path / name)
    (tmp_path / "workspace").mkdir()
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    runtime = Path(os.environ.get("TOOLFRAME_TEST_RUNTIME", tmp_path / ".toolframe"))
    common = [
        str(executable),
        "--runtime-dir",
        str(runtime),
        "--uv-path",
        shutil.which("uv"),
        "--domains-dir",
        "capabilities",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--port-search-span",
        "1",
    ]
    environment = {**inherited, "TOOLFRAME_DB_PROVIDER": provider.dialect}
    dsn = None
    if provider.dialect == "sqlite":
        common.extend(["--database", str(provider.settings.sqlite_path)])
    else:
        dsn = provider.settings.postgres_dsn.get_secret_value()
        environment["TOOLFRAME_POSTGRES_DSN"] = dsn

    def command(*args):
        result = subprocess.run(
            [*common, *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=240,
            check=False,
        )
        if dsn:
            assert dsn.encode() not in result.stdout + result.stderr
        if result.returncode:
            (tmp_path / f"{args[0]}.log").write_bytes(result.stdout + result.stderr)
            pytest.fail(f"{args[0]} exited {result.returncode}; inspect fixture log")
        return json.loads(result.stdout)

    checked = command("check", "--format", "json")
    assert checked["ok"] and checked["databaseMutated"] is False
    assert checked["pluginValidation"] == "definitions-only"
    assert {tool["name"] for tool in checked["tools"]} == {
        "stockroom_receive_delivery",
        "stockroom_stock_levels",
    }
    assert "fastapi-cordis==0.2.0" in checked["dependencies"]
    definitions = {item["name"]: item for item in checked["plugins"]}
    assert set(definitions["domain.stockroom"]["requires"]) == {
        "toolframe.database." + provider.dialect,
        "toolframe.artifacts",
        "stockroom.local",
    }
    assert definitions["stockroom.local"]["provides"] == "stockroom.service"
    assert definitions["stockroom.local"]["requires"] == [
        "toolframe.database." + provider.dialect
    ]
    assert not (tmp_path / ".agents").exists()
    payload = {"reference": "CLI-1", "item": "Notebook", "quantity": 3}
    first = command(
        "invoke", "stockroom_receive_delivery", "--json", json.dumps(payload)
    )
    assert not first["result"][1]["replayed"]
    replay = command(
        "invoke", "stockroom_receive_delivery", "--json", json.dumps(payload)
    )
    assert replay["result"][1]["replayed"]
    assert not (tmp_path / ".agents").exists()
    installed = tmp_path / ".agents/skills/toolframe-business-app"
    personal = source["SKILL.md"] + b"\nPersonal workflow: preserve this note.\n"
    expected_quantity = 3

    async def exercise():
        nonlocal expected_quantity
        for restart in range(2):
            with (tmp_path / f"serve-{restart}.log").open("wb") as log:
                child = await asyncio.create_subprocess_exec(
                    *common,
                    "serve",
                    cwd=tmp_path,
                    env=environment,
                    stdout=log,
                    stderr=log,
                )
                url = f"http://127.0.0.1:{port}"
                try:
                    async with httpx.AsyncClient() as client:
                        for _ in range(240):
                            assert child.returncode is None, (
                                "server stopped before readiness"
                            )
                            try:
                                response = await client.get(url + "/api/runtime")
                                if response.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.25)
                        else:
                            pytest.fail("server readiness timed out")
                        info = response.json()
                        assert info["database"]["provider"] == provider.dialect
                        assert info["migrations"]["applied"] == [
                            "stockroom@0000_deliveries"
                        ]
                        if dsn:
                            assert dsn not in response.text
                        if restart == 0:
                            assert production_files(installed) == source
                            (installed / "SKILL.md").write_bytes(personal)
                        else:
                            assert (installed / "SKILL.md").read_bytes() == personal
                        assert (await client.get(url + "/llms.txt")).status_code == 200
                        async with Client(url + "/mcp") as mcp:
                            assert {tool.name for tool in await mcp.list_tools()} == {
                                "stockroom_receive_delivery",
                                "stockroom_stock_levels",
                            }
                            assert (
                                await mcp.call_tool(
                                    "stockroom_receive_delivery", payload
                                )
                            ).data["replayed"]
                            if (
                                restart == 0
                                and os.environ.get("TOOLFRAME_TEST_BROWSER") == "1"
                            ):
                                await receive_in_browser(url, tmp_path)
                                expected_quantity = 5
                            totals = await mcp.call_tool("stockroom_stock_levels", {})
                            assert totals.data["items"] == [
                                {"item": "Notebook", "quantity": expected_quantity}
                            ]
                            assert (
                                await client.post(
                                    url + "/api/plugins/stockroom.local/disable"
                                )
                            ).status_code == 200
                            states = {
                                item["name"]: item
                                for item in (
                                    await client.get(url + "/api/plugins")
                                ).json()["plugins"]
                            }
                            assert states["domain.stockroom"]["state"] == "blocked"
                            assert states["domain.stockroom"]["enabled"]
                            denied = await mcp.call_tool(
                                "stockroom_stock_levels", {}, raise_on_error=False
                            )
                            assert denied.is_error
                            assert (
                                provider.query(
                                    "SELECT SUM(quantity) AS quantity FROM stockroom_deliveries"
                                ).iloc[0, 0]
                                == expected_quantity
                            )
                            assert (
                                await client.post(
                                    url + "/api/plugins/stockroom.local/enable"
                                )
                            ).status_code == 200
                            assert (
                                await mcp.call_tool("stockroom_stock_levels", {})
                            ).data == totals.data
                finally:
                    if child.returncode is None:
                        child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), timeout=20)
                    except TimeoutError:
                        child.kill()
                        await child.wait()

    asyncio.run(exercise())
    fresh = tmp_path / "review-new-skill"
    result = subprocess.run(
        [str(executable), "export-skill", str(fresh)],
        cwd=tmp_path,
        env=inherited,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0 and production_files(fresh) == source
    result = subprocess.run(
        [str(executable), "export-skill", str(installed)],
        cwd=tmp_path,
        env=inherited,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert (installed / "SKILL.md").read_bytes() == personal


async def receive_in_browser(url, directory):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as playwright:
        executable = os.environ.get("TOOLFRAME_TEST_BROWSER_EXECUTABLE")
        options = (
            {"executable_path": executable} if executable else {"channel": "chrome"}
        )
        browser = await playwright.chromium.launch(headless=True, **options)
        page = await browser.new_page()
        try:
            await page.goto(url + "/")
            await expect(
                page.get_by_role("heading", name="Stockroom", exact=True)
            ).to_be_visible(timeout=30000)
            await page.get_by_role(
                "textbox", name="Delivery reference", exact=True
            ).fill("UI-1")
            await page.get_by_role("textbox", name="Item", exact=True).fill("Notebook")
            await page.get_by_role("spinbutton", name="Quantity", exact=True).fill("2")
            receive = page.get_by_role("button", name="Receive delivery", exact=True)
            await receive.click()
            await expect(
                page.get_by_text("Delivery recorded.", exact=True)
            ).to_be_visible()
            await receive.click()
            await expect(
                page.get_by_text("Delivery already recorded.", exact=True)
            ).to_be_visible()
            await page.get_by_role("spinbutton", name="Quantity", exact=True).fill(
                "1001"
            )
            await receive.click()
            await expect(
                page.get_by_text("stockroom_invalid_delivery", exact=False)
            ).to_be_visible()
            await page.screenshot(
                path=str(directory / "stockroom-form.png"), full_page=True
            )
        except BaseException:
            await page.screenshot(
                path=str(directory / "stockroom-failure.png"), full_page=True
            )
            raise
        finally:
            await browser.close()
