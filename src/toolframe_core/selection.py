"""Choose index versions across the existing worker and presentation boundaries."""

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from toolframe_sdk import digest

from .graph import Graph
from .packages import floating_index_source, pin_candidate


def cohorts(candidates, bindings, enabled):
    graph = Graph(
        {name: value for name, value in candidates.items() if name in enabled}, bindings
    )
    for kind, extra, resource_kind in (
        ("dashboard", "ui", "dashboard.page"),
        ("web", "web", "web.app"),
    ):
        provider = graph.providers.get(kind)
        if provider:
            yield (
                provider,
                extra,
                {
                    name: candidates[name]
                    for name, definition in graph.definitions.items()
                    if name == provider
                    or any(r.kind == resource_kind for r in definition.resources)
                },
            )


async def prepare_selection(
    packages,
    requests,
    bindings,
    *,
    fixed=None,
    enabled=None,
    pin=False,
    artifacts=None,
):
    """Backtrack within declared ranges without combining unrelated workers.

    Each worker resolves independently. Only actual Dashboard/Web cohorts share
    an extras solve. Their selected ceilings feed back into the worker solves;
    ceilings only decrease, until every boundary accepts the same plugin wheels.
    Fixed candidates and explicit pins/custom sources never become floating.
    """
    candidates = dict(fixed or {})
    enabled = set(enabled) if enabled is not None else set(candidates) | set(requests)
    artifacts = artifacts or {}
    for name, request in requests.items():
        candidates[name] = await packages.prepare(
            name, request.source, request.config, artifact=artifacts.get(name)
        )
    while True:
        changed = {}
        for _, extra, cohort in cohorts(candidates, bindings, enabled):
            flexible = {
                name: requests[name].source
                for name in cohort
                if name in requests and floating_index_source(requests[name].source)
            }
            if not flexible:
                continue
            requirements = []
            for name, candidate in cohort.items():
                if name in flexible:
                    requirement = Requirement(flexible[name][5:])
                    requirement.specifier &= SpecifierSet("<=" + candidate["version"])
                    requirement.extras |= {extra}
                    requirements.append(str(requirement))
                else:
                    extras = {extra} if extra in candidate.get("extras", []) else set()
                    source = candidate["source"]["request"]
                    if source.startswith("pypi:"):
                        extras |= Requirement(source[5:]).extras
                    suffix = "[" + ",".join(sorted(extras)) + "]" if extras else ""
                    path = packages.home / candidate["artifact"]
                    requirements.append(
                        f"{candidate['distribution']}{suffix} @ {path.as_uri()}"
                    )
            document = await packages.resolve(
                requirements,
                stable=[candidates[name]["distribution"] for name in flexible],
            )
            versions = {
                canonicalize_name(item["name"]): str(item["version"])
                for item in document["packages"]
            }
            for name, source in flexible.items():
                version = versions[candidates[name]["distribution"]]
                if version != candidates[name]["version"]:
                    requirement = Requirement(source[5:])
                    requirement.specifier &= SpecifierSet("<=" + version)
                    candidate = await packages.prepare(
                        name, "pypi:" + str(requirement), requests[name].config
                    )
                    candidate["source"]["request"] = source
                    candidate["id"] = digest(
                        {
                            key: value
                            for key, value in candidate.items()
                            if key not in {"id", "validation"}
                        }
                    )
                    changed[name] = candidate
        if not changed:
            break
        candidates.update(changed)
    if pin:
        for name, request in requests.items():
            candidates[name] = pin_candidate(candidates[name], request.source)
    for provider, extra, cohort in cohorts(candidates, bindings, enabled):
        environment = await packages.cohort_environment(
            list(cohort.values()), extra=extra
        )
        identities = {name: value["id"] for name, value in cohort.items()}
        # A staged page can retain its future UI lock without restaging the
        # unchanged provider (which may have a separate pending update).
        for name in {provider} | (set(cohort) & set(requests)):
            candidates[name] = {
                **candidates[name],
                extra + "_environment": environment,
                extra + "_cohort": identities,
            }
    return {name: candidates[name] for name in requests}
