"""Persistent Cordis host; plugin implementation code only runs in workers."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import os
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from fastapi_cordis import Plugin as CordisPlugin
from fastapi_cordis import PluginManager
from toolframe_sdk import PluginError, Principal, digest
from toolframe_sdk.logging import LoggingSession
from toolframe_sdk.tracing import configure_tracing
from toolframe_sdk.web import WebAppCatalog

from .access import Access, operation_scopes
from .config import ConfigFile, HostLock, PluginRequest, atomic_write, management_token
from .diagnostics import dependency_snapshot
from .gateway import compile_gateway, endpoints, validate_web_catalogs
from .graph import Graph
from .lockfile import write_lock
from .mcp_access import McpAccess
from .network import bind_host
from .packages import Packages, floating_index_source, index_source, source_files
from .process import WorkerProcess, group_exists, process_inventory, stop_group
from .redaction import redact, secret_values
from .selection import prepare_selection
from .state import CoreState, StateSettings
from .stores import RetainedStores
from .web_access import WebAccess


@dataclass
class Runtime:
    name: str
    generation: str
    candidate: dict
    worker: WorkerProcess | None = None
    health: dict = field(default_factory=dict)
    observed: float = 0
    admitted: bool = False
    epoch: str | None = None
    _logged_state: str | None = field(default=None, repr=False)


@dataclass
class Root:
    id: str
    owner: str
    deadline: float
    runtimes: dict
    leases: ExitStack
    paths: dict = field(default_factory=dict)
    read_only: bool = False
    principal: Principal | None = None
    scope_catalog: dict = field(default_factory=dict)
    bindings: dict = field(default_factory=dict)


class Host:
    @classmethod
    async def create(
        cls, home, *, scratch=False, listen_host=None, state_settings=None
    ):
        host = cls()
        try:
            await host._initialize(
                home,
                scratch=scratch,
                listen_host=listen_host,
                state_settings=state_settings,
            )
            return host
        except BaseException:
            if getattr(host, "logging", None):
                host.logging.close()
            if getattr(host, "tracing", None):
                await asyncio.to_thread(host.tracing.shutdown)
            if getattr(host, "state", None):
                await host.state.close()
            if getattr(host, "lock", None):
                host.lock.close()
            raise

    async def _initialize(
        self, home, *, scratch=False, listen_host=None, state_settings=None
    ):
        self.home = Path(home).resolve()
        self.config = ConfigFile(self.home)
        self.lock = HostLock(self.home)
        self.lock.acquire()
        self.state = await CoreState.open(self.home, settings=state_settings)
        self.journal_error = None
        self.configuration_error = None
        try:
            self.composition, self.revision = self.config.read()
        except PluginError as error:
            from .config import Application, Composition

            self.configuration_error = error.as_dict()
            previous = await self.state.meta("last_valid_composition")
            self.composition = (
                Composition.model_validate(previous)
                if previous
                else Composition(application=Application(id="unconfigured"))
            )
            self.revision = self.config.revision
        self.initial_application = self.composition.application.model_dump()
        self.stores = await RetainedStores.load(self.state)
        self.listen_host = bind_host(
            listen_host
            if listen_host is not None
            else self.composition.application.host
        )
        self.host_run = uuid.uuid4().hex
        self.tracing = (
            None
            if scratch
            else configure_tracing(
                "toolframe.core",
                {
                    "service.namespace": self.composition.application.id,
                    "service.instance.id": self.host_run,
                    "toolframe.application.id": self.composition.application.id,
                },
            )
        )
        # A private logger keeps multiple embedded hosts and their exporters isolated.
        self.logging = LoggingSession(
            logging.Logger("toolframe.core", logging.DEBUG),  # noqa: LOG001 - host-owned, never registered globally
            {
                "toolframe.application.id": self.composition.application.id,
                "service.instance.id": self.host_run,
            },
            tracing=self.tracing,
        )
        self.log = self.logging.log
        if (
            self.state.settings.backend == "postgresql"
            and self.composition.application.id != self.state.settings.application_id
        ):
            raise PluginError(
                "core_state_identity_mismatch",
                "Composition and Core database identities differ",
            )
        self.token = management_token(self.home)
        self.packages = Packages(self.home, self.composition.packages)
        self._package_overrides()
        self.manager = PluginManager(
            drain_timeout=self.composition.runtime.drain_timeout
        )
        self.graph = Graph({}, self.composition.bindings)
        self.runtimes: dict[str, Runtime] = {}
        self.registered = set()
        self.gated = set()
        self.roots: dict[str, Root] = {}
        self.changed = asyncio.Event()
        self.mutation = asyncio.Lock()
        self.operations = {}
        self.cancellations = set()
        self.tasks = []
        self.closing = False
        self._closed = False
        self.scratch = scratch
        self.epoch = None
        self.publication = {}
        self.health_publication_pending = False
        self.gateway_sessions = {}
        self.startup_error = None
        self.schema_changed = False
        self.schema_events = []
        self.optional_failures = {}
        self.optional_new = set()
        self.orphans = {}
        self.failed_roots = set()
        self.cleanup_blocked = set()
        self.worker_owners = await self.state.meta("worker_owners", {})
        self.starting = True
        self.transitioning = set()
        self.access = Access(self)
        self.mcp_access = McpAccess(self)
        self.web_access = WebAccess(self)
        self.gateway = compile_gateway(self.composition)
        self.operation_identities = {}

    async def start(self, *, apply=True, wait=True):
        previous_node = await self.state.meta("host_node")
        node = self.state.settings.node_id
        if node and previous_node and previous_node != node and self.worker_owners:
            raise PluginError(
                "core_state_recovery_required",
                "Worker history belongs to another node; verify cleanup before recovery",
            )
        await self.reconcile_workers()
        (await self.state.reconcile_startup(self.host_run))
        (await self.state.set_meta("host_run", self.host_run))
        await self.state.set_meta("host_node", node)
        await self.manager.start()
        atomic_write(
            self.home / ".toolframe/endpoint.json",
            json.dumps(
                {
                    "host": self.listen_host,
                    "port": self.composition.application.port,
                }
            ),
        )
        if apply:
            operation = await self.submit("apply", {"startup": True})
            if wait:
                await self.wait_operation(operation["id"])
        else:
            self.starting = False
        if not self.scratch:
            self.tasks = [
                asyncio.create_task(self._health_loop()),
                asyncio.create_task(self._watch_loop()),
                asyncio.create_task(self._exit_loop()),
            ]
            if self.state.settings.backend == "postgresql":
                self.tasks.append(asyncio.create_task(self._journal_loop()))

    async def _journal_loop(self):
        while not self.closing:
            await asyncio.sleep(0.5)
            try:
                await self.state.ping()
            except PluginError as error:
                self.journal_error = error.as_dict()
                await self._stop_after_journal_loss()
                return

    async def _stop_after_journal_loss(self):
        self.closing = True
        self.changed.set()
        self.gated.update(self.runtimes)
        current = asyncio.current_task()
        pending = [
            task
            for task in [*self.tasks, *self.operations.values()]
            if task is not current
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for sessions in self.gateway_sessions.values():
            for task in list(sessions):
                task.cancel()
        # EOF cleanup may finish gracefully; failures remain visible and leave the
        # durable ownership marker dirty. There is never an automatic takeover.
        for runtime in list(self.runtimes.values()):
            runtime.admitted = False
            if runtime.worker:
                try:
                    await runtime.worker.close(
                        timeout=self.composition.runtime.disposal_timeout,
                        allow_exited=True,
                    )
                except BaseException as error:
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    self.orphans[runtime.name] = {
                        "pid": runtime.worker.pid,
                        "reason": "journal_loss_cleanup_unproven",
                    }
        try:
            await self.manager.close()
        except Exception:  # noqa: BLE001 - preserve a failed cleanup for explicit recovery
            # Cleanup failures keep the durable marker dirty for operator recovery.
            self.journal_error = {**(self.journal_error or {}), "cleanup": "unproven"}

    async def reconcile_workers(self):
        previous_run = await self.state.meta("host_run")
        inventory = await process_inventory()
        # Also find the narrow spawn-before-journal window using the previous run's
        # token, which is durable before any worker is launched.
        if previous_run:
            for pid, item in inventory.items():
                marker = f"--host-token {previous_run}/"
                if (
                    "toolframe_sdk.worker" in item["command"]
                    and marker in item["command"]
                ):
                    token = item["command"].split("--host-token ", 1)[1].split()[0]
                    self.worker_owners[str(pid)] = {
                        "name": token.split("/")[1],
                        "token": token,
                    }
        if any(group_exists(int(pid)) for pid in self.worker_owners):
            deadline = (
                asyncio.get_running_loop().time()
                + self.composition.runtime.disposal_timeout
            )
            while (
                any(group_exists(int(pid)) for pid in self.worker_owners)
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(
                    0.1
                )  # EOF cleanup is cooperative, never an implicit kill.
        for pid, owner in list(self.worker_owners.items()):
            if group_exists(int(pid)):
                self.orphans[owner["name"]] = {"pid": int(pid), **owner}
            else:
                self.worker_owners.pop(pid)
        (await self.state.set_meta("worker_owners", self.worker_owners))

    async def stop_orphan(self, name):
        orphan = self.orphans[name]
        current = (await process_inventory()).get(orphan["pid"])
        if group_exists(orphan["pid"]):
            if (
                not current
                or f"--host-token {orphan['token']}" not in current["command"]
            ):
                raise PluginError(
                    "ownership_unproven",
                    "Surviving process ownership cannot be verified; resolve it before retrying",
                    orphan,
                )
            await stop_group(orphan["pid"])
        self.orphans.pop(name)
        self.worker_owners.pop(str(orphan["pid"]), None)
        (await self.state.set_meta("worker_owners", self.worker_owners))
        (await self.state.stopped(name, True))

    async def close(self):
        if self._closed:
            return
        self.closing = True
        self.changed.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.state.failed:
            await self._stop_after_journal_loss()
        else:
            for operation in self.operations.values():
                if not operation.done():
                    await asyncio.shield(operation)
        self.gated.update(self.runtimes)
        clean = False
        try:
            if not self.state.failed:
                await self._drain(
                    set(self.runtimes), self.composition.runtime.drain_timeout
                )
                await self.manager.close()
                clean = not self.orphans and not self.worker_owners
        finally:
            self.lock.close()
            await self.state.close(clean=clean)
            self._closed = True
            self.logging.close()
            if self.tracing:
                await asyncio.to_thread(self.tracing.shutdown)

    async def submit(self, action, params, *, idempotency=None, identity=None):
        if self.state.failed:
            raise PluginError("core_state_unavailable", "Core journal is unavailable")
        if self.closing:
            raise PluginError("host_closing", "Core is shutting down")
        actions = {
            "add",
            "validate",
            "update",
            "reload",
            "enable",
            "disable",
            "remove",
            "stop",
            "apply",
            "plan",
            "migrate-plan",
            "migrate-apply",
        }
        if (
            not isinstance(action, str)
            or action not in actions
            or not isinstance(params, dict)
        ):
            raise PluginError(
                "invalid_request", "Supply a known action and object parameters"
            )
        for flag in ("force", "runtime", "enable", "watch", "startup"):
            if flag in params and not isinstance(params[flag], bool):
                raise PluginError("invalid_request", f"{flag} must be boolean")
        if action not in {"add", "apply", "plan"} and not isinstance(
            params.get("id"), str
        ):
            raise PluginError("invalid_request", "This action requires a plugin ID")
        if action == "add" and not isinstance(params.get("source"), str):
            raise PluginError("invalid_request", "Add requires a package source")
        # A retry refers to the original operation, whose own successful write
        # may already have changed the desired revision.
        existing = await self.state.idempotent_operation(
            action, params.get("id"), params, idempotency
        )
        if existing is not None:
            return existing
        if (
            params.get("expected_revision")
            and params["expected_revision"] != self.config.revision
        ):
            raise PluginError(
                "stale_revision", "Composition changed; refresh before submitting"
            )
        operation, created = await self.state.new_operation(
            action, params.get("id"), params, idempotency
        )
        if created:
            if identity is not None:
                self.operation_identities[operation["id"]] = identity
            self.operations[operation["id"]] = asyncio.create_task(
                self._operate(operation)
            )
        return operation

    async def wait_operation(self, identifier):
        task = self.operations.get(identifier)
        if task:
            await asyncio.shield(task)
        return await self.state.operation(identifier)

    async def phase(self, operation, phase, result=None):
        (await self.state.update_operation(operation, phase=phase, result=result))

    def check_cancel(self, operation):
        if operation in self.cancellations:
            raise PluginError("cancelled", "Operation was cancelled before disposal")

    async def _operate(self, operation):
        identifier = operation["id"]
        async with self.mutation:
            (
                await self.state.update_operation(
                    identifier, status="running", phase="planning"
                )
            )
            try:
                self.check_cancel(identifier)
                if identity := self.operation_identities.get(identifier):
                    await self.access.admit(
                        identity,
                        operation_scopes(operation["kind"], operation["request"]),
                        activity=True,
                        effective=False,
                    )
                result = await self._dispatch(
                    operation["kind"], operation["request"], identifier
                )
                (
                    await self.state.update_operation(
                        identifier, status="succeeded", phase="complete", result=result
                    )
                )
            except Exception as error:  # noqa: BLE001 - durable boundary, keep management alive
                detail = (
                    error.as_dict()
                    if isinstance(error, PluginError)
                    else {
                        "code": "operation_failed",
                        "message": str(error),
                        "details": {},
                    }
                )
                status = (
                    "failed_recovered"
                    if detail["code"] == "failed_recovered"
                    else "cancelled"
                    if detail["code"] == "cancelled"
                    else "failed"
                )
                (
                    await self.state.update_operation(
                        identifier, status=status, error=self.redact(detail)
                    )
                )
            finally:
                self.cancellations.discard(identifier)
                self.operation_identities.pop(identifier, None)
                if operation["request"].get("startup"):
                    self.starting = False

    async def refresh(self):
        composition, revision = self.config.read()
        if composition.application.model_dump() != self.initial_application:
            raise PluginError(
                "restart_required",
                "Application identity/listen configuration changed; restart core explicitly",
            )
        self.composition, self.revision = composition, revision
        self.configuration_error = None
        self.packages.settings = composition.packages
        self._package_overrides()
        self.manager.drain_timeout = composition.runtime.drain_timeout
        (await self.state.set_meta("last_valid_composition", composition.model_dump()))

    def _package_overrides(self):
        wheelhouse = os.environ.get("TOOLFRAME_WHEELHOUSE")
        if wheelhouse and wheelhouse not in self.packages.settings.find_links:
            self.packages.settings.find_links.append(wheelhouse)
        if os.environ.get("TOOLFRAME_OFFLINE") == "1":
            self.packages.settings.offline = True

    async def _prepare(self, name, operation, *, installed=False):
        (await self.phase(operation, "capture_validate", {"plugin_id": name}))
        request = self.composition.plugins[name]
        stored = await self.state.plugin(name)
        previous = stored["last_candidate"] or stored["current_candidate"]
        retained = (await self.state.candidate(previous)) if previous else None
        if (
            not retained
            or retained["source"]["request"] != request.source
            or not (installed or index_source(request.source))
        ):
            retained = None
        candidate = await self.packages.prepare(
            name, request.source, request.config, retained=retained
        )
        if self.config.revision != self.revision:
            raise PluginError(
                "stale_plan", "Composition changed while preparing the candidate"
            )
        (await self.save_candidate(candidate))
        return candidate

    async def save_candidate(self, candidate):
        # Cohort locks enrich an immutable artifact identity after activation.
        # Restaging the same wheel must not erase the locks needed for recovery.
        try:
            retained = await self.state.candidate(candidate["id"])
        except PluginError as error:
            if error.code != "candidate_missing":
                raise
            retained = None
        if retained:
            for profile in ("ui", "web"):
                for suffix in ("environment", "cohort"):
                    field = f"{profile}_{suffix}"
                    if field not in candidate and field in retained:
                        candidate[field] = retained[field]
        await self.state.save_candidate(candidate)

    async def selected(self):
        selected = {}
        for name, request in self.composition.plugins.items():
            if request.enabled and not (await self.state.plugin(name))["stopped"]:
                stored = await self.state.plugin(name)
                candidate = stored["current_candidate"] or stored["last_candidate"]
                if candidate:
                    selected[name] = await self.state.candidate(candidate)
        return selected

    async def _dispatch(self, action, params, operation):
        (await self.refresh())
        name = params.get("id")
        if params.get("startup") and (await self.state.meta("forward_repair")):
            raise PluginError(
                "forward_repair_required",
                "Interrupted schema operation requires explicit inspection and repair",
                (await self.state.meta("forward_repair")),
            )
        if (
            params.get("expected_revision")
            and params["expected_revision"] != self.revision
        ):
            raise PluginError(
                "stale_revision", "Composition changed before operation started"
            )
        if action == "plan":
            return await self.plan(params, operation)
        approved = None
        if params.get("plan_id") and action != "migrate-apply":
            approved = await self.state.meta(f"plan/{params['plan_id']}")
            if (
                not approved
                or approved["action"] != action
                or approved.get("target") != name
                or approved["basis"] != await self.plan_basis()
            ):
                raise PluginError(
                    "stale_plan",
                    "Review a fresh plan; composition, runtime or schema changed",
                )
        if action == "add":
            source = params["source"]
            if not name:
                name = (await self.packages.capture(source))["plugin_id"]
            if name in self.composition.plugins:
                raise PluginError(
                    "already_installed",
                    "Plugin already exists in composition; validate/reload or edit its source",
                )
            request = PluginRequest(
                source=source,
                enabled=params.get("enable", False),
                config=params.get("config", {}),
            )
            candidate = await self.packages.prepare(name, source, request.config)
            await self.validate_candidate(candidate, operation)
            self.composition, self.revision = self.config.put_plugin(
                name, request, self.revision
            )
            (await self.save_candidate(candidate))
            if request.enabled:
                proposed = await self.selected()
                proposed[name] = candidate
                await self.replace(proposed, {name}, operation)
            (await self.write_lock())
            return {
                "plugin_id": name,
                "candidate": candidate["id"],
                "enabled": request.enabled,
            }
        if (
            action
            in (
                "validate",
                "update",
                "reload",
                "enable",
                "disable",
                "remove",
                "stop",
                "migrate-plan",
                "migrate-apply",
            )
            and name not in self.composition.plugins
        ):
            raise PluginError(
                "plugin_missing", "Plugin is not declared in toolframe.toml"
            )
        if action == "update":
            request = self.composition.plugins[name]
            source = params.get("source", request.source)
            pin = False
            if "source" not in params:
                row = await self.state.plugin(name)
                previous = row["last_candidate"] or row["current_candidate"]
                retained = await self.state.candidate(previous) if previous else None
                if retained and retained["source"]["request"] == source:
                    source = retained["source"].get("requirement", source)
                    pin = source != request.source
            if floating_index_source(source):
                fixed = await self.selected()
                fixed.pop(name, None)
                candidates = await prepare_selection(
                    self.packages,
                    {name: request.model_copy(update={"source": source})},
                    self.composition.bindings,
                    fixed=fixed,
                    enabled={
                        key
                        for key, item in self.composition.plugins.items()
                        if item.enabled
                    },
                    pin=pin,
                )
                candidate = candidates[name]
                source = candidate["source"]["request"]
            else:
                candidate = await self.packages.prepare(name, source, request.config)
            if source != request.source:
                self.composition, self.revision = self.config.put_plugin(
                    name, request.model_copy(update={"source": source}), self.revision
                )
            elif self.config.revision != self.revision:
                raise PluginError(
                    "stale_plan", "Composition changed while staging update"
                )
            (await self.save_candidate(candidate))
            (await self.write_lock())
            return {
                "candidate": candidate["id"],
                "validation": candidate["validation"],
                "activated": False,
            }
        if action == "validate":
            candidate = await self._prepare(name, operation)
            await self.validate_candidate(candidate, operation)
            if params.get("runtime"):
                await self.runtime_validate(name, candidate)
                candidate["validation"]["runtime"] = "passed_scratch"
                (await self.save_candidate(candidate))
            (await self.write_lock())
            return {"candidate": candidate["id"], "validation": candidate["validation"]}
        if action == "stop" and params.get("force"):
            if name in self.orphans:
                await self.stop_orphan(name)
                return {"plugin_id": name, "orphan_stopped": True}
            affected = self.graph.affected(self.graph, {name})
            self.gated.update(affected)
            for member in affected:
                runtime = self.runtimes.get(member)
                if runtime:
                    runtime.admitted = False
                    await runtime.worker.force_stop()
                    # A failed Cordis disposer has already consumed its scope;
                    # record confirmed termination even when it cannot run again.
                    await self._dispose(runtime)
                (await self.state.stopped(member, True))
            for root in list(self.roots.values()):
                if affected & root.runtimes.keys():
                    await self.end_root(root)
            for member in reversed(self.graph.order):
                if member in affected and member in self.registered:
                    with contextlib.suppress(Exception):
                        await self.manager.unregister(member)
                    self.registered.discard(member)
            await self.publish()
            return {
                "affected": sorted(affected),
                "warning": "Interrupted write acknowledgements may be unknown; inspect mutation receipts",
            }
        if action in ("migrate-plan", "migrate-apply"):
            candidate = await self._prepare(name, operation)
            schema = candidate["definition"].get("store")
            if not schema:
                raise PluginError(
                    "store_not_declared", "Plugin does not declare a store schema"
                )
            proposed = {**(await self.selected()), name: candidate}
            graph = Graph(proposed, self.composition.bindings)
            storage = self._sql_runtime(name, graph=graph)
            plan = await self._migration_plan(storage, name, schema)
            if action == "migrate-plan":
                (
                    await self.state.set_meta(
                        f"migration/{name}",
                        {
                            "plan": plan,
                            "candidate": candidate["id"],
                            "revision": self.revision,
                        },
                    )
                )
                return {
                    **plan,
                    "candidate": candidate["id"],
                    "desired_revision": self.revision,
                }
            saved = await self.state.meta(f"migration/{name}")
            if (
                not saved
                or params.get("plan_id") != plan["plan_id"]
                or saved["candidate"] != candidate["id"]
                or saved["revision"] != self.revision
                or saved["plan"]["plan_id"] != plan["plan_id"]
            ):
                raise PluginError(
                    "stale_plan", "Review a current migration plan before applying"
                )
            proposed = await self.selected()
            proposed[name] = candidate
            await self.replace(
                proposed,
                {name},
                operation,
                migration={
                    "owner": name,
                    "schema": schema,
                    "provider": storage.name,
                    "plan_id": plan["provider_plan_id"],
                },
            )
            return {"migration": plan["plan_id"], "candidate": candidate["id"]}
        if action in ("reload", "enable", "disable", "remove", "stop"):
            if action in ("reload", "enable"):
                candidate = (
                    (
                        await self.state.candidate(
                            approved.get("target_candidate")
                            or approved["candidates"][name]
                        )
                    )
                    if approved
                    else await self._prepare(
                        name, operation, installed=action == "enable"
                    )
                )
            if action == "stop":
                (await self.state.stopped(name, True))
            if action in ("enable", "disable", "remove"):

                def edit(document):
                    if action == "remove":
                        del document["plugins"][name]
                    else:
                        document["plugins"][name]["enabled"] = action == "enable"

                self.composition, self.revision = self.config.edit(self.revision, edit)
            proposed = await self.selected()
            if action in ("reload", "enable"):
                (await self.state.stopped(name, False))
                if self.composition.plugins[name].enabled:
                    proposed[name] = candidate
            else:
                proposed.pop(name, None)
            await self.replace(proposed, {name}, operation)
            return {"plugin_id": name, "affected_action": action}
        if action == "apply":
            proposed = {}
            for member, request in self.composition.plugins.items():
                self.check_cancel(operation)
                (await self.state.stopped(member, False))
                if request.enabled:
                    proposed[member] = (
                        (await self.state.candidate(approved["candidates"][member]))
                        if approved
                        else await self._prepare(member, operation, installed=True)
                    )
            changed = {
                member
                for member in proposed.keys() | self.graph.candidates.keys()
                if proposed.get(member, {}).get("id")
                != self.graph.candidates.get(member, {}).get("id")
            }
            changed |= {
                member
                for member in proposed
                if member not in self.runtimes or not self.runtimes[member].admitted
            }
            graph = Graph(proposed, self.composition.bindings)
            changed |= {
                member
                for member in graph.bindings
                if graph.bindings[member] != self.graph.bindings.get(member)
            }
            await self.replace(proposed, changed, operation)
            return {"plugins": sorted(proposed)}
        raise PluginError("action_unknown", f"Unknown management action {action}")

    async def validate_candidate(self, candidate, operation):
        """Validate a hypothetical target without publishing or changing live stores."""
        name = candidate["plugin_id"]
        proposed = {**(await self.selected()), name: candidate}
        validation = candidate["validation"]
        stages = validation.setdefault("stages", {})
        stage = "dependency_graph"
        try:
            (await self.phase(operation, "preflight", {"plugin_id": name}))
            graph = Graph(proposed, self.composition.bindings)
            blockers = {
                member: graph.missing[member]
                for member in graph.closure(name)
                if graph.missing[member]
            }
            stages[stage] = {
                "status": "blocked" if blockers else "passed",
                "reasons": blockers,
            }
            validation["blockers"] = blockers
            definition = graph.definitions[name]
            stage = "store_history"
            stages[stage] = {"status": "skipped", "reason": "No managed store declared"}
            requires_migration = False
            if definition.store:
                storage_owner = graph.stores.get(name)
                if storage_owner:
                    self.stores.check(name, storage_owner)
                if storage_owner and self.healthy_runtime(storage_owner):
                    plan = await self._migration_plan(
                        self.runtimes[storage_owner],
                        name,
                        definition.store.model_dump(),
                    )
                    requires_migration = plan["requires_apply"]
                    stages[stage] = {
                        "status": "blocked" if requires_migration else "passed",
                        "plan": plan,
                    }
                else:
                    stages[stage] = {
                        "status": "blocked",
                        "reason": "Storage provider is unavailable",
                    }
                    blockers.setdefault(name, []).append("live_sql_history")
            stage = "dashboard_environment"
            stages[stage] = {"status": "skipped", "reason": "No Dashboard provider"}
            dashboard = graph.providers.get("dashboard")
            if dashboard:
                cohort = [
                    item
                    for owner, item in proposed.items()
                    if owner == dashboard
                    or any(
                        resource.kind == "dashboard.page"
                        for resource in graph.definitions[owner].resources
                    )
                ]
                environment = await self.packages.cohort_environment(cohort, extra="ui")
                proposed[dashboard] = {
                    **proposed[dashboard],
                    "ui_environment": environment,
                }
                stages[stage] = {"status": "passed", "environment": environment["id"]}
            resources = graph.resources()
            gateway = compile_gateway(self.composition, graph)
            web = graph.providers.get("web")
            if web:
                stage = "web_environment"
                cohort = [
                    item
                    for owner, item in proposed.items()
                    if owner == web
                    or any(
                        r.kind == "web.app" for r in graph.definitions[owner].resources
                    )
                ]
                environment = await self.packages.cohort_environment(
                    cohort, extra="web"
                )
                proposed[web] = {**proposed[web], "web_environment": environment}
                stages[stage] = {"status": "passed", "environment": environment["id"]}
            for kind, resource_kind in {
                "dashboard": "dashboard.page",
                "web": "web.app",
                "mcp": "mcp.tool",
                "llms": "llms.document",
            }.items():
                stage = f"provider_{kind}"
                owned = [
                    resource
                    for resource in resources
                    if resource.resource.kind == resource_kind
                ]
                provider = graph.providers.get(kind)
                if owned and provider:
                    prepared = await self.prepare_provider(proposed[provider], owned)
                    if kind == "web":
                        self.validate_web_preparation(prepared, owned, graph, gateway)
                    stages[stage] = {"status": "passed"}
                else:
                    stages[stage] = {
                        "status": "skipped",
                        "reason": "No matching resources or provider",
                    }
            if self.config.revision != self.revision:
                raise PluginError("stale_plan", "Composition changed during validation")
            self.check_cancel(operation)
            validation.update(
                preflight="passed",
                ready_to_activate=not blockers and not requires_migration,
                migration_required=requires_migration,
                observed_at=time.time(),
            )
        except Exception as error:
            stages[stage] = {"status": "failed", "reason": str(error)}
            validation.update(
                preflight="failed", ready_to_activate=False, observed_at=time.time()
            )
            raise
        finally:
            (await self.save_candidate(candidate))

    async def plan_basis(self):
        schemas = {}
        for name, definition in self.graph.definitions.items():
            if definition.store and name in self.runtimes:
                schemas[name] = await self.admin(
                    self._sql_runtime(name), "inspect", {"owner": name}
                )
        return {
            "revision": self.config.revision,
            "generations": {
                name: item.generation for name, item in self.runtimes.items()
            },
            "epoch": self.epoch,
            "schemas": schemas,
            "store_bindings": dict(self.stores.bindings),
            "stopped": sorted(
                [row["id"] for row in (await self.state.plugins()) if row["stopped"]]
            ),
        }

    async def plan(self, params, operation):
        action, name = params.get("action", "apply"), params.get("id")
        if action not in {"apply", "reload", "enable", "disable", "remove", "stop"}:
            raise PluginError("invalid_request", "This action cannot be planned")
        if action != "apply" and name not in self.composition.plugins:
            raise PluginError("plugin_missing", "Plan requires a declared plugin ID")
        basis = await self.plan_basis()
        proposed = await self.selected()
        if action == "apply":
            proposed = {}
            for member, request in self.composition.plugins.items():
                if request.enabled:
                    proposed[member] = await self._prepare(
                        member, operation, installed=True
                    )
        elif action in {"reload", "enable"}:
            candidate = await self._prepare(
                name, operation, installed=action == "enable"
            )
            if action == "enable" or self.composition.plugins[name].enabled:
                proposed[name] = candidate
        else:
            proposed.pop(name, None)
        replacement = Graph(proposed, self.composition.bindings)
        changed = {
            member
            for member in proposed.keys() | self.graph.candidates.keys()
            if proposed.get(member, {}).get("id")
            != self.graph.candidates.get(member, {}).get("id")
            or replacement.bindings.get(member) != self.graph.bindings.get(member)
            or member not in self.runtimes
            or not self.runtimes[member].admitted
        }
        if name:
            changed.add(name)
        result = {
            "action": action,
            "target": name,
            "basis": basis,
            "candidates": {
                member: candidate["id"] for member, candidate in proposed.items()
            },
            "affected": sorted(self.graph.affected(replacement, changed)),
            "missing_dependencies": {
                member: missing
                for member, missing in replacement.missing.items()
                if missing
            },
            "migrations": {},
            "stores": self.stores.describe(replacement),
        }
        if action in {"reload", "enable"}:
            result["target_candidate"] = candidate["id"]
        for member in changed & proposed.keys():
            definition = replacement.definitions[member]
            if definition.store and member in self.runtimes:
                provider = replacement.stores.get(member)
                if provider and self.healthy_runtime(provider):
                    result["migrations"][member] = await self._migration_plan(
                        self.runtimes[provider], member, definition.store.model_dump()
                    )
        if basis != await self.plan_basis():
            raise PluginError("stale_plan", "Runtime changed while planning")
        result["plan_id"] = digest(result)
        (await self.state.set_meta(f"plan/{result['plan_id']}", result))
        return result

    def _sql_runtime(self, owner, *, graph=None):
        provider = (graph or self.graph).stores.get(owner)
        if not provider or provider not in self.runtimes:
            raise PluginError(
                "sql_unavailable",
                "The owner's declared storage provider must be active",
            )
        self.stores.check(owner, provider)
        return self.runtimes[provider]

    async def _migration_plan(self, storage, owner, schema):
        plan = await self.admin(storage, "plan", {"owner": owner, "schema": schema})
        identity = {
            "provider": storage.name,
            "provider_generation": storage.generation,
            "provider_plan_id": plan["plan_id"],
        }
        return {**plan, **identity, "plan_id": digest({**plan, **identity})}

    async def admin(self, storage, method, params):
        return await storage.worker.request(
            "runtime.invoke",
            {
                "service": "toolframe.sql.admin",
                "method": method,
                "params": params,
                "context": {
                    "management": True,
                    "application_id": self.composition.application.id,
                    "deadline": time.time()
                    + self.composition.runtime.activation_timeout,
                },
            },
            timeout=self.composition.runtime.activation_timeout,
        )

    async def change_schema(self, storage, method, params):
        owner = params["owner"]
        self.stores.check(owner, storage.name)
        before = await self.admin(storage, "inspect", {"owner": owner})
        remembered = owner in self.stores.bindings
        await self.stores.remember(owner, storage.name)
        previous_repair = await self.state.meta("forward_repair")
        (
            await self.state.set_meta(
                "forward_repair",
                {
                    "owner": owner,
                    "provider": storage.name,
                    "before": before,
                    "target": params["schema"],
                    "state": "in_progress",
                },
            )
        )
        try:
            result = await self.admin(storage, method, params)
        except BaseException:
            try:
                after = await self.admin(storage, "inspect", {"owner": owner})
                changed = after["history"] != before["history"]
            except BaseException:  # noqa: BLE001 - unverified commit forbids automatic rollback
                changed = (
                    True  # Lost acknowledgements never authorize an old-code restart.
                )
            if changed:
                self.schema_changed = True
                self.schema_events.append(
                    {
                        "owner": owner,
                        "provider": storage.name,
                        "state": "committed_or_unknown",
                    }
                )
            else:
                (await self.state.set_meta("forward_repair", previous_repair))
                if not remembered and not before["exists"] and not after["exists"]:
                    await self.stores.forget_empty(owner)
            raise
        if result.get("committed"):
            self.schema_changed = True
            self.schema_events.append(
                {
                    **result,
                    "owner": owner,
                    "provider": storage.name,
                    "state": "committed",
                }
            )
        else:
            (await self.state.set_meta("forward_repair", previous_repair))
        return result

    async def _activate(self, name, context):
        candidate = self.graph.candidates[name]
        generation = uuid.uuid4().hex
        runtime = Runtime(name, generation, candidate)
        self.runtimes[name] = runtime
        definition = self.graph.definitions[name]
        # Cordis cleanup owns even partially activated workers.
        context.on_dispose(self._dispose, runtime)
        environment = candidate.get(
            "web_environment", candidate.get("ui_environment", candidate["environment"])
        )
        runtime.worker = await WorkerProcess.start(
            self.home / environment["python"],
            candidate["entrypoint"],
            lambda method, params: self.worker_call(runtime, method, params),
            cwd=self.home,
            host_token=f"{self.host_run}/{name}/{generation}",
            tracing=self.tracing,
            trace_attributes={
                "service.name": f"toolframe.plugin.{name}",
                "service.namespace": self.composition.application.id,
                "service.instance.id": generation,
                "toolframe.application.id": self.composition.application.id,
                "toolframe.plugin.id": name,
                "toolframe.plugin.generation": generation,
            },
        )
        self.worker_owners[str(runtime.worker.pid)] = {
            "name": name,
            "token": f"{self.host_run}/{name}/{generation}",
        }
        (await self.state.set_meta("worker_owners", self.worker_owners))
        (await self.record(runtime, "starting"))
        await runtime.worker.request("runtime.describe")
        store_exists = None
        if definition.private_secrets and definition.store:
            observed_store = await self.admin(
                self._sql_runtime(name), "inspect", {"owner": name}
            )
            store_exists = observed_store["exists"]
        private_secrets = self.access.private_secrets(
            definition, store_exists=store_exists
        )
        if definition.store:
            await self.change_schema(
                self._sql_runtime(name),
                "ensure_store",
                {"owner": name, "schema": definition.store.model_dump()},
            )
        runtime.health = await self.probe(
            runtime,
            "runtime.activate",
            {
                "generation": generation,
                "config": candidate["config"],
                "private_secrets": private_secrets,
                "endpoints": candidate.get(
                    "endpoints", endpoints(self.gateway, definition.provider_kind)
                ),
                "state_directory": str(self.home / "data/plugins")
                if definition.provider_kind == "sql"
                else str(self.home / "data/objects" / name)
                if definition.provider_kind == "object-store"
                else str(self.home / ".toolframe/web-assets")
                if definition.provider_kind == "web"
                else None,
            },
        )
        runtime.observed = time.time()
        if runtime.health.get("runtime") != "ready" or runtime.health.get(
            "business"
        ) in ("failed", "not_ready"):
            raise PluginError(
                "health_failed", f"{name} did not become ready", runtime.health
            )
        (await self.record(runtime, "prepared"))
        return runtime

    async def probe(self, runtime, method="runtime.health", params=None):
        members = self.graph.closure(
            runtime.name,
            optional=True,
            available=self.available_dependencies(),
        )
        leases = ExitStack()
        root = None
        try:
            for name in members - {runtime.name}:
                leases.enter_context(self.manager.lease(name))
            root = Root(
                uuid.uuid4().hex,
                runtime.name,
                time.time() + self.composition.runtime.activation_timeout,
                {name: self.runtimes[name] for name in members},
                leases,
                read_only=True,
                bindings={name: dict(self.graph.bindings[name]) for name in members},
            )
            root.paths[(runtime.name,)] = 1
            self.roots[root.id] = root
            try:
                payload = {
                    **(params or {}),
                    "context": {
                        "root_id": root.id,
                        "deadline": root.deadline,
                        "chain": [runtime.name],
                    },
                }
                return await runtime.worker.request(
                    method, payload, timeout=self.composition.runtime.activation_timeout
                )
            finally:
                await self.end_root(root)
        finally:
            if root is None or root.id not in self.roots:
                leases.close()

    async def _dispose(self, runtime):
        runtime.admitted = False
        for session in list(self.gateway_sessions.get(runtime.name, ())):
            session.cancel()
        try:
            if runtime.worker:
                await runtime.worker.close(
                    timeout=self.composition.runtime.disposal_timeout,
                    allow_exited=True,
                )
        except BaseException:
            if not self.state.failed:
                await self.record(runtime, "cleanup_failed")
            raise
        else:
            if runtime.worker:
                self.worker_owners.pop(str(runtime.worker.pid), None)
                if not self.state.failed:
                    await self.state.set_meta("worker_owners", self.worker_owners)
            if self.runtimes.get(runtime.name) is runtime:
                self.runtimes.pop(runtime.name)
            if not self.state.failed:
                await self.record(runtime, "stopped")

    async def record(self, runtime, state):
        (
            await self.state.runtime(
                runtime.name,
                {
                    "state": state,
                    "generation": runtime.generation,
                    "candidate": runtime.candidate["id"],
                    "pid": runtime.worker.pid if runtime.worker else None,
                    "health": runtime.health,
                    "observed": runtime.observed,
                    "host_run": self.host_run,
                    "epoch": runtime.epoch,
                },
            )
        )
        if runtime._logged_state != state:
            self.log.info(
                "Plugin state changed",
                extra={
                    "toolframe.target.plugin.id": runtime.name,
                    "toolframe.target.generation": runtime.generation,
                    "toolframe.plugin.state": state,
                },
            )
            runtime._logged_state = state

    async def _register_set(self, names):
        for name in self.graph.order:
            if name not in names or name in self.optional_failures:
                continue
            missing = tuple(
                f"missing:{name}:{service}" for service in self.graph.missing[name]
            )
            try:
                await self.manager.register(
                    CordisPlugin(
                        name=name,
                        setup=lambda context, name=name: self._activate(name, context),
                        requires=tuple(self.graph.dependencies[name]) + missing,
                    )
                )
            except Exception as error:
                if (
                    self.graph.definitions[name].provider_kind
                    not in {
                        "dashboard",
                        "web",
                        "mcp",
                        "llms",
                        "auth",
                        "dashboard-auth",
                        "mcp-auth",
                        "oidc-auth",
                    }
                    or name not in self.optional_new
                ):
                    raise
                # A newly installed presentation must not prevent independent
                # business services from starting. Cleanup still has to succeed.
                if name in {item["name"] for item in self.manager.snapshot()}:
                    await self.manager.unregister(name)
                self.optional_failures[name] = str(error)
                (
                    await self.state.runtime(
                        name,
                        {
                            "state": "failed",
                            "error": str(error),
                            "host_run": self.host_run,
                        },
                    )
                )
            finally:
                # Registration remains present when setup raises; cleanup/recovery must remove it.
                self.registered = {item["name"] for item in self.manager.snapshot()}
            runtime = self.runtimes.get(name)
            if (
                name not in self.optional_failures
                and not self.graph.missing[name]
                and all(
                    dependency in self.runtimes
                    for dependency in self.graph.dependencies[name]
                )
                and (runtime is None or runtime.health.get("runtime") != "ready")
            ):
                raise PluginError(
                    "activation_failed",
                    f"{name} failed to activate",
                    {"cordis": self.manager.snapshot()},
                )

    async def _drain(self, affected, timeout, operation=None):
        try:
            async with asyncio.timeout(timeout):
                while any(
                    affected & root.runtimes.keys() for root in self.roots.values()
                ):
                    if operation:
                        self.check_cancel(operation)
                    # Shutdown cancels the health/exit loops; a cancelled probe
                    # can leave cleanup awaiting acknowledgement. The drain must
                    # retry that idempotent cleanup before releasing its leases.
                    for root_id in list(self.failed_roots):
                        root = self.roots.get(root_id)
                        if root and affected & root.runtimes.keys():
                            try:
                                await self.end_root(root)
                            except Exception:  # noqa: BLE001 - retain lease until cleanup is proven
                                (
                                    await self.state.set_meta(
                                        "cleanup_pending", sorted(self.failed_roots)
                                    )
                                )
                    self.changed.clear()
                    try:
                        await asyncio.wait_for(self.changed.wait(), 0.1)
                    except TimeoutError:
                        pass
        except TimeoutError as error:
            raise PluginError(
                "drain_timeout",
                "Admitted calls have not completed; old workers are retained",
            ) from error

    async def _resume(self, affected):
        errors = {}
        for name in affected:
            runtime = self.runtimes.get(name)
            if runtime:
                try:
                    await runtime.worker.request("runtime.resume")
                except Exception as error:  # noqa: BLE001
                    errors[name] = str(error)
        if errors:
            raise PluginError("resume_failed", "Some workers could not resume", errors)
        # All acknowledgements precede a single admission barrier. No partial cohort
        # may write while another member can still cause pre-admission recovery.
        for name in affected:
            runtime = self.runtimes.get(name)
            if runtime:
                runtime.admitted = True
                (await self.record(runtime, "active"))
                if name not in self.cleanup_blocked:
                    self.gated.discard(name)
        self.changed.set()

    async def replace(self, proposed, changed, operation, *, migration=None):
        try:
            return await self._replace(
                proposed, changed, operation, migration=migration
            )
        finally:
            self.transitioning.clear()

    async def _replace(self, proposed, changed, operation, *, migration=None):
        repair = await self.state.meta("forward_repair")
        self.schema_changed = bool(repair)
        self.schema_events = (
            [repair]
            if repair and repair.get("owner")
            else (repair or {}).get("schemas", [])
        ).copy()
        if self.orphans:
            raise PluginError(
                "cleanup_failed",
                "Previous worker ownership has not been released; use explicit stop --force for verified survivors",
                self.orphans,
            )
        replacement = Graph(proposed, self.composition.bindings)
        old_graph = self.graph
        old_gateway = self.gateway
        target_gateway = compile_gateway(self.composition, replacement)
        for name, definition in replacement.definitions.items():
            if definition.provider_kind in {"dashboard", "dashboard-auth", "web"}:
                settings = {
                    **endpoints(target_gateway, definition.provider_kind),
                    "web": self.composition.web.model_dump(),
                }
                proposed[name] = {**proposed[name], "endpoints": settings}
                if (
                    name in self.runtimes
                    and self.runtimes[name].candidate.get("endpoints") != settings
                ):
                    changed = set(changed) | {name}
        changed = set(changed) | {
            name
            for name in replacement.bindings
            if replacement.bindings[name] != old_graph.bindings.get(name)
        }
        for owner, provider in replacement.stores.items():
            self.stores.check(owner, provider)
        self.optional_new = {
            name
            for name, definition in replacement.definitions.items()
            if definition.provider_kind
            in {
                "dashboard",
                "web",
                "mcp",
                "llms",
                "auth",
                "dashboard-auth",
                "mcp-auth",
                "oidc-auth",
            }
            and name not in self.runtimes
        }
        self.optional_failures = {}
        affected = old_graph.affected(replacement, changed)
        for pending in self.schema_events:
            owner = pending["owner"]
            provider = pending.get("provider")
            if (
                owner not in affected
                or owner not in replacement.stores
                or (provider and replacement.stores[owner] != provider)
            ):
                raise PluginError(
                    "forward_repair_required",
                    "Recover the recorded store and its original provider before "
                    "performing an unrelated replacement",
                    {"owner": owner, "provider": provider},
                )
        if (
            migration
            and replacement.stores.get(migration["owner"]) != migration["provider"]
        ):
            raise PluginError("stale_plan", "Migration storage binding changed")
        for name, definition in replacement.definitions.items():
            if definition.provider_kind in {"dashboard", "web"} and (
                name in affected or name not in self.runtimes
            ):
                cohort = [
                    candidate
                    for owner, candidate in proposed.items()
                    if owner == name
                    or any(
                        resource.kind
                        == (
                            "web.app"
                            if definition.provider_kind == "web"
                            else "dashboard.page"
                        )
                        for resource in replacement.definitions[owner].resources
                    )
                ]
                try:
                    cohort_ids = {item["plugin_id"]: item["id"] for item in cohort}
                    prior = proposed[name]
                    profile = "web" if definition.provider_kind == "web" else "ui"
                    environment = await self.packages.cohort_environment(
                        cohort, extra=profile
                    )
                    proposed[name] = {
                        **prior,
                        profile + "_environment": environment,
                        profile + "_cohort": cohort_ids,
                    }
                except Exception as error:
                    if name not in self.optional_new:
                        raise
                    self.optional_failures[name] = str(error)
                    (
                        await self.state.runtime(
                            name,
                            {
                                "state": "failed",
                                "error": str(error),
                                "host_run": self.host_run,
                            },
                        )
                    )
        replacement = Graph(proposed, self.composition.bindings)
        (await self.phase(operation, "prepare_resources"))
        for kind, provider in replacement.providers.items():
            if (
                kind not in {"dashboard", "web", "mcp", "llms"}
                or provider in self.optional_failures
            ):
                continue
            resource_kind = {
                "dashboard": "dashboard.page",
                "web": "web.app",
                "mcp": "mcp.tool",
                "llms": "llms.document",
            }[kind]
            if provider in affected or old_graph.descriptor_digest(
                resource_kind
            ) != replacement.descriptor_digest(resource_kind):
                try:
                    prepared = await self.prepare_provider(
                        proposed[provider],
                        [
                            resource
                            for resource in replacement.resources()
                            if resource.resource.kind == resource_kind
                        ],
                    )
                    if kind == "web":
                        self.validate_web_preparation(
                            prepared,
                            replacement.resources(),
                            replacement,
                            target_gateway,
                        )
                except Exception as error:
                    if provider not in self.optional_new:
                        raise
                    self.optional_failures[provider] = str(error)
                    (
                        await self.state.runtime(
                            provider,
                            {
                                "state": "failed",
                                "error": str(error),
                                "host_run": self.host_run,
                            },
                        )
                    )
        # Existing stores are inspected before quiescence. A schema change is never hidden in reload.
        for name in changed & replacement.candidates.keys():
            definition = replacement.definitions[name]
            if (
                definition.store
                and name in self.runtimes
                and replacement.stores.get(name) in self.runtimes
            ):
                plan = await self.admin(
                    self._sql_runtime(name, graph=replacement),
                    "plan",
                    {"owner": name, "schema": definition.store.model_dump()},
                )
                if plan["requires_apply"] and migration is None:
                    raise PluginError(
                        "migration_required",
                        "Review and explicitly apply the existing-data migration",
                        plan,
                    )
        if self.config.revision != self.revision:
            raise PluginError("stale_plan", "Composition changed before cutover")
        self.check_cancel(operation)
        (
            await self.phase(
                operation,
                "quiesce",
                {
                    "affected": sorted(affected),
                    "old": {
                        name: value["id"]
                        for name, value in old_graph.candidates.items()
                    },
                    "candidate": {
                        name: value["id"] for name, value in proposed.items()
                    },
                    "revision": self.revision,
                },
            )
        )
        self.gated.update(affected)
        self.transitioning.update(affected)
        try:
            for name in affected:
                if name in self.runtimes:
                    self.runtimes[name].admitted = False
                    worker = self.runtimes[name].worker
                    if worker.process.returncode is None:
                        await worker.request("runtime.quiesce")
            (await self.phase(operation, "draining"))
            await self._drain(
                affected, self.composition.runtime.drain_timeout, operation
            )
            self.check_cancel(operation)
            if self.config.revision != self.revision:
                raise PluginError(
                    "stale_plan",
                    "Composition changed while draining; old workers are retained",
                )
        except BaseException:
            await self._resume(affected)
            raise
        (await self.phase(operation, "disposing"))
        for name in reversed(old_graph.order):
            if name in affected and name in self.registered:
                try:
                    await self.manager.unregister(name)
                    self.registered.remove(name)
                except BaseException as error:
                    raise PluginError(
                        "cleanup_failed",
                        "Old worker cleanup failed; affected admission remains closed",
                        {"plugin_id": name, "error": str(error)},
                    ) from error
        try:
            if migration:
                (await self.phase(operation, "migrating"))
                storage = self._sql_runtime(migration["owner"], graph=replacement)
                result = await self.change_schema(storage, "apply", migration)
                (await self.phase(operation, "migration_committed", result))
            self.graph = replacement
            (await self.phase(operation, "activating"))
            await self._register_set(set(proposed) - self.registered)
            (await self.phase(operation, "publishing"))
            await self.publish(gateway=target_gateway)
            for name, candidate in proposed.items():
                (await self.save_candidate(candidate))
                if (await self.state.plugin(name))["current_candidate"] != candidate[
                    "id"
                ]:
                    (await self.state.select(name, candidate["id"]))
            (await self.write_lock())
            await self._resume(affected | (set(proposed) - set(old_graph.candidates)))
            self.gated.difference_update(affected - self.runtimes.keys())
            (await self.state.set_meta("forward_repair", None))
        except Exception as failure:
            self.gated.update(affected)
            for name in reversed(self.graph.order):
                if name in affected and name in self.registered:
                    try:
                        await self.manager.unregister(name)
                        self.registered.remove(name)
                    except BaseException as cleanup:
                        raise PluginError(
                            "cleanup_failed",
                            "Candidate cleanup could not be proven",
                            {"error": str(cleanup)},
                        ) from cleanup
            if self.schema_changed:
                (
                    await self.state.set_meta(
                        "forward_repair",
                        {
                            "operation": operation,
                            "schemas": self.schema_events,
                            "candidates": {
                                name: item["id"] for name, item in proposed.items()
                            },
                        },
                    )
                )
                raise PluginError(
                    "forward_repair_required",
                    "Schema committed or acknowledgement is unknown; data retained and old code was not restarted",
                    {"error": str(failure), "schemas": self.schema_events},
                ) from failure
            self.graph = old_graph
            self.gateway = old_gateway
            recover = affected & old_graph.candidates.keys()
            if not recover:
                raise
            (await self.phase(operation, "recovering"))
            try:
                await self._register_set(recover - self.registered)
                await self.publish(gateway=old_gateway)
                await self._resume(recover)
                for name in recover:
                    old = old_graph.candidates[name]
                    if (await self.state.plugin(name))["current_candidate"] != old[
                        "id"
                    ]:
                        (await self.state.select(name, old["id"]))
                (await self.write_lock())
            except Exception as recovery:
                raise PluginError(
                    "recovery_failed",
                    "One old-snapshot recovery failed",
                    {"error": str(recovery), "original": str(failure)},
                ) from recovery
            raise PluginError(
                "failed_recovered",
                "Candidate failed; the old snapshot cohort was restarted once",
                {"error": str(failure), "affected": sorted(recover)},
            ) from failure
        if self.optional_failures:
            raise PluginError(
                "optional_provider_failed",
                "Business services started; some presentation providers need repair",
                self.optional_failures,
            )
        failures = {
            kind: status
            for kind, status in self.publication.items()
            if status["state"] == "failed"
        }
        if failures:
            raise PluginError(
                "publication_failed",
                "Business services are selected; failed presentation entrances remain unavailable",
                failures,
            )

    async def prepare_provider(self, candidate, resources):
        async def unavailable(method, params):
            raise PluginError(
                "validation_only",
                "Resource preparation cannot call application services",
            )

        environment = candidate.get(
            "web_environment", candidate.get("ui_environment", candidate["environment"])
        )
        worker = await WorkerProcess.start(
            self.home / environment["python"],
            candidate["entrypoint"],
            unavailable,
            cwd=self.home,
        )
        activated = False
        try:
            await worker.request("runtime.describe")
            health = await worker.request(
                "runtime.activate",
                {
                    "generation": "preflight",
                    "config": candidate["config"],
                    "endpoints": candidate.get("endpoints", {}),
                },
            )
            activated = True
            supplied = candidate.get("endpoints", {})
            if (
                candidate["definition"].get("provider_kind") == "dashboard"
                and supplied.get("profile") == "gateway-v2"
                and supplied.get("public_base") != "/"
                and (
                    "gateway-endpoints-v1" not in health.get("capabilities", [])
                    or health.get("public_base") != supplied.get("public_base")
                )
            ):
                raise PluginError(
                    "gateway_capability_missing",
                    "The Dashboard provider does not support its configured public base",
                )
            result = await worker.request(
                "provider.prepare",
                {
                    "resources": [resource.model_dump() for resource in resources],
                    "epoch": "preflight",
                },
            )
            await worker.request("provider.abort", {"epoch": "preflight"})
            return result
        finally:
            try:
                await worker.close(activated=activated)
            except BaseException:
                await worker.force_stop()  # validation has no live service/data access
                raise

    def validate_web_preparation(self, prepared, resources, graph, gateway):
        resources = [r for r in resources if r.resource.kind == "web.app"]
        values = prepared.get("web_apps", {})
        if set(values) != {r.key for r in resources}:
            raise PluginError(
                "web_contract_unsupported",
                "Web provider must return one versioned catalog per resource",
            )
        catalogs = {
            key: WebAppCatalog.model_validate(value) for key, value in values.items()
        }
        validate_web_catalogs(gateway, resources, catalogs, graph)
        return catalogs

    async def publish(self, *, gateway=None):
        # This snapshot includes health changes already observed, including those
        # repaired by a reload. A new failure during publication queues another pass.
        self.health_publication_pending = False
        gateway = gateway or self.gateway
        epoch = uuid.uuid4().hex
        generations = {
            name: runtime.generation for name, runtime in self.runtimes.items()
        }
        resources = [
            resource
            for resource in self.graph.resources(generations)
            if resource.owner in generations
            and all(
                member in self.runtimes
                and self.runtimes[member].health.get("runtime") == "ready"
                and self.runtimes[member].health.get("business")
                not in ("failed", "not_ready")
                for member in self.graph.closure(resource.owner)
            )
        ]
        self.epoch = epoch
        (
            await self.state.set_meta(
                "catalog",
                {
                    "epoch": epoch,
                    "resources": [resource.model_dump() for resource in resources],
                },
            )
        )
        for kind, resource_kind in (
            ("dashboard", "dashboard.page"),
            ("web", "web.app"),
            ("mcp", "mcp.tool"),
            ("llms", "llms.document"),
        ):
            name = self.graph.providers.get(kind)
            runtime = self.runtimes.get(name)
            if not runtime:
                self.publication[kind] = {"state": "unavailable", "epoch": epoch}
                continue
            try:
                prepared = await runtime.worker.request(
                    "provider.prepare",
                    {
                        "resources": [
                            resource.model_dump()
                            for resource in resources
                            if resource.resource.kind == resource_kind
                        ],
                        "epoch": epoch,
                    },
                )
                web_catalogs = (
                    self.validate_web_preparation(
                        prepared, resources, self.graph, gateway
                    )
                    if kind == "web"
                    else None
                )
                result = await runtime.worker.request(
                    "provider.commit", {"epoch": epoch}
                )
                if result.get("epoch") != epoch:
                    raise PluginError(
                        "epoch_mismatch",
                        "Provider did not acknowledge selected catalog",
                    )
                runtime.epoch = epoch
                if web_catalogs is not None:
                    self.web_access.publish(web_catalogs)
                self.publication[kind] = {
                    "state": "available",
                    "epoch": epoch,
                    "plugin_id": name,
                }
            except Exception as error:  # noqa: BLE001 - optional provider failure must not stop business
                runtime.epoch = None
                self.publication[kind] = {
                    "state": "failed",
                    "epoch": epoch,
                    "error": str(error),
                }
        self.gateway = gateway

    async def write_lock(self):
        await write_lock(self.home, self.state, self.composition, self.revision)

    def begin_root(self, owner, *, deadline=None, principal=None):
        if (
            owner in self.gated
            or owner not in self.runtimes
            or not self.runtimes[owner].admitted
        ):
            raise PluginError("plugin_reloading", f"{owner} is not admitting new calls")
        members = self.graph.closure(
            owner,
            optional=True,
            available=self.available_dependencies(),
        )
        leases = ExitStack()
        try:
            for name in members:
                if (
                    name in self.gated
                    or name not in self.runtimes
                    or not self.healthy_runtime(name)
                    or self.runtimes[name].worker.process.returncode is not None
                ):
                    raise PluginError(
                        "dependency_unavailable", f"{name} is unavailable"
                    )
                leases.enter_context(self.manager.lease(name))
            root = Root(
                uuid.uuid4().hex,
                owner,
                deadline or time.time() + 30,
                {name: self.runtimes[name] for name in members},
                leases,
                principal=principal,
                scope_catalog=self.access.catalog(),
                bindings={name: dict(self.graph.bindings[name]) for name in members},
            )
            self.roots[root.id] = root
            return root
        except BaseException:
            leases.close()
            raise

    async def end_root(self, root):
        if root.id not in self.roots:
            return
        try:
            failure = None
            for runtime in root.runtimes.values():
                if (
                    runtime.candidate["definition"].get("provider_kind") == "sql"
                    and runtime.worker.process.returncode is None
                ):
                    try:
                        await self.admin(runtime, "cleanup_root", {"root_id": root.id})
                    except BaseException as error:  # noqa: BLE001 - settle every leased store before propagating failure
                        # A failed provider must not leave another provider's
                        # transaction alive merely because it appeared first.
                        if failure is None:
                            failure = error
            if failure is not None:
                raise failure
        except BaseException:
            self.failed_roots.add(root.id)
            self.cleanup_blocked.add(root.owner)
            self.gated.add(root.owner)
            raise
        else:
            self.roots.pop(root.id, None)
            self.failed_roots.discard(root.id)
            if not any(
                self.roots[item].owner == root.owner for item in self.failed_roots
            ):
                self.cleanup_blocked.discard(root.owner)
            root.leases.close()
            self.changed.set()

    async def invoke(
        self, root, target, service, method, payload, *, owner=None, chain=()
    ):
        if target in chain:
            raise PluginError(
                "call_cycle", "Recursive cross-plugin service call rejected"
            )
        runtime = root.runtimes.get(target)
        if runtime is None:
            raise PluginError(
                "undeclared_dependency",
                "Target is outside the admitted dependency closure",
            )
        specification = (
            runtime.candidate["definition"]["services"]
            .get(service, {})
            .get("methods", {})
            .get(method)
        )
        if not specification:
            raise PluginError("method_not_found", "Method is not declared")
        if (
            root.principal is not None
            and specification.get("audience") != "internal"
            and not root.principal.allows(
                specification.get("required_scopes", ()), root.scope_catalog
            )
        ):
            raise PluginError(
                "forbidden", "The admitted caller does not have this method's scopes"
            )
        if root.read_only and (not specification or not specification.get("read_only")):
            raise PluginError(
                "probe_write_forbidden",
                "Activation and health probes may only call declared read-only methods",
            )
        path = (*chain, target)
        root.paths[path] = root.paths.get(path, 0) + 1
        source = root.runtimes.get(owner or root.owner, runtime)
        try:
            return await runtime.worker.request(
                "runtime.invoke",
                {
                    "service": service,
                    "method": method,
                    "params": payload,
                    "context": {
                        "root_id": root.id,
                        "deadline": root.deadline,
                        "chain": list(path),
                        "owner": source.name,
                        "owner_generation": source.generation,
                        "application_id": self.composition.application.id,
                        "principal": root.principal.model_dump()
                        if root.principal
                        else None,
                        "scope_catalog": root.scope_catalog,
                    },
                },
                timeout=max(0.01, root.deadline - time.time()),
            )
        finally:
            root.paths[path] -= 1
            if not root.paths[path]:
                del root.paths[path]

    async def call_service(self, owner, service, method, params, *, principal=None):
        root = self.begin_root(owner, principal=principal)
        try:
            return await self.invoke(root, owner, service, method, params)
        finally:
            await self.end_root(root)

    async def worker_call(self, runtime, method, params):
        if method == "task.end":
            root = self.roots.get(params["root_id"])
            if root is not None:
                if (
                    root.owner != runtime.name
                    or root.runtimes.get(runtime.name) is not runtime
                ):
                    raise PluginError(
                        "stale_generation",
                        "Task root belongs to another worker generation",
                    )
                # A withdrawn worker may settle only the root admitted to that
                # exact runtime; it cannot start work or settle its replacement.
                await self.end_root(root)
            return {}
        if self.runtimes.get(runtime.name) is not runtime:
            raise PluginError(
                "stale_generation", "Worker generation is no longer current"
            )
        if method == "mcp.access":
            if runtime.candidate["definition"].get("provider_kind") != "mcp":
                raise PluginError(
                    "forbidden", "Only the MCP provider may resolve MCP requests"
                )
            return await self.mcp_access.presentation(
                runtime, params.get("access_context")
            )
        if method in {"management.call", "access.call"}:
            if runtime.candidate["definition"].get("provider_kind") != "dashboard":
                raise PluginError(
                    "forbidden",
                    "Only the management dashboard may request management operations",
                )
            action = params["action"]
            identity = self.access.context(params.get("access_context"))
            if method == "access.call":
                return await self.access.manage(
                    identity, action, params.get("params", {})
                )
            if action == "cancel":
                operation = await self.state.operation(params["params"]["id"])
                scopes = operation_scopes(operation["kind"], operation["request"])
            else:
                scopes = operation_scopes(action, params.get("params"))
            await self.access.admit(
                identity, scopes, effective=False, activity=action == "cancel"
            )
            if action == "status":
                return await self.status()
            if action == "operation":
                return self.redact(await self.state.operation(params["params"]["id"]))
            if action == "operations":
                return self.redact(
                    await self.state.operation_page(params.get("params", {}))
                )
            if action == "cancel":
                identifier = params["params"]["id"]
                (await self.state.operation(identifier))
                self.cancellations.add(identifier)
                self.changed.set()
                return {"id": identifier, "requested": True}
            return self.redact(
                await self.submit(action, params.get("params", {}), identity=identity)
            )
        if method == "resource.call":
            resource = next(
                (
                    resource
                    for resource in self.graph.resources()
                    if resource.key == params["key"]
                ),
                None,
            )
            kind = runtime.candidate["definition"].get("provider_kind")
            if (
                not resource
                or {
                    "mcp": "mcp.tool",
                    "dashboard": "dashboard.page",
                    "web": "web.app",
                }.get(kind)
                != resource.resource.kind
            ):
                raise PluginError(
                    "resource_unavailable",
                    "Resource is absent or not owned by this provider kind",
                )
            if params["epoch"] != runtime.epoch or runtime.epoch != self.epoch:
                raise PluginError(
                    "stale_catalog",
                    "Resource callback belongs to an unselected catalog",
                )
            if kind == "mcp":
                service = resource.resource.data["service"]
                name = resource.resource.data["method"]
                if params["method"] != name:
                    raise PluginError(
                        "undeclared_method",
                        "MCP tool may call only its declared method",
                    )
                version = None  # The declared target service owns the tool's contract version.
            else:
                qualified, _, name = params["method"].partition("/")
                service, _, version = qualified.partition("@")
                version = int(version)
            definition = self.graph.definitions[resource.owner]
            target = resource.owner
            if service not in definition.services:
                options = [
                    item
                    for item in self.graph.bindings[resource.owner].values()
                    if item[1] == service
                ]
                if not options:
                    raise PluginError(
                        "undeclared_service",
                        "Page/tool owner did not declare this service",
                    )
                target = options[0][0]
            if (
                version is not None
                and self.graph.definitions[target].services[service].version != version
            ):
                raise PluginError(
                    "service_version_mismatch",
                    "Requested service version is unavailable",
                )
            specification = (
                self.graph.definitions[target].services[service].methods.get(name)
            )
            if specification is None or specification.audience == "internal":
                raise PluginError(
                    "forbidden", "Internal services are not page/tool entrances"
                )
            if kind == "mcp":
                if specification.audience != "business":
                    raise PluginError(
                        "forbidden", "MCP cannot publish system operations"
                    )
                principal = await self.mcp_access.resolve(
                    runtime,
                    params.get("access_context"),
                    scopes=(
                        *resource.resource.required_scopes,
                        *specification.required_scopes,
                    ),
                    activity=True,
                )
            elif kind == "web":
                if specification.audience != "business":
                    raise PluginError(
                        "forbidden", "Web apps may call only business methods"
                    )
                entry = self.web_access.context(
                    runtime,
                    params["key"],
                    params["epoch"],
                    params.get("access_context"),
                )
                if entry.method in {"GET", "HEAD"} and not specification.read_only:
                    raise PluginError(
                        "forbidden", "Safe Web requests may call only read-only methods"
                    )
                principal = await self.web_access.resolve(
                    runtime, params, specification.required_scopes
                )
            else:
                identity = self.access.context(params.get("access_context"))
                principal = await self.access.admit(
                    identity,
                    (
                        *resource.resource.required_scopes,
                        *specification.required_scopes,
                    ),
                    activity=params.get("activity", True),
                )
            root = self.begin_root(resource.owner, principal=principal)

            async def admitted():
                try:
                    return await self.invoke(
                        root, target, service, name, params.get("params", {})
                    )
                finally:
                    await self.end_root(root)

            # A browser disconnect ends its UI, not work already admitted by core.
            task = asyncio.create_task(admitted())
            task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            return await asyncio.shield(task)
        if method == "resource.admit":
            if runtime.candidate["definition"].get("provider_kind") != "web":
                raise PluginError(
                    "forbidden", "Only the Web provider may admit Web handlers"
                )
            return await self.web_access.admit(runtime, params)
        if method == "task.begin":

            def check_open():
                if self.closing:
                    raise PluginError("host_closing", "Core is shutting down")

            check_open()
            timeout = params.get("timeout", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                )
            try:
                timeout = float(timeout)
            except OverflowError as error:
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                ) from error
            if not math.isfinite(timeout) or timeout <= 0:
                raise PluginError(
                    "invalid_task_timeout", "Task timeout must be finite and positive"
                )

            def check_policy():
                if timeout > self.composition.runtime.max_task_timeout:
                    raise PluginError(
                        "task_timeout_exceeded",
                        "Task timeout exceeds the operator policy",
                        {"max_task_timeout": self.composition.runtime.max_task_timeout},
                    )

            check_policy()
            async with asyncio.timeout(self.composition.runtime.activation_timeout):
                while runtime.name in self.gated or not runtime.admitted:
                    check_open()
                    if self.runtimes.get(runtime.name) is not runtime:
                        raise PluginError(
                            "stale_generation", "Task belongs to a withdrawn generation"
                        )
                    self.changed.clear()
                    await self.changed.wait()
            check_open()
            if self.runtimes.get(runtime.name) is not runtime:
                raise PluginError(
                    "stale_generation", "Task belongs to a withdrawn generation"
                )
            # Waiting requests use the policy in force when they are admitted.
            check_policy()
            root = self.begin_root(runtime.name, deadline=time.time() + timeout)
            root.paths[(runtime.name,)] = 1
            return {
                "root_id": root.id,
                "deadline": root.deadline,
                "chain": [runtime.name],
            }
        if method == "service.call":
            root = self.roots.get(params.get("root_id"))
            if root is None:
                raise PluginError(
                    "root_required",
                    "Services require an admitted root; activation/probes may not start business calls",
                )
            chain = tuple(params.get("chain", ()))
            if (
                root.runtimes.get(runtime.name) is not runtime
                or not chain
                or chain[-1] != runtime.name
                or not root.paths.get(chain)
            ):
                raise PluginError(
                    "stale_generation",
                    "Nested call is outside its admitted root/generation",
                )
            binding = root.bindings.get(runtime.name, {}).get(params["alias"])
            if not binding:
                raise PluginError(
                    "undeclared_dependency", "Dependency alias was not bound"
                )
            target, service = binding
            if service == "toolframe.sql.admin":
                raise PluginError(
                    "forbidden", "Storage administrative interface is host-only"
                )
            return await self.invoke(
                root,
                target,
                service,
                params["method"],
                params.get("params", {}),
                owner=runtime.name,
                chain=chain,
            )
        raise PluginError("method_not_found", method)

    async def runtime_validate(self, name, candidate):
        import tempfile

        if self.state.settings.backend != "sqlite":
            raise PluginError(
                "runtime_validation_unsupported",
                "Use a separate application and Core database for runtime validation",
            )
        proposed = {**(await self.selected()), name: candidate}
        graph = Graph(proposed, self.composition.bindings)
        if any(
            definition.runtime_validation == "separate_application"
            for definition in graph.definitions.values()
        ):
            raise PluginError(
                "runtime_validation_unsupported",
                "This composition requires a separately provisioned test application; use preflight validation on the live application",
            )
        with tempfile.TemporaryDirectory(prefix="toolframe-runtime-") as temporary:
            home = Path(temporary)
            composition = self.composition.model_copy(deep=True)
            composition.application.id = str(uuid.uuid4())
            composition.plugins[name].enabled = True
            atomic_write(
                home / "toolframe.toml", tomlkit.dumps(composition.model_dump())
            )
            scratch = await Host.create(
                home, scratch=True, state_settings=StateSettings()
            )
            cloned = copy.deepcopy(proposed)
            for item in cloned.values():
                item["artifact"] = str(self.home / item["artifact"])
                item["environment"]["python"] = str(
                    self.home / item["environment"]["python"]
                )
                item.pop("ui_environment", None)
                item.pop("web_environment", None)
            try:
                await scratch.start(apply=False)
                operation, _ = await scratch.state.new_operation(
                    "runtime-validation", name, {}
                )
                await scratch.replace(cloned, set(cloned), operation["id"])
                if name not in scratch.runtimes or not scratch.runtimes[name].admitted:
                    raise PluginError(
                        "runtime_validation_failed",
                        "Scratch dependency cohort did not become ready",
                    )
            finally:
                await scratch.close()

    async def _health_loop(self):
        while True:
            await asyncio.sleep(self.composition.runtime.health_interval)
            for name, runtime in list(self.runtimes.items()):
                # Preparation may take minutes. Keep probing unrelated admitted
                # workers while the mutation writer owns its operation lock.
                members = self.graph.closure(
                    name,
                    optional=True,
                    available=self.available_dependencies(),
                )
                if any(
                    member in self.gated
                    or member not in self.runtimes
                    or not self.runtimes[member].admitted
                    for member in members
                ):
                    continue
                try:
                    health = await self.probe(runtime)
                    if (
                        self.runtimes.get(name) is not runtime
                        or name in self.gated
                        or not runtime.admitted
                    ):
                        continue
                    if health.get("runtime") != "ready" or health.get("business") in (
                        "failed",
                        "not_ready",
                    ):
                        raise PluginError(
                            "health_failed", "Worker probe is not ready", health
                        )
                    runtime.health, runtime.observed = health, time.time()
                    (
                        await self.record(
                            runtime, "active" if runtime.admitted else "gated"
                        )
                    )
                except Exception as error:  # noqa: BLE001 - failed workers stay repairable via core
                    if (
                        self.runtimes.get(name) is not runtime
                        or name in self.gated
                        or not runtime.admitted
                    ):
                        continue
                    runtime.health = (
                        {**error.details, "error": str(error)}
                        if isinstance(error, PluginError)
                        and error.code == "health_failed"
                        else {"runtime": "failed", "error": str(error)}
                    )
                    runtime.observed = time.time()
                    affected = self.graph.dependents({name})
                    self.gated.update(affected)
                    for member in affected:
                        if member in self.runtimes:
                            self.runtimes[member].admitted = False
                            (
                                await self.record(
                                    self.runtimes[member],
                                    "failed" if member == name else "blocked",
                                )
                            )
                    self.health_publication_pending = True
            if self.health_publication_pending and not self.mutation.locked():
                async with self.mutation:
                    await self.publish()

    async def _exit_loop(self):
        while True:
            await asyncio.sleep(0.1)
            if self.mutation.locked():
                continue
            for root_id in list(self.failed_roots):
                root = self.roots[root_id]
                try:
                    await self.end_root(root)
                except Exception:  # noqa: BLE001 - lease remains held for a later cleanup acknowledgement
                    (
                        await self.state.set_meta(
                            "cleanup_pending", sorted(self.failed_roots)
                        )
                    )
                    continue
                runtime = self.runtimes.get(root.owner)
                if (
                    runtime
                    and runtime.admitted
                    and root.owner not in self.cleanup_blocked
                ):
                    self.gated.discard(root.owner)
            failed = {
                name
                for name, runtime in self.runtimes.items()
                if runtime.admitted and runtime.worker.process.returncode is not None
            }
            if failed:
                for name in self.graph.dependents(failed):
                    self.gated.add(name)
                    if runtime := self.runtimes.get(name):
                        runtime.admitted = False
                        runtime.health = {
                            "runtime": "failed",
                            "error": "Worker or required dependency exited",
                        }
                        (
                            await self.record(
                                runtime, "failed" if name in failed else "blocked"
                            )
                        )
                await self.publish()

    async def _watch_loop(self):
        seen, pending = {}, {}
        while True:
            await asyncio.sleep(self.composition.runtime.watch_debounce_ms / 1000)
            if self.mutation.locked():
                continue
            for name, request in list(self.composition.plugins.items()):
                if not request.watch or request.source.startswith(
                    ("git+", "https://", "pypi:")
                ):
                    continue
                root = (self.home / request.source.removeprefix("path:")).resolve()
                if not root.is_dir():
                    continue
                try:
                    current = digest(await asyncio.to_thread(source_files, root))
                except OSError, PluginError:
                    continue
                if name not in seen:
                    candidate_id = (await self.state.plugin(name))["last_candidate"]
                    seen[name] = (
                        (await self.state.candidate(candidate_id))["source"].get(
                            "source_hash", current
                        )
                        if candidate_id
                        else current
                    )
                if current == seen[name]:
                    pending.pop(name, None)
                    continue
                observed, since = pending.get(name, (None, 0))
                if current != observed:
                    pending[name] = (current, time.monotonic())
                    continue
                if (
                    time.monotonic() - since
                    < self.composition.runtime.watch_debounce_ms / 1000
                ):
                    continue
                seen[name] = current
                pending.pop(name, None)
                action = (
                    "reload"
                    if request.enabled
                    and not (await self.state.plugin(name))["stopped"]
                    else "validate"
                )
                (await self.submit(action, {"id": name, "watch": True}))

    def redact_composition(self, value):
        """Protect the exported snapshot even before desired state is refreshed."""
        secrets = set()
        plugins = value.get("plugins", {})
        if isinstance(plugins, dict):
            for name, request in plugins.items():
                definition = self.graph.definitions.get(name)
                if isinstance(request, dict) and definition:
                    secrets.update(
                        secret_values(
                            request.get("config", {}), definition.config_schema
                        )
                    )
        return self.redact(redact(value, secrets))

    def redact(self, value):
        secrets = {self.token}
        for name, request in self.composition.plugins.items():
            definition = self.graph.definitions.get(name)
            secrets.update(
                secret_values(
                    request.config, definition.config_schema if definition else {}
                )
            )
        secrets.update(self.state.secrets)
        return redact(value, secrets)

    def available_dependencies(self):
        return {name for name in self.graph.definitions if self.healthy_runtime(name)}

    def healthy_runtime(self, name):
        if self.state.failed:
            return False
        runtime = self.runtimes.get(name)
        return bool(
            runtime
            and runtime.admitted
            and name not in self.gated
            and time.time() - runtime.observed <= self.composition.runtime.health_ttl
            and runtime.health.get("runtime") == "ready"
            and runtime.health.get("business") not in ("failed", "not_ready")
            and runtime.worker.process.returncode is None
        )

    async def status(self):
        try:
            self.config.read()
            self.configuration_error = None
        except PluginError as error:
            self.configuration_error = error.as_dict()
        plugins = []
        candidates = {}
        for name in sorted(
            set(self.composition.plugins)
            | {row["id"] for row in (await self.state.plugins())}
        ):
            row = await self.state.plugin(name)
            request = self.composition.plugins.get(name)
            runtime = self.runtimes.get(name)
            candidate = (
                (await self.state.candidate(row["last_candidate"]))
                if row["last_candidate"]
                else None
            )
            if candidate:
                candidates[name] = candidate
            plugins.append(
                {
                    "id": name,
                    "desired": request.model_dump()
                    if request
                    else {"installed": False},
                    "installed": row["last_candidate"],
                    "active": runtime.candidate["id"]
                    if runtime and runtime.admitted and name not in self.gated
                    else None,
                    "active_status": {
                        "version": runtime.candidate["version"],
                        "artifact_hash": runtime.candidate["artifact_hash"],
                        "environment": runtime.candidate["environment"]["id"],
                        "generation": runtime.generation,
                    }
                    if runtime
                    else None,
                    "source_condition": "not_checked",
                    "candidate": row["last_candidate"],
                    "candidate_status": {
                        key: candidate[key]
                        for key in (
                            "distribution",
                            "version",
                            "artifact_hash",
                            "source",
                            "environment",
                            "validation",
                        )
                    }
                    if candidate
                    else None,
                    "runtime": row["runtime"],
                    "stopped_override": row["stopped"],
                    "blocked_by": self.graph.missing.get(name, []),
                    "health_fresh": bool(
                        runtime
                        and time.time() - runtime.observed
                        <= self.composition.runtime.health_ttl
                    ),
                    "healthy": self.healthy_runtime(name),
                }
            )
        available = set()
        for name, runtime in self.runtimes.items():
            if self.healthy_runtime(name):
                available.add(f"plugin:{name}")
                for service in self.graph.definitions[name].services:
                    available.add(f"service:{service}")
                for resource in self.graph.definitions[name].resources:
                    kind = {
                        "dashboard.page": "dashboard",
                        "web.app": "web",
                        "mcp.tool": "mcp",
                        "llms.document": "llms",
                    }[resource.kind]
                    if self.publication.get(kind, {}).get(
                        "state"
                    ) == "available" and self.healthy_runtime(
                        self.graph.providers.get(kind)
                    ):
                        available.add(f"{name}/{resource.kind}/{resource.id}")
        missing = sorted(set(self.composition.required_capabilities) - available)
        needed_presentations = {
            {
                "dashboard.page": "dashboard",
                "web.app": "web",
                "mcp.tool": "mcp",
                "llms.document": "llms",
            }[resource.resource.kind]
            for resource in self.graph.resources()
        }
        degraded = any(
            self.publication.get(kind, {}).get("state") != "available"
            or not self.healthy_runtime(self.graph.providers.get(kind))
            for kind in needed_presentations
        ) or any(
            request.enabled and not self.healthy_runtime(name)
            for name, request in self.composition.plugins.items()
        )
        ready = not (
            missing
            or self.configuration_error
            or self.orphans
            or (await self.state.meta("forward_repair"))
        )
        cordis = self.manager.snapshot()
        return self.redact(
            {
                "report_schema": 1,
                "core": "ready",
                "access": await self.access.mode(),
                "mcp_access": self.mcp_access.status(),
                "ready": ready,
                "degraded": degraded,
                "configuration_error": self.configuration_error,
                "orphaned_workers": self.orphans,
                "forward_repair": (await self.state.meta("forward_repair")),
                "stores": self.stores.describe(self.graph),
                "host_run": self.host_run,
                "pid": os.getpid(),
                "desired_revision": self.config.revision,
                "observed_revision": (await self.state.meta("desired_revision")),
                "catalog_epoch": self.epoch,
                "application": "not_ready"
                if not ready
                else "degraded"
                if degraded
                else "ready",
                "missing_required": missing,
                "plugins": plugins,
                "presentations": self.publication,
                "operations": (await self.state.operations()),
                "cordis": cordis,
                "dependency_graph": dependency_snapshot(
                    self.graph,
                    plugins,
                    candidates,
                    self.composition.bindings,
                    cordis,
                    self.publication,
                ),
            }
        )
