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
from toolframe_sdk import PluginError, Principal, digest

from .access import Access, operation_scopes
from .config import ConfigFile, HostLock, PluginRequest, atomic_write, management_token
from .diagnostics import dependency_snapshot
from .graph import Graph
from .network import bind_host
from .packages import Packages, source_files
from .process import WorkerProcess, group_exists, process_inventory, stop_group
from .redaction import redact, secret_values
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
    principal: Principal | None = None
    scope_catalog: dict = field(default_factory=dict)


class Host:
    def __init__(self, home, *, scratch=False, listen_host=None):
        self.home = Path(home).resolve()
        self.config = ConfigFile(self.home)
        self.state = CoreState(self.home)
        self.configuration_error = None
        try:
            self.composition, self.revision = self.config.read()
        except PluginError as error:
            from .config import Application, Composition

            self.configuration_error = error.as_dict()
            previous = self.state.meta("last_valid_composition")
            self.composition = (
                Composition.model_validate(previous)
                if previous
                else Composition(application=Application(id="unconfigured"))
            )
            self.revision = self.config.revision
        self.initial_application = self.composition.application.model_dump()
        self.listen_host = bind_host(
            listen_host
            if listen_host is not None
            else self.composition.application.host
        )
        self.host_run = uuid.uuid4().hex
        self.lock = HostLock(self.home)
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
        self.worker_owners = self.state.meta("worker_owners", {})
        self.starting = True
        self.transitioning = set()
        self.access = Access(self)
        self.operation_identities = {}

    async def start(self, *, apply=True, wait=True):
        self.lock.acquire()
        await self.reconcile_workers()
        self.state.reconcile_startup(self.host_run)
        self.state.set_meta("host_run", self.host_run)
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
            operation = self.submit("apply", {"startup": True})
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

    async def reconcile_workers(self):
        previous_run = self.state.meta("host_run")
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
        self.state.set_meta("worker_owners", self.worker_owners)

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
        self.state.set_meta("worker_owners", self.worker_owners)
        self.state.stopped(name, True)

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

    def submit(self, action, params, *, idempotency=None, identity=None):
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
        existing = self.state.idempotent_operation(
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
        operation, created = self.state.new_operation(
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
                self.state.update_operation(
                    identifier, status=status, error=self.redact(detail)
                )
            finally:
                self.cancellations.discard(identifier)
                self.operation_identities.pop(identifier, None)
                if operation["request"].get("startup"):
                    self.starting = False

    def refresh(self):
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
        self.state.set_meta("last_valid_composition", composition.model_dump())

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
        if params.get("startup") and self.state.meta("forward_repair"):
            raise PluginError(
                "forward_repair_required",
                "Interrupted schema operation requires explicit inspection and repair",
                self.state.meta("forward_repair"),
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
            approved = self.state.meta(f"plan/{params['plan_id']}")
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
            candidate = await self.packages.prepare(name, source, request.config)
            if source != request.source:
                self.composition, self.revision = self.config.put_plugin(
                    name, request.model_copy(update={"source": source}), self.revision
                )
            elif self.config.revision != self.revision:
                raise PluginError(
                    "stale_plan", "Composition changed while staging update"
                )
            self.state.save_candidate(candidate)
            self.write_lock()
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
                self.state.save_candidate(candidate)
            self.write_lock()
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
                candidate = (
                    self.state.candidate(
                        approved.get("target_candidate") or approved["candidates"][name]
                    )
                    if approved
                    else await self._prepare(name, operation)
                )
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
                previous = (
                    approved["candidates"].get(member)
                    if approved
                    else existing["last_candidate"] or existing["current_candidate"]
                )
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

    async def validate_candidate(self, candidate, operation):
        """Validate a hypothetical target without publishing or changing live stores."""
        name = candidate["plugin_id"]
        proposed = {**self.selected(), name: candidate}
        validation = candidate["validation"]
        stages = validation.setdefault("stages", {})
        stage = "dependency_graph"
        try:
            self.phase(operation, "preflight", {"plugin_id": name})
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
                storage_owner = next(
                    (
                        owner
                        for owner, service in graph.bindings[name].values()
                        if service == "toolframe.storage.sql"
                    ),
                    None,
                )
                if storage_owner and self.healthy_runtime(storage_owner):
                    plan = await self.admin(
                        self.runtimes[storage_owner],
                        "plan",
                        {"owner": name, "schema": definition.store.model_dump()},
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
                    blockers.setdefault(name, []).append("live_storage_history")
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
                environment = await self.packages.environment(cohort, ui=True)
                proposed[dashboard] = {
                    **proposed[dashboard],
                    "ui_environment": environment,
                }
                stages[stage] = {"status": "passed", "environment": environment["id"]}
            resources = graph.resources()
            for kind, resource_kind in {
                "dashboard": "dashboard.page",
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
                    await self.prepare_provider(proposed[provider], owned)
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
            self.state.save_candidate(candidate)

    async def plan_basis(self):
        schemas = {}
        for name, definition in self.graph.definitions.items():
            if definition.store and name in self.runtimes:
                schemas[name] = await self.admin(
                    self._storage_runtime(name), "inspect", {"owner": name}
                )
        return {
            "revision": self.config.revision,
            "generations": {
                name: item.generation for name, item in self.runtimes.items()
            },
            "epoch": self.epoch,
            "schemas": schemas,
            "stopped": sorted(
                row["id"] for row in self.state.plugins() if row["stopped"]
            ),
        }

    async def plan(self, params, operation):
        action, name = params.get("action", "apply"), params.get("id")
        if action not in {"apply", "reload", "enable", "disable", "remove", "stop"}:
            raise PluginError("invalid_request", "This action cannot be planned")
        if action != "apply" and name not in self.composition.plugins:
            raise PluginError("plugin_missing", "Plan requires a declared plugin ID")
        basis = await self.plan_basis()
        proposed = self.selected()
        if action == "apply":
            proposed = {}
            for member, request in self.composition.plugins.items():
                if request.enabled:
                    proposed[member] = await self._prepare(member, operation)
        elif action in {"reload", "enable"}:
            candidate = await self._prepare(name, operation)
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
        }
        if action in {"reload", "enable"}:
            result["target_candidate"] = candidate["id"]
        for member in changed & proposed.keys():
            definition = replacement.definitions[member]
            if definition.store and member in self.runtimes:
                result["migrations"][member] = await self.admin(
                    self._storage_runtime(member),
                    "plan",
                    {"owner": member, "schema": definition.store.model_dump()},
                )
        if basis != await self.plan_basis():
            raise PluginError("stale_plan", "Runtime changed while planning")
        result["plan_id"] = digest(result)
        self.state.set_meta(f"plan/{result['plan_id']}", result)
        return result

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

    async def change_schema(self, storage, method, params):
        owner = params["owner"]
        before = await self.admin(storage, "inspect", {"owner": owner})
        previous_repair = self.state.meta("forward_repair")
        self.state.set_meta(
            "forward_repair",
            {
                "owner": owner,
                "before": before,
                "target": params["schema"],
                "state": "in_progress",
            },
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
                    {"owner": owner, "state": "committed_or_unknown"}
                )
            else:
                self.state.set_meta("forward_repair", previous_repair)
            raise
        if result.get("committed"):
            self.schema_changed = True
            self.schema_events.append({"owner": owner, "state": "committed", **result})
        else:
            self.state.set_meta("forward_repair", previous_repair)
        return result

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
            host_token=f"{self.host_run}/{name}/{generation}",
        )
        self.worker_owners[str(runtime.worker.pid)] = {
            "name": name,
            "token": f"{self.host_run}/{name}/{generation}",
        }
        self.state.set_meta("worker_owners", self.worker_owners)
        self.record(runtime, "starting")
        await runtime.worker.request("runtime.describe")
        private_secrets = self.access.private_secrets(definition)
        if definition.store:
            await self.change_schema(
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
                "private_secrets": private_secrets,
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
        members = self.graph.closure(
            runtime.name, optional=True, available=self.runtimes
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
            self.record(runtime, "cleanup_failed")
            raise
        else:
            if runtime.worker:
                self.worker_owners.pop(str(runtime.worker.pid), None)
                self.state.set_meta("worker_owners", self.worker_owners)
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
                    not in {"dashboard", "mcp", "llms", "auth", "dashboard-auth"}
                    or name not in self.optional_new
                ):
                    raise
                # A newly installed presentation must not prevent independent
                # business services from starting. Cleanup still has to succeed.
                if name in {item["name"] for item in self.manager.snapshot()}:
                    await self.manager.unregister(name)
                self.optional_failures[name] = str(error)
                self.state.runtime(
                    name,
                    {"state": "failed", "error": str(error), "host_run": self.host_run},
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
                                self.state.set_meta(
                                    "cleanup_pending", sorted(self.failed_roots)
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
                self.record(runtime, "active")
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
        self.schema_changed = bool(self.state.meta("forward_repair"))
        self.schema_events = []
        if self.orphans:
            raise PluginError(
                "cleanup_failed",
                "Previous worker ownership has not been released; use explicit stop --force for verified survivors",
                self.orphans,
            )
        replacement = Graph(proposed, self.composition.bindings)
        old_graph = self.graph
        self.optional_new = {
            name
            for name, definition in replacement.definitions.items()
            if definition.provider_kind
            in {"dashboard", "mcp", "llms", "auth", "dashboard-auth"}
            and name not in self.runtimes
        }
        self.optional_failures = {}
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
                try:
                    cohort_ids = {item["plugin_id"]: item["id"] for item in cohort}
                    prior = proposed[name]
                    environment = prior.get("ui_environment")
                    if (
                        not environment
                        or prior.get("ui_cohort") != cohort_ids
                        or not (self.home / environment["python"]).is_file()
                    ):
                        environment = await self.packages.environment(cohort, ui=True)
                    proposed[name] = {
                        **prior,
                        "ui_environment": environment,
                        "ui_cohort": cohort_ids,
                    }
                except Exception as error:
                    if name not in self.optional_new:
                        raise
                    self.optional_failures[name] = str(error)
                    self.state.runtime(
                        name,
                        {
                            "state": "failed",
                            "error": str(error),
                            "host_run": self.host_run,
                        },
                    )
        replacement = Graph(proposed, self.composition.bindings)
        self.phase(operation, "prepare_resources")
        for kind, provider in replacement.providers.items():
            if (
                kind not in {"dashboard", "mcp", "llms"}
                or provider in self.optional_failures
            ):
                continue
            resource_kind = {
                "dashboard": "dashboard.page",
                "mcp": "mcp.tool",
                "llms": "llms.document",
            }[kind]
            if provider in affected or old_graph.descriptor_digest(
                resource_kind
            ) != replacement.descriptor_digest(resource_kind):
                try:
                    await self.prepare_provider(
                        proposed[provider],
                        [
                            resource
                            for resource in replacement.resources()
                            if resource.resource.kind == resource_kind
                        ],
                    )
                except Exception as error:
                    if provider not in self.optional_new:
                        raise
                    self.optional_failures[provider] = str(error)
                    self.state.runtime(
                        provider,
                        {
                            "state": "failed",
                            "error": str(error),
                            "host_run": self.host_run,
                        },
                    )
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
        self.transitioning.update(affected)
        try:
            for name in affected:
                if name in self.runtimes:
                    self.runtimes[name].admitted = False
                    worker = self.runtimes[name].worker
                    if worker.process.returncode is None:
                        await worker.request("runtime.quiesce")
            self.phase(operation, "draining")
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
        try:
            if migration:
                self.phase(operation, "migrating")
                storage = self._storage_runtime(migration["owner"])
                result = await self.change_schema(storage, "apply", migration)
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
            self.state.set_meta("forward_repair", None)
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
                self.state.set_meta(
                    "forward_repair",
                    {
                        "operation": operation,
                        "schemas": self.schema_events,
                        "candidates": {
                            name: item["id"] for name, item in proposed.items()
                        },
                    },
                )
                raise PluginError(
                    "forward_repair_required",
                    "Schema committed or acknowledgement is unknown; data retained and old code was not restarted",
                    {"error": str(failure), "schemas": self.schema_events},
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

        environment = candidate.get("ui_environment", candidate["environment"])
        worker = await WorkerProcess.start(
            self.home / environment["python"],
            candidate["entrypoint"],
            unavailable,
            cwd=self.home,
        )
        activated = False
        try:
            await worker.request("runtime.describe")
            await worker.request(
                "runtime.activate",
                {"generation": "preflight", "config": candidate["config"]},
            )
            activated = True
            await worker.request(
                "provider.prepare",
                {
                    "resources": [resource.model_dump() for resource in resources],
                    "epoch": "preflight",
                },
            )
            await worker.request("provider.abort", {"epoch": "preflight"})
        finally:
            try:
                await worker.close(activated=activated)
            except BaseException:
                await worker.force_stop()  # validation has no live service/data access
                raise

    async def publish(self):
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
        self.state.set_meta("last_valid_composition", self.composition.model_dump())

    def begin_root(self, owner, *, deadline=None, principal=None):
        if (
            owner in self.gated
            or owner not in self.runtimes
            or not self.runtimes[owner].admitted
        ):
            raise PluginError("plugin_reloading", f"{owner} is not admitting new calls")
        members = self.graph.closure(owner, optional=True, available=self.runtimes)
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
            for runtime in root.runtimes.values():
                if (
                    runtime.candidate["definition"].get("provider_kind") == "storage"
                    and runtime.worker.process.returncode is None
                ):
                    await self.admin(runtime, "cleanup_root", {"root_id": root.id})
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
        if self.runtimes.get(runtime.name) is not runtime:
            raise PluginError(
                "stale_generation", "Worker generation is no longer current"
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
                operation = self.state.operation(params["params"]["id"])
                scopes = operation_scopes(operation["kind"], operation["request"])
            else:
                scopes = operation_scopes(action, params.get("params"))
            await self.access.admit(
                identity, scopes, effective=False, activity=action == "cancel"
            )
            if action == "status":
                return self.status()
            if action == "operation":
                return self.redact(self.state.operation(params["params"]["id"]))
            if action == "cancel":
                identifier = params["params"]["id"]
                self.state.operation(identifier)
                self.cancellations.add(identifier)
                self.changed.set()
                return {"id": identifier, "requested": True}
            return self.redact(
                self.submit(action, params.get("params", {}), identity=identity)
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
                principal = Principal(kind="public", all_business=True)
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
        if method == "task.begin":
            async with asyncio.timeout(self.composition.runtime.activation_timeout):
                while runtime.name in self.gated or not runtime.admitted:
                    if self.runtimes.get(runtime.name) is not runtime:
                        raise PluginError(
                            "stale_generation", "Task belongs to a withdrawn generation"
                        )
                    self.changed.clear()
                    await self.changed.wait()
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
            for name, runtime in list(self.runtimes.items()):
                # Preparation may take minutes. Keep probing unrelated admitted
                # workers while the mutation writer owns its operation lock.
                members = self.graph.closure(
                    name, optional=True, available=self.runtimes
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
                    self.record(runtime, "active" if runtime.admitted else "gated")
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
                            self.record(
                                self.runtimes[member],
                                "failed" if member == name else "blocked",
                            )
                    self.health_publication_pending = True
            if self.health_publication_pending and not self.mutation.locked():
                async with self.mutation:
                    await self.publish()
                    self.health_publication_pending = False

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
                    self.state.set_meta("cleanup_pending", sorted(self.failed_roots))
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
                        self.record(runtime, "failed" if name in failed else "blocked")
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
                    candidate_id = self.state.plugin(name)["last_candidate"]
                    seen[name] = (
                        self.state.candidate(candidate_id)["source"].get(
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
                    if request.enabled and not self.state.plugin(name)["stopped"]
                    else "validate"
                )
                self.submit(action, {"id": name, "watch": True})

    def redact(self, value):
        secrets = {self.token}
        for name, request in self.composition.plugins.items():
            definition = self.graph.definitions.get(name)
            secrets.update(
                secret_values(
                    request.config, definition.config_schema if definition else {}
                )
            )
        for row in self.state.plugins():
            for key in ("last_candidate", "current_candidate", "previous_candidate"):
                if row[key]:
                    candidate = self.state.candidate(row[key])
                    secrets.update(
                        secret_values(
                            candidate.get("config", {}),
                            candidate.get("definition", {}).get("config_schema", {}),
                        )
                    )
        for operation in self.state.operations():
            secrets.update(secret_values(operation["request"]))
        return redact(value, secrets)

    def healthy_runtime(self, name):
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

    def status(self):
        try:
            self.config.read()
            self.configuration_error = None
        except PluginError as error:
            self.configuration_error = error.as_dict()
        plugins = []
        candidates = {}
        for name in sorted(
            set(self.composition.plugins) | {row["id"] for row in self.state.plugins()}
        ):
            row = self.state.plugin(name)
            request = self.composition.plugins.get(name)
            runtime = self.runtimes.get(name)
            candidate = (
                self.state.candidate(row["last_candidate"])
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
            {"dashboard.page": "dashboard", "mcp.tool": "mcp", "llms.document": "llms"}[
                resource.resource.kind
            ]
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
            or self.state.meta("forward_repair")
        )
        cordis = self.manager.snapshot()
        return self.redact(
            {
                "report_schema": 1,
                "core": "ready",
                "access": self.access.mode(),
                "ready": ready,
                "degraded": degraded,
                "configuration_error": self.configuration_error,
                "orphaned_workers": self.orphans,
                "forward_repair": self.state.meta("forward_repair"),
                "host_run": self.host_run,
                "pid": os.getpid(),
                "desired_revision": self.config.revision,
                "observed_revision": self.state.meta("desired_revision"),
                "catalog_epoch": self.epoch,
                "application": "not_ready"
                if not ready
                else "degraded"
                if degraded
                else "ready",
                "missing_required": missing,
                "plugins": plugins,
                "presentations": self.publication,
                "operations": self.state.operations(),
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
