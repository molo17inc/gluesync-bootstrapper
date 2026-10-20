"""The function collection of a pipeline (Gluesync 2.3.0.0+): backup, restore and the check of the functions a column binds.

A function collection is a set of Java sources scoped to one pipeline. Its ``@GsFunction`` methods are what every UDF and
every Custom Field Function (``expression.type: CustomFn``) of the pipeline can call, and the target agent refuses any
entity whose ``CustomFn`` names a function the *installed* collection does not export. A backup that carries only the
YAML and the UDF sources therefore cannot be restored: the collection travels under ``function-collection/`` in the
backup ZIP (next to the YAML, under ``pipeline_<id>/`` when the ZIP holds several pipelines), and a restore publishes
it, and installs it, before any UDF is compiled and any entity is saved.

CoreHub routes, all under ``/pipelines/{id}/config/function-collection`` (permission ``canManageFunctionCollection``):
``GET`` (404: nothing published, also what a CoreHub before 2.3 answers), ``PUT`` (a publication job, 409 while another
runs), ``GET /publications/{jobId}`` and ``POST /install``.

Two settings, read when a publication starts: ``FUNCTION_COLLECTION_POLL_SECONDS`` (seconds between two reads of a
publication job, default 2) and ``FUNCTION_COLLECTION_PUBLICATION_TIMEOUT_SECONDS`` (how long a restore waits for
publications, 409 waits included, default 1800: a publication also recompiles every UDF of the pipeline).
``FUNCTION_COLLECTION_PATH`` names a directory of sources a standalone run publishes (see ``create_all_entities``).
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import commons

logger = logging.getLogger(__name__)

# Folder of a backup ZIP (and of an Automator session directory) holding the sources of a pipeline's function collection.
FUNCTION_COLLECTION_FOLDER = "function-collection"

# First release with function collections, the one that also takes the grouped column mapping matrix.
FUNCTION_COLLECTION_MIN_COREHUB_VERSION = (2, 3)

_COLLECTION_PATH = "/pipelines/{pipeline_id}/config/function-collection"
_PUBLICATION_PATH = _COLLECTION_PATH + "/publications/{job_id}"
_INSTALL_PATH = _COLLECTION_PATH + "/install"

_DEFAULT_POLL_SECONDS = 2.0
_DEFAULT_PUBLICATION_TIMEOUT_SECONDS = 1800.0

_RUNNING = "Running"
_COMPLETED = "Completed"
_NOTHING_PUBLISHED = "NothingPublished"

_CUSTOM_FUNCTION_TYPE = "CustomFn"

# What :func:`installed_function_names` answers for a pipeline with no installed collection, as opposed to an installed
# one that exports no function (an empty set): CoreHub does not check the functions a column binds while none is
# installed, because one may be published after the entity is configured.
NO_COLLECTION = object()


class FunctionCollectionError(RuntimeError):
    """A function collection could not be read, published or installed; the message says why."""


# ---------------------------------------------------------------------------
# CoreHub errors
# ---------------------------------------------------------------------------

def http_status(error: Any) -> Optional[int]:
    """The HTTP status of a failed CoreHub request (``commons.fetch_core_hub`` chains requests' error), or None."""
    while error is not None:
        response = getattr(error, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return response.status_code
        error = error.__cause__
    return None


def _error_body(error: Any) -> Any:
    """The JSON body of a failed CoreHub request, None when there is none."""
    while error is not None:
        response = getattr(error, "response", None)
        if response is not None:
            try:
                return response.json()
            except Exception:  # pylint: disable=broad-except
                return None
        error = error.__cause__
    return None


def _env_seconds(name: str, default: float) -> float:
    """A number of seconds set in the environment; the default when it is not set or not a number."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Reading the collection (backup)
# ---------------------------------------------------------------------------

def corehub_has_function_collections(token: str, fetch=None) -> bool:
    """Whether the CoreHub in use (Gluesync 2.3 or later) has function collections.

    A version that cannot be read is taken as a current CoreHub, as for the UDF signature.
    """
    from utils.udf_signature import parse_corehub_version

    parsed = parse_corehub_version(commons.corehub_version(token, fetch=fetch or commons.fetch_core_hub))
    return parsed is None or parsed[:2] >= FUNCTION_COLLECTION_MIN_COREHUB_VERSION


def fetch_function_collection(token: str, pipeline_id: str, fetch=None) -> Optional[Dict[str, Any]]:
    """``GET`` the function collection of a pipeline: its ``FunctionCollectionDto``, None when it has none.

    A 404 means nothing is published (or a CoreHub before 2.3, which has no such route). Any other failure raises
    :class:`FunctionCollectionError`, so a backup never silently loses the collection.
    """
    fetch = fetch or commons.fetch_core_hub
    try:
        # A 404 is the answer for "no collection", not an error to log
        response = fetch(_COLLECTION_PATH.format(pipeline_id=pipeline_id), token=token, expected_error_statuses={404})
    except Exception as exc:  # pylint: disable=broad-except
        if http_status(exc) == 404:
            return None
        raise FunctionCollectionError(
            f"Could not read the function collection of pipeline {pipeline_id}: {exc}"
        ) from exc
    return response if isinstance(response, dict) and response else None


def exported_function_names(collection: Optional[Dict[str, Any]]) -> frozenset:
    """The names of the functions the installed version of a collection exports (none for no collection)."""
    exported = (collection or {}).get("exportedFunctions") or []
    return frozenset(
        str(function["name"]) for function in exported if isinstance(function, dict) and function.get("name")
    )


def installed_function_names(collection: Optional[Dict[str, Any]]):
    """The names the *installed* collection exports, or :data:`NO_COLLECTION` when the pipeline has none installed.

    A collection that is published and not installed is not what the entity validation of the target agent sees.
    """
    if not collection or not collection.get("installedVersion"):
        return NO_COLLECTION
    return exported_function_names(collection)


def _safe_name_part(part: str) -> str:
    """A file or folder name made of letters, digits, ``_``, ``-`` and ``.`` only, never starting with a dot."""
    return "".join(c for c in part if c.isalnum() or c in ("_", "-", ".")).lstrip(".")


def safe_relative_path(file_name: Any, default: str = "Source.java") -> str:
    """A ``fileName`` of the collection as a path that stays under its folder (it may have package directories)."""
    parts = [_safe_name_part(p) for p in str(file_name or "").replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p) or default


def function_collection_files(token: str, pipeline_id: str, fetch=None) -> List[Tuple[str, str]]:
    """``(path under function-collection/, code)`` of each source of the pipeline's collection; empty without one.

    Raises :class:`FunctionCollectionError` when the collection cannot be read.
    """
    collection = fetch_function_collection(token, pipeline_id, fetch=fetch)
    files: List[Tuple[str, str]] = []
    taken = set()
    for source in (collection or {}).get("sources") or []:
        if not isinstance(source, dict) or not isinstance(source.get("code"), str):
            continue
        path = safe_relative_path(source.get("fileName"))
        stem, dot, extension = path.rpartition(".")
        number = 2
        while path in taken:
            path = f"{stem}_{number}.{extension}" if dot else f"{path}_{number}"
            number += 1
        taken.add(path)
        files.append((path, source["code"]))
    return files


# ---------------------------------------------------------------------------
# Finding the collection in a backup (restore)
# ---------------------------------------------------------------------------

def _zip_member_parts(member: str) -> Optional[Tuple[str, str]]:
    """``(folder prefix, path under function-collection/)`` of a ZIP member that is a collection source, else None."""
    parts = member.replace("\\", "/").split("/")
    if member.endswith("/") or FUNCTION_COLLECTION_FOLDER not in parts[:2] or parts[-1].startswith("._"):
        return None
    index = parts.index(FUNCTION_COLLECTION_FOLDER)
    if index == len(parts) - 1:
        return None
    prefix = "".join(c for c in parts[0] if c.isalnum() or c in ("_", "-")) if index == 1 else ""
    path = safe_relative_path("/".join(parts[index + 1:]), default="")
    if not path or (index == 1 and not prefix) or parts[0] == "__MACOSX":
        return None
    return prefix, path


def extract_function_collection_members(zip_file, root: Path) -> int:
    """Extract the function collection sources of a backup ZIP under ``root``, keeping their folder in the ZIP.

    A member lands in ``root/function-collection/`` or ``root/pipeline_<id>/function-collection/``, whatever
    path it has in the ZIP otherwise. Returns the number of files written.
    """
    written = 0
    for member in zip_file.namelist():
        found = _zip_member_parts(member)
        if found is None:
            continue
        prefix, path = found
        destination = root.joinpath(*([prefix] if prefix else []), FUNCTION_COLLECTION_FOLDER, *path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(zip_file.read(member))
        written += 1
    return written


def _collection_dir_under(root: Path, pipeline_id: Optional[str]) -> Optional[Path]:
    """The ``function-collection`` folder of a backup extracted under one root (see :func:`find_function_collection_dir`)."""
    if pipeline_id:
        safe_id = "".join(c for c in str(pipeline_id) if c.isalnum() or c in ("_", "-"))
        of_pipeline = root / f"pipeline_{safe_id}" / FUNCTION_COLLECTION_FOLDER
        if of_pipeline.is_dir():
            return of_pipeline
    if (root / FUNCTION_COLLECTION_FOLDER).is_dir():
        return root / FUNCTION_COLLECTION_FOLDER
    if pipeline_id:
        return None
    nested = [p for p in sorted(root.glob(f"pipeline_*/{FUNCTION_COLLECTION_FOLDER}")) if p.is_dir()]
    if len(nested) > 1:
        logger.warning(
            "The backup holds the function collections of several pipelines and the YAML does not say which one "
            "it belongs to; none is restored"
        )
    return nested[0] if len(nested) == 1 else None


def find_function_collection_dir(roots: Iterable[Any], pipeline_id: Optional[str] = None) -> Optional[Path]:
    """The ``function-collection`` folder of a backup extracted under one of ``roots``, None when there is none.

    ``pipeline_id`` is the id the backup was taken from: its ``pipeline_<id>/function-collection`` comes first, then
    ``function-collection`` at the root. Without an id a single ``pipeline_*/function-collection`` is the one.
    """
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        found = _collection_dir_under(Path(root), pipeline_id)
        if found is not None:
            return found
    return None


def read_function_collection_dir(directory: Any) -> List[Dict[str, str]]:
    """The sources in a function collection folder, as ``{fileName, code}`` sorted by name (subfolders count)."""
    directory = Path(directory)
    sources = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory)
        if any(part.startswith(".") for part in relative.parts):
            continue
        sources.append({"fileName": relative.as_posix(), "code": path.read_text(encoding="utf-8")})
    return sources


# ---------------------------------------------------------------------------
# Publishing the collection (restore)
# ---------------------------------------------------------------------------

def _compilation_message(message: Any) -> str:
    """One compiler message of a rejection: ``file:line:column: text``."""
    if not isinstance(message, dict):
        return str(message)
    place = ":".join(str(message[k]) for k in ("file", "line", "column") if message.get(k) is not None)
    return f"{place}: {message.get('message')}" if place else str(message.get("message"))


def describe_refusal(job: Dict[str, Any]) -> str:
    """What a refused or failed publication says, one finding after the other."""
    rejection = job.get("rejection") or {}
    findings = [f"collection: {_compilation_message(m)}" for m in rejection.get("collectionErrors") or []]
    for failure in rejection.get("consumerFailures") or []:
        errors = "; ".join(_compilation_message(m) for m in failure.get("errors") or [])
        findings.append(f"UDF {failure.get('consumer')} no longer compiles: {errors}")
    for failure in rejection.get("fieldFunctionFailures") or []:
        details = "; ".join(str(f) for f in failure.get("findings") or [])
        findings.append(f"entity {failure.get('entityName')} column {failure.get('columnName')}: {details}")
    if job.get("failure"):
        findings.append(f"publication failed: {job['failure']}")
    return "; ".join(findings) or f"the publication ended {job.get('state')} without a result"


def _wait_for_job(token: str, pipeline_id: str, job: Dict[str, Any], deadline: float, fetch) -> Dict[str, Any]:
    """Read a publication job until it is no longer running; the finished job."""
    while job.get("state") == _RUNNING:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FunctionCollectionError(
                f"The function collection publication of pipeline {pipeline_id} (job {job.get('jobId')}) did not "
                f"finish in time; it may still be running on CoreHub. Raise "
                f"FUNCTION_COLLECTION_PUBLICATION_TIMEOUT_SECONDS to wait longer"
            )
        if not job.get("jobId"):
            raise FunctionCollectionError(f"CoreHub started a publication for pipeline {pipeline_id} without a job id")
        time.sleep(min(_env_seconds("FUNCTION_COLLECTION_POLL_SECONDS", _DEFAULT_POLL_SECONDS), remaining))
        try:
            job = fetch(_PUBLICATION_PATH.format(pipeline_id=pipeline_id, job_id=job["jobId"]), token=token)
        except Exception as exc:  # pylint: disable=broad-except
            raise FunctionCollectionError(
                f"Could not read the function collection publication of pipeline {pipeline_id}: {exc}"
            ) from exc
        if not isinstance(job, dict):
            raise FunctionCollectionError(f"Unexpected publication job of pipeline {pipeline_id}: {job!r}")
    return job


def _start_publication(token: str, pipeline_id: str, sources: List[Dict[str, str]], deadline: float, fetch):
    """``PUT`` the sources; the publication job. While another publication runs (409) it is waited for first."""
    while True:
        try:
            # A 409 (a publication is running) is waited for, not an error to log
            job = fetch(_COLLECTION_PATH.format(pipeline_id=pipeline_id), method="PUT", token=token,
                        body={"sources": sources}, expected_error_statuses={409})
        except Exception as exc:  # pylint: disable=broad-except
            if http_status(exc) != 409:
                raise FunctionCollectionError(
                    f"CoreHub refused to publish the function collection of pipeline {pipeline_id}: {exc}"
                ) from exc
            running = _error_body(exc)
            logger.info("A function collection publication is already running for pipeline %s; waiting for it",
                        pipeline_id)
            if isinstance(running, dict) and running.get("jobId"):
                _wait_for_job(token, pipeline_id, running, deadline, fetch)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FunctionCollectionError(
                    f"A function collection publication kept pipeline {pipeline_id} busy until the time to wait ran out"
                ) from exc
            time.sleep(min(_env_seconds("FUNCTION_COLLECTION_POLL_SECONDS", _DEFAULT_POLL_SECONDS), remaining))
            continue
        if not isinstance(job, dict):
            raise FunctionCollectionError(f"Unexpected answer publishing the function collection of {pipeline_id}: {job!r}")
        return job


def _same_sources(published: Any, sources: List[Dict[str, str]]) -> bool:
    """Whether the published sources are exactly ``sources`` (same file names, same code)."""
    if not isinstance(published, list):
        return False
    current = {s.get("fileName"): s.get("code") for s in published if isinstance(s, dict)}
    return len(current) == len(published) and current == {s["fileName"]: s["code"] for s in sources}


def _publish(token: str, pipeline_id: str, sources: List[Dict[str, str]], fetch) -> Dict[str, Any]:
    """Publish ``sources`` and wait for the verdict; the published collection, else :class:`FunctionCollectionError`."""
    deadline = time.monotonic() + _env_seconds(
        "FUNCTION_COLLECTION_PUBLICATION_TIMEOUT_SECONDS", _DEFAULT_PUBLICATION_TIMEOUT_SECONDS)
    job = _start_publication(token, pipeline_id, sources, deadline, fetch)
    job = _wait_for_job(token, pipeline_id, job, deadline, fetch)
    published = job.get("published")
    if job.get("state") != _COMPLETED or not isinstance(published, dict) or job.get("rejection"):
        raise FunctionCollectionError(
            f"CoreHub refused the function collection of pipeline {pipeline_id}: {describe_refusal(job)}"
        )
    for warning in published.get("warnings") or []:
        logger.warning("Function collection of pipeline %s: %s", pipeline_id, _compilation_message(warning))
    if published.get("unverifiedConsumers"):
        logger.warning("Function collection of pipeline %s: not checked against %s", pipeline_id,
                       ", ".join(map(str, published["unverifiedConsumers"])))
    return published


def _install(token: str, pipeline_id: str, fetch) -> Dict[str, Any]:
    """Make the published collection the one the pipeline uses; CoreHub's answer."""
    try:
        installed = fetch(_INSTALL_PATH.format(pipeline_id=pipeline_id), method="POST", token=token)
    except Exception as exc:  # pylint: disable=broad-except
        raise FunctionCollectionError(
            f"Could not install the function collection of pipeline {pipeline_id}: {exc}"
        ) from exc
    if not isinstance(installed, dict) or installed.get("result") == _NOTHING_PUBLISHED:
        raise FunctionCollectionError(
            f"CoreHub has no published function collection to install on pipeline {pipeline_id}"
        )
    return installed


def restore_function_collection(token: str, pipeline_id: str, sources: List[Dict[str, str]],
                                fetch=None) -> Optional[Dict[str, Any]]:
    """Publish ``sources`` as the function collection of a pipeline and install it.

    Does nothing (None) for no sources and for a CoreHub before 2.3. A pipeline that already has a published
    collection with these very sources is not published again, only installed. Raises
    :class:`FunctionCollectionError` when CoreHub refuses the collection, naming every finding, or on a timeout.
    Returns ``{"version", "exportedFunctions", "published"}`` (``published`` is False when the sources were there).
    """
    fetch = fetch or commons.fetch_core_hub
    if not sources:
        return None
    if not corehub_has_function_collections(token, fetch=fetch):
        logger.warning(
            "The backup holds a function collection, but the CoreHub in use is older than Gluesync 2.3 and has no "
            "function collections: it is not restored on pipeline %s", pipeline_id)
        return None

    existing = fetch_function_collection(token, pipeline_id, fetch=fetch)
    published_now = not (existing and _same_sources(existing.get("sources"), sources))
    if published_now:
        if existing:
            logger.warning("Pipeline %s already has a function collection (version %s) with other sources; the one "
                           "of the backup replaces it", pipeline_id, existing.get("version"))
        logger.info("Publishing the function collection of the backup (%d file(s)) on pipeline %s",
                    len(sources), pipeline_id)
        _publish(token, pipeline_id, sources, fetch)
    else:
        logger.info("Pipeline %s already has the function collection of the backup (version %s); installing it",
                    pipeline_id, existing.get("version"))

    installed = _install(token, pipeline_id, fetch)
    names = sorted(exported_function_names(installed) or exported_function_names(existing))
    logger.info("Function collection of pipeline %s %s: version %s exporting %s", pipeline_id,
                str(installed.get("result")).lower(), installed.get("version") or (existing or {}).get("version"),
                ", ".join(names) or "no function")
    if installed.get("restartedEntities"):
        logger.info("Installing the function collection restarted %d entit(ies): %s",
                    len(installed["restartedEntities"]), ", ".join(map(str, installed["restartedEntities"])))
    return {"version": installed.get("version"), "exportedFunctions": names, "published": published_now}


def copy_function_collection(token: str, from_pipeline_id: str, to_pipeline_id: str, fetch=None) -> Optional[Dict[str, Any]]:
    """Publish and install the function collection of a pipeline on another one (a duplicated pipeline)."""
    collection = fetch_function_collection(token, from_pipeline_id, fetch=fetch)
    sources = [
        {"fileName": s["fileName"], "code": s["code"]}
        for s in (collection or {}).get("sources") or []
        if isinstance(s, dict) and s.get("fileName") and isinstance(s.get("code"), str)
    ]
    return restore_function_collection(token, to_pipeline_id, sources, fetch=fetch)


# ---------------------------------------------------------------------------
# The functions the YAML binds (pre-flight check of the entities)
# ---------------------------------------------------------------------------

def custom_function_names(node: Any) -> List[str]:
    """The ``functionName`` of every ``CustomFn`` in a YAML section or an expression, nested ones included."""
    names: List[str] = []
    if isinstance(node, dict):
        if node.get("type") == _CUSTOM_FUNCTION_TYPE and node.get("functionName"):
            names.append(str(node["functionName"]))
        for child in node.values():
            names.extend(custom_function_names(child))
    elif isinstance(node, list):
        for child in node:
            names.extend(custom_function_names(child))
    return names


def _bindings_text(bindings: Dict[str, List[str]]) -> str:
    """``column A is composed by the function f, column B ...``"""
    return ", ".join(f"column {column} is composed by the function {', '.join(functions)}"
                     for column, functions in bindings.items())


def unbound_functions_message(table: Any, bindings: Dict[str, List[str]], pipeline_id: Any, exported: Iterable[str]) -> str:
    """The refusal of a table whose columns name functions the installed collection does not export."""
    exported = sorted(exported)
    names = {name for functions in bindings.values() for name in functions}
    return (
        f"Table {table}: {_bindings_text(bindings)}, but the function collection installed on the target pipeline "
        f"{pipeline_id} does not export {'it' if len(names) == 1 else 'them'} "
        f"({'it exports ' + ', '.join(exported) if exported else 'it exports no function'}). Restore or publish "
        f"the function collection on the target pipeline first (it is the {FUNCTION_COLLECTION_FOLDER}/ folder of a "
        f"backup ZIP); a backup made with an Automator older than this release does not contain it"
    )


def no_collection_message(table: Any, bindings: Dict[str, List[str]], pipeline_id: Any) -> str:
    """The warning for a table whose columns name functions while the target pipeline has no function collection."""
    return (
        f"Table {table}: {_bindings_text(bindings)}, and the target pipeline {pipeline_id} has no function "
        f"collection installed. CoreHub saves the entity anyway, but it fails on every row until the function "
        f"collection is restored or published on the pipeline (it is the {FUNCTION_COLLECTION_FOLDER}/ folder of a "
        f"backup ZIP; a backup made with an Automator older than this release does not contain it)"
    )
