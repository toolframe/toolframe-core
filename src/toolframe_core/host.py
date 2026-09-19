"""Persistent Cordis host; plugin implementation code only runs in workers."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from fastapi_cordis import Plugin as CordisPlugin
from fastapi_cordis import PluginManager
from toolframe_sdk import PluginError, digest

from .config import ConfigFile, HostLock, PluginRequest, atomic_write, management_token
from .graph import Graph
from .packages import Packages, source_files
from .process import WorkerProcess
from .state import CoreState


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


@dataclass
class Root:
    id: str
    owner: str
    deadline: float
    runtimes: dict
    leases: ExitStack
    paths: dict = field(default_factory=dict)
    read_only: bool = False


class Host:
    def __init__(self, home, *, scratch=False):
        self.home = Path(home).resolve()
        self.config = ConfigFile(self.home)
        self.composition, self.revision = self.config.read()
        self.initial_application = self.composition.application.model_dump()
        self.host_run = uuid.uuid4().hex
        self.lock = HostLock(self.home)
        self.state = CoreState(self.home)
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
        self.scratch = scratch
        self.epoch = None
        self.publication = {}
        self.gateway_sessions = {}
        self.startup_error = None

    async def start(self, *, apply=True):
        self.lock.acquire()
        self.state.reconcile_startup(self.host_run)
        await self.manager.start()
        if apply:
            operation = self.submit("apply", {})
            await self.wait_operation(operation["id"])
        if not self.scratch:
            self.tasks = [
                asyncio.create_task(self._health_loop()),
                asyncio.create_task(self._watch_loop()),
            ]

    async def close(self):
        self.closing = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for operation in self.operations.values():
            if not operation.done():
                await asyncio.shield(operation)
        self.gated.update(self.runtimes)
        try:
            await self._drain(
                set(self.runtimes), self.composition.runtime.drain_timeout
            )
            await self.manager.close()
        finally:
            self.lock.close()
            self.state.close()

    def submit(self, action, params, *, idempotency=None):
        if self.closing:
            raise PluginError("host_closing", "Core is shutting down")
        operation, created = self.state.new_operation(
            action, params.get("id"), params, idempotency
        )
        if created:
            self.operations[operation["id"]] = asyncio.create_task(
                self._operate(operation)
            )
        return operation

    async def wait_operation(self, identifier):
        task = self.operations.get(identifier)
        if task:
            await asyncio.shield(task)
        return self.state.operation(identifier)

    def phase(self, operation, phase, result=None):
        self.state.update_operation(operation, phase=phase, result=result)

    def check_cancel(self, operation):
        if operation in self.cancellations:
            raise PluginError("cancelled", "Operation was cancelled before disposal")

    async def _operate(self, operation):
        identifier = operation["id"]
        async with self.mutation:
            self.state.update_operation(identifier, status="running", phase="planning")
            try:
                self.check_cancel(identifier)
                result = await self._dispatch(
                    operation["kind"], operation["request"], identifier
                )
                self.state.update_operation(
                    identifier, status="succeeded", phase="complete", result=result
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
                self.state.update_operation(identifier, status=status, error=detail)
            finally:
                self.cancellations.discard(identifier)

    def refresh(self):
        composition, revision = self.config.read()
        if composition.application.model_dump() != self.initial_application:
            raise PluginError(
                "restart_required",
                "Application identity/listen configuration changed; restart core explicitly",
            )
        self.composition, self.revision = composition, revision
        self.packages.settings = composition.packages
        self._package_overrides()
        self.manager.drain_timeout = composition.runtime.drain_timeout

    def _package_overrides(self):
        wheelhouse = os.environ.get("TOOLFRAME_WHEELHOUSE")
        if wheelhouse and wheelhouse not in self.packages.settings.find_links:
            self.packages.settings.find_links.append(wheelhouse)
        if os.environ.get("TOOLFRAME_OFFLINE") == "1":
            self.packages.settings.offline = True

    async def _prepare(self, name, operation):
        self.phase(operation, "capture_validate", {"plugin_id": name})
        request = self.composition.plugins[name]
        candidate = await self.packages.prepare(name, request.source, request.config)
        if self.config.revision != self.revision:
            raise PluginError(
                "stale_plan", "Composition changed while preparing the candidate"
            )
        self.state.save_candidate(candidate)
        return candidate

    def selected(self):
        selected = {}
        for name, request in self.composition.plugins.items():
            if request.enabled and not self.state.plugin(name)["stopped"]:
                stored = self.state.plugin(name)
                candidate = stored["current_candidate"] or stored["last_candidate"]
                if candidate:
                    selected[name] = self.state.candidate(candidate)
        return selected

    async def _dispatch(self, action, params, operation):
        self.refresh()
        name = params.get("id")
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
            self.composition, self.revision = self.config.put_plugin(
                name, request, self.revision
            )
            self.state.save_candidate(candidate)
            if request.enabled:
                proposed = self.selected()
                proposed[name] = candidate
                await self.replace(proposed, {name}, operation)
            self.write_lock()
            return {
                "plugin_id": name,
                "candidate": candidate["id"],
                "enabled": request.enabled,
            }
        if (
            action
            in (
                "validate",
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
        if action == "validate":
            candidate = await self._prepare(name, operation)
            if params.get("runtime"):
                await self.runtime_validate(name, candidate)
                candidate["validation"]["runtime"] = "passed_scratch"
                self.state.save_candidate(candidate)
            self.write_lock()
            return {"candidate": candidate["id"], "validation": candidate["validation"]}
        if action == "stop" and params.get("force"):
            affected = self.graph.affected(self.graph, {name})
            self.gated.update(affected)
            for member in affected:
                runtime = self.runtimes.get(member)
                if runtime:
                    runtime.admitted = False
                    await runtime.worker.force_stop()
                self.state.stopped(member, True)
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
            storage = self._storage_runtime(name)
            plan = await self.admin(storage, "plan", {"owner": name, "schema": schema})
            if action == "migrate-plan":
                self.state.set_meta(
                    f"migration/{name}",
                    {
                        "plan": plan,
                        "candidate": candidate["id"],
                        "revision": self.revision,
                    },
                )
                return {
                    **plan,
                    "candidate": candidate["id"],
                    "desired_revision": self.revision,
                }
            saved = self.state.meta(f"migration/{name}")
            if (
                not saved
                or params.get("plan_id") != plan["plan_id"]
                or saved["candidate"] != candidate["id"]
                or saved["revision"] != self.revision
            ):
                raise PluginError(
                    "stale_plan", "Review a current migration plan before applying"
                )
            proposed = self.selected()
            proposed[name] = candidate
            await self.replace(
                proposed,
                {name},
                operation,
                migration={"owner": name, "schema": schema, "plan_id": plan["plan_id"]},
            )
            return {"migration": plan["plan_id"], "candidate": candidate["id"]}
        if action in ("reload", "enable", "disable", "remove", "stop"):
            if action == "stop":
                self.state.stopped(name, True)
            if action in ("enable", "disable", "remove"):

                def edit(document):
                    if action == "remove":
                        del document["plugins"][name]
                    else:
                        document["plugins"][name]["enabled"] = action == "enable"

                self.composition, self.revision = self.config.edit(self.revision, edit)
            proposed = self.selected()
            if action in ("reload", "enable"):
                self.state.stopped(name, False)
                candidate = await self._prepare(name, operation)
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
                self.state.stopped(member, False)
                existing = self.state.plugin(member)
                previous = existing["current_candidate"] or existing["last_candidate"]
                candidate = self.state.candidate(previous) if previous else None
                if (
                    not candidate
                    or candidate["source"]["request"] != request.source
                    or candidate["config"] != request.config
                ):
                    candidate = await self._prepare(member, operation)
                if request.enabled:
                    proposed[member] = candidate
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

    def _storage_runtime(self, owner):
        binding = next(
            (
                item
                for item in self.graph.bindings.get(owner, {}).values()
                if item[1] == "toolframe.storage.sql"
            ),
            None,
        )
        if not binding or binding[0] not in self.runtimes:
            raise PluginError(
                "storage_unavailable",
                "The owner's declared storage provider must be active",
            )
        return self.runtimes[binding[0]]

    async def admin(self, storage, method, params):
        return await storage.worker.request(
            "runtime.invoke",
            {
                "service": "toolframe.storage.admin",
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

    async def _activate(self, name, context):
        candidate = self.graph.candidates[name]
        generation = uuid.uuid4().hex
        runtime = Runtime(name, generation, candidate)
        self.runtimes[name] = runtime
        definition = self.graph.definitions[name]
        # Cordis cleanup owns even partially activated workers.
        context.on_dispose(self._dispose, runtime)
        environment = candidate.get("ui_environment", candidate["environment"])
        runtime.worker = await WorkerProcess.start(
            self.home / environment["python"],
            candidate["entrypoint"],
            lambda method, params: self.worker_call(runtime, method, params),
            cwd=self.home,
        )
        await runtime.worker.request("runtime.describe")
        if definition.store:
            await self.admin(
                self._storage_runtime(name),
                "ensure_store",
                {"owner": name, "schema": definition.store.model_dump()},
            )
        runtime.health = await self.probe(
            runtime,
            "runtime.activate",
            {
                "generation": generation,
                "config": candidate["config"],
                "state_directory": str(self.home / "data/plugins")
                if definition.provider_kind == "storage"
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
        self.record(runtime, "prepared")
        return runtime

    async def probe(self, runtime, method="runtime.health", params=None):
        members = self.graph.closure(runtime.name, optional=True)
        leases = ExitStack()
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
            leases.close()

    async def _dispose(self, runtime):
        runtime.admitted = False
        for session in list(self.gateway_sessions.get(runtime.name, ())):
            session.cancel()
        try:
            if runtime.worker:
                if (
                    runtime.worker.expected_exit
                    and runtime.worker.process.returncode is not None
                ):
                    await runtime.worker.peer.close()
                else:
                    await runtime.worker.close(
                        timeout=self.composition.runtime.disposal_timeout
                    )
        except BaseException:
            self.record(runtime, "cleanup_failed")
            raise
        else:
            if self.runtimes.get(runtime.name) is runtime:
                self.runtimes.pop(runtime.name)
            self.record(runtime, "stopped")

    def record(self, runtime, state):
        self.state.runtime(
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

    async def _register_set(self, names):
        for name in self.graph.order:
            if name not in names:
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
            finally:
                # Registration remains present when setup raises; cleanup/recovery must remove it.
                self.registered = {item["name"] for item in self.manager.snapshot()}
            runtime = self.runtimes.get(name)
            if (
                not self.graph.missing[name]
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
                    runtime.admitted = True
                    self.record(runtime, "active")
                    self.gated.discard(name)
                except Exception as error:  # noqa: BLE001
                    errors[name] = str(error)
        if errors:
            raise PluginError("resume_failed", "Some workers could not resume", errors)

    async def replace(self, proposed, changed, operation, *, migration=None):
        replacement = Graph(proposed, self.composition.bindings)
        old_graph = self.graph
        affected = old_graph.affected(replacement, changed)
        for name, definition in replacement.definitions.items():
            if definition.provider_kind == "dashboard" and (
                name in affected or name not in self.runtimes
            ):
                cohort = [
                    candidate
                    for owner, candidate in proposed.items()
                    if owner == name
                    or any(
                        resource.kind == "dashboard.page"
                        for resource in replacement.definitions[owner].resources
                    )
                ]
                proposed[name] = {
                    **proposed[name],
                    "ui_environment": await self.packages.environment(cohort, ui=True),
                }
        replacement = Graph(proposed, self.composition.bindings)
        # Existing stores are inspected before quiescence. A schema change is never hidden in reload.
        for name in changed & replacement.candidates.keys():
            definition = replacement.definitions[name]
            if definition.store and name in self.runtimes:
                plan = await self.admin(
                    self._storage_runtime(name),
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
        self.phase(
            operation,
            "quiesce",
            {
                "affected": sorted(affected),
                "old": {
                    name: value["id"] for name, value in old_graph.candidates.items()
                },
                "candidate": {name: value["id"] for name, value in proposed.items()},
                "revision": self.revision,
            },
        )
        self.gated.update(affected)
        try:
            for name in affected:
                if name in self.runtimes:
                    self.runtimes[name].admitted = False
                    await self.runtimes[name].worker.request("runtime.quiesce")
            self.phase(operation, "draining")
            await self._drain(
                affected, self.composition.runtime.drain_timeout, operation
            )
            self.check_cancel(operation)
        except BaseException:
            await self._resume(affected)
            raise
        self.phase(operation, "disposing")
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
        schema_committed = False
        try:
            if migration:
                self.phase(operation, "migrating")
                storage = self._storage_runtime(migration["owner"])
                result = await self.admin(storage, "apply", migration)
                schema_committed = result["committed"]
                self.phase(operation, "migration_committed", result)
            self.graph = replacement
            self.phase(operation, "activating")
            await self._register_set(set(proposed) - self.registered)
            self.phase(operation, "publishing")
            await self.publish()
            for name, candidate in proposed.items():
                self.state.save_candidate(candidate)
                if self.state.plugin(name)["current_candidate"] != candidate["id"]:
                    self.state.select(name, candidate["id"])
            self.write_lock()
            await self._resume(affected | (set(proposed) - set(old_graph.candidates)))
            self.gated.difference_update(affected - self.runtimes.keys())
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
            if schema_committed:
                raise PluginError(
                    "forward_repair_required",
                    "Schema committed; data retained and old code was not restarted",
                    {"error": str(failure)},
                ) from failure
            self.graph = old_graph
            recover = affected & old_graph.candidates.keys()
            if not recover:
                raise
            self.phase(operation, "recovering")
            try:
                await self._register_set(recover - self.registered)
                await self.publish()
                await self._resume(recover)
                for name in recover:
                    old = old_graph.candidates[name]
                    if self.state.plugin(name)["current_candidate"] != old["id"]:
                        self.state.select(name, old["id"])
                self.write_lock()
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

    async def publish(self):
        epoch = uuid.uuid4().hex
        generations = {
            name: runtime.generation for name, runtime in self.runtimes.items()
        }
        resources = [
            resource
            for resource in self.graph.resources(generations)
            if resource.owner in generations
            and self.runtimes[resource.owner].health.get("runtime") == "ready"
        ]
        self.epoch = epoch
        self.state.set_meta(
            "catalog",
            {
                "epoch": epoch,
                "resources": [resource.model_dump() for resource in resources],
            },
        )
        for kind, resource_kind in (
            ("dashboard", "dashboard.page"),
            ("mcp", "mcp.tool"),
            ("llms", "llms.document"),
        ):
            name = self.graph.providers.get(kind)
            runtime = self.runtimes.get(name)
            if not runtime:
                self.publication[kind] = {"state": "unavailable", "epoch": epoch}
                continue
            try:
                await runtime.worker.request(
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
                result = await runtime.worker.request(
                    "provider.commit", {"epoch": epoch}
                )
                if result.get("epoch") != epoch:
                    raise PluginError(
                        "epoch_mismatch",
                        "Provider did not acknowledge selected catalog",
                    )
                runtime.epoch = epoch
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

    def write_lock(self):
        plugins = {}
        for name in self.composition.plugins:
            row = self.state.plugin(name)
            candidates = {}
            for selection in (
                "current_candidate",
                "last_candidate",
                "previous_candidate",
            ):
                if row[selection]:
                    candidate = self.state.candidate(row[selection])
                    candidates[selection] = {
                        key: candidate[key]
                        for key in (
                            "id",
                            "version",
                            "artifact",
                            "artifact_hash",
                            "source",
                            "environment",
                        )
                    }
            plugins[name] = candidates
        atomic_write(
            self.home / "toolframe.lock",
            json.dumps(
                {"format": 1, "desired_revision": self.revision, "plugins": plugins},
                indent=2,
            )
            + "\n",
        )
        self.state.set_meta("desired_revision", self.revision)

    def begin_root(self, owner, *, deadline=None):
        if (
            owner in self.gated
            or owner not in self.runtimes
            or not self.runtimes[owner].admitted
        ):
            raise PluginError("plugin_reloading", f"{owner} is not admitting new calls")
        members = self.graph.closure(owner, optional=True)
        leases = ExitStack()
        try:
            for name in members:
                if name in self.gated or name not in self.runtimes:
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
            )
            self.roots[root.id] = root
            return root
        except BaseException:
            leases.close()
            raise

    async def end_root(self, root):
        if self.roots.pop(root.id, None) is None:
            return
        try:
            for runtime in root.runtimes.values():
                if (
                    runtime.candidate["definition"].get("provider_kind") == "storage"
                    and runtime.worker.process.returncode is None
                ):
                    await self.admin(runtime, "cleanup_root", {"root_id": root.id})
        finally:
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
        if root.read_only and (not specification or not specification.get("read_only")):
            raise PluginError(
                "probe_write_forbidden",
                "Activation and health probes may only call declared read-only methods",
            )
        path = (*chain, target)
        root.paths[path] = root.paths.get(path, 0) + 1
        source = root.runtimes.get(owner or target, runtime)
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
                    },
                },
                timeout=max(0.01, root.deadline - time.time()),
            )
        finally:
            root.paths[path] -= 1
            if not root.paths[path]:
                del root.paths[path]

    async def call_service(self, owner, service, method, params):
        root = self.begin_root(owner)
        try:
            return await self.invoke(root, owner, service, method, params)
        finally:
            await self.end_root(root)

    async def worker_call(self, runtime, method, params):
        if self.runtimes.get(runtime.name) is not runtime:
            raise PluginError(
                "stale_generation", "Worker generation is no longer current"
            )
        if method == "management.call":
            if runtime.candidate["definition"].get("provider_kind") != "dashboard":
                raise PluginError(
                    "forbidden",
                    "Only the management dashboard may request management operations",
                )
            action = params["action"]
            if action == "status":
                return self.status()
            if action == "operation":
                return self.state.operation(params["params"]["id"])
            if action == "cancel":
                identifier = params["params"]["id"]
                self.state.operation(identifier)
                self.cancellations.add(identifier)
                self.changed.set()
                return {"id": identifier, "requested": True}
            return self.submit(action, params.get("params", {}))
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
                or {"mcp": "mcp.tool", "dashboard": "dashboard.page"}.get(kind)
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
                version = 1
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
            if self.graph.definitions[target].services[service].version != version:
                raise PluginError(
                    "service_version_mismatch",
                    "Requested service version is unavailable",
                )
            root = self.begin_root(resource.owner)
            try:
                return await self.invoke(
                    root, target, service, name, params.get("params", {})
                )
            finally:
                await self.end_root(root)
        if method == "task.begin":
            root = self.begin_root(runtime.name)
            root.paths[(runtime.name,)] = 1
            return {
                "root_id": root.id,
                "deadline": root.deadline,
                "chain": [runtime.name],
            }
        if method == "task.end":
            root = self.roots.get(params["root_id"])
            if root and root.owner == runtime.name:
                await self.end_root(root)
            return {}
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
            binding = self.graph.bindings.get(runtime.name, {}).get(params["alias"])
            if not binding:
                raise PluginError(
                    "undeclared_dependency", "Dependency alias was not bound"
                )
            target, service = binding
            if service == "toolframe.storage.admin":
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

        with tempfile.TemporaryDirectory(prefix="toolframe-runtime-") as temporary:
            home = Path(temporary)
            composition = self.composition.model_copy(deep=True)
            composition.plugins[name].enabled = True
            atomic_write(
                home / "toolframe.toml", tomlkit.dumps(composition.model_dump())
            )
            scratch = Host(home, scratch=True)
            proposed = self.selected()
            proposed[name] = candidate
            cloned = copy.deepcopy(proposed)
            for item in cloned.values():
                item["artifact"] = str(self.home / item["artifact"])
                item["environment"]["python"] = str(
                    self.home / item["environment"]["python"]
                )
                item.pop("ui_environment", None)
            try:
                await scratch.start(apply=False)
                operation, _ = scratch.state.new_operation(
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
            if self.mutation.locked():
                continue
            for name, runtime in list(self.runtimes.items()):
                try:
                    health = await self.probe(runtime)
                    if health.get("runtime") != "ready":
                        raise PluginError(
                            "health_failed", "Worker probe is not ready", health
                        )
                    runtime.health, runtime.observed = health, time.time()
                    self.record(runtime, "active" if runtime.admitted else "gated")
                except Exception as error:  # noqa: BLE001 - failed workers stay repairable via core
                    runtime.health = {"runtime": "failed", "error": str(error)}
                    runtime.observed = time.time()
                    affected = self.graph.dependents({name})
                    self.gated.update(affected)
                    for member in affected:
                        if member in self.runtimes:
                            self.runtimes[member].admitted = False
                            self.record(
                                self.runtimes[member],
                                "failed" if member == name else "blocked",
                            )
                    await self.publish()

    async def _watch_loop(self):
        seen = {}
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
                previous = seen.get(name)
                seen[name] = current
                if previous is None or current == previous:
                    continue
                action = (
                    "reload"
                    if request.enabled and not self.state.plugin(name)["stopped"]
                    else "validate"
                )
                self.submit(action, {"id": name, "watch": True})

    def status(self):
        plugins = []
        for name in sorted(
            set(self.composition.plugins) | {row["id"] for row in self.state.plugins()}
        ):
            row = self.state.plugin(name)
            request = self.composition.plugins.get(name)
            runtime = self.runtimes.get(name)
            plugins.append(
                {
                    "id": name,
                    "desired": request.model_dump()
                    if request
                    else {"installed": False},
                    "installed": row["last_candidate"],
                    "active": runtime.candidate["id"]
                    if runtime and runtime.admitted
                    else None,
                    "candidate": row["last_candidate"],
                    "runtime": row["runtime"],
                    "stopped_override": row["stopped"],
                    "blocked_by": self.graph.missing.get(name, []),
                    "health_fresh": bool(
                        runtime
                        and time.time() - runtime.observed
                        <= self.composition.runtime.health_ttl
                    ),
                }
            )
        available = set()
        for name, runtime in self.runtimes.items():
            if (
                runtime.admitted
                and name not in self.gated
                and time.time() - runtime.observed
                <= self.composition.runtime.health_ttl
            ):
                available.add(f"plugin:{name}")
                for service in self.graph.definitions[name].services:
                    available.add(f"service:{service}")
                for resource in self.graph.definitions[name].resources:
                    kind = {
                        "dashboard.page": "dashboard",
                        "mcp.tool": "mcp",
                        "llms.document": "llms",
                    }[resource.kind]
                    if self.publication.get(kind, {}).get("state") == "available":
                        available.add(f"{name}/{resource.kind}/{resource.id}")
        missing = sorted(set(self.composition.required_capabilities) - available)
        degraded = (
            any(item["state"] != "available" for item in self.publication.values())
            if self.graph.candidates
            else False
        )
        return {
            "core": "ready",
            "host_run": self.host_run,
            "pid": os.getpid(),
            "desired_revision": self.config.revision,
            "observed_revision": self.state.meta("desired_revision"),
            "catalog_epoch": self.epoch,
            "application": "not_ready"
            if missing
            else "degraded"
            if degraded
            else "ready",
            "missing_required": missing,
            "plugins": plugins,
            "presentations": self.publication,
            "operations": self.state.operations(),
            "cordis": self.manager.snapshot(),
        }
