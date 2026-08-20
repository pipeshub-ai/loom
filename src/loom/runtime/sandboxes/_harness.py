"""The child harness: what runs on the untrusted side of every out-of-process
sandbox (:class:`~loom.runtime.sandboxes.subprocess.SubprocessSandbox`,
:class:`~loom.runtime.sandboxes.docker.DockerSandbox`).

A string, not a module — deliberately. The child imports nothing of Loom's: no
store, no credentials, no engine. Giving it any of those would put trusted code
on the untrusted side of the boundary a sandbox exists to hold. Both adapters
above build this same script and spawn a fresh interpreter with it as the
program to run.

``build_child_script(ctx_shims=...)`` is the one seam a host may use to extend
what the child can *ask for*: extra source injected into the ``Ctx`` class body,
after every standard method. It runs inside the sandbox, on the untrusted side
— a host adding a shim widens the vocabulary the body can use, never what it
can *do*, because every request the shim builds still crosses the wire to
:class:`~loom.runtime.sandbox.RuntimeChannel` and the broker chain behind it.
A shim that redefines a method already defined above it (a class body executes
top to bottom, and a later ``def`` of the same name simply replaces the
earlier one in the class namespace) is how a host adapts an existing call
shape — see PipesHub's ``pipeshub_tool`` positional-argument compatibility shim
for the case this was built for.
"""

from __future__ import annotations

__all__ = ["build_child_script"]

#: The placeholder `build_child_script` replaces — a marker rather than
#: `str.format()`, because the template's own JSON-literal braces
#: (`{"t": "call", ...}`) would otherwise all need escaping to `{{`/`}}`.
_SHIM_MARKER = "    # __CTX_SHIMS__"

_TEMPLATE = '''\
import asyncio, json, sys

# The real stdout, captured before user source can touch `sys.stdout`. A
# body that calls `print()` (routine in model-generated code) would
# otherwise write straight into the JSON protocol channel and corrupt
# every message after it.
_wire_out = sys.stdout

def _emit(message):
    _wire_out.write(json.dumps(message) + "\\n")
    _wire_out.flush()

def _await_reply():
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("the host closed the channel")
    return json.loads(line)

class _AgentResult(dict):
    """JSON shape of ``ctx.agent()``, plus the methods inline ``AgentResult`` has.

    The wire cannot carry a Pydantic model into this process (importing the
    parent's types is the thing a sandbox exists to avoid), so the parent
    sends a dict. Generated bodies call ``result.text()`` because that is
    what the coding-agent prompt teaches; a plain dict raises
    ``AttributeError: 'dict' object has no attribute 'text'``. A dict
    subclass stays JSON-serializable if the workflow returns the object
    itself.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def text(self):
        output = self.get("output")
        if isinstance(output, str):
            return output
        messages = self.get("messages") or []
        if messages:
            last = messages[-1]
            if isinstance(last, dict) and last.get("content"):
                return last["content"]
        return "" if output is None else str(output)

class Ctx:
    """Every durable call becomes a line on stdout.

    No journal, no store, no credentials here. The parent owns all of it,
    which is the point: this process is the untrusted side.
    """

    def __init__(self, run_id):
        self.run_id = run_id

    @staticmethod
    def _named(target):
        """A step's name, however the body referred to it.

        A body writes ``ctx.step(charge, ...)`` and gets a StepDefinition,
        which cannot cross the pipe. Only the name has to: the parent holds
        the implementations, and sending anything more would be sending the
        untrusted side a handle on trusted code.
        """
        return getattr(target, "name", None) or getattr(
            target, "__name__", None
        ) or str(target)

    async def _call(self, kind, target, arguments, effect, *, name=None):
        runnable = not isinstance(target, str) and (
            callable(target) or hasattr(target, "fn")
        )
        message = {"t": "call", "kind": kind, "target": self._named(target),
                   "arguments": arguments, "effect": effect, "local": bool(runnable)}
        if name is not None:
            message["name"] = name
        _emit(message)
        reply = _await_reply()
        if reply.get("delegate"):
            if not runnable:
                raise RuntimeError("parent asked to run a step this child cannot")
            try:
                if hasattr(target, "invoke"):
                    result = target.invoke(None, **arguments)
                else:
                    result = target(**arguments)
                if hasattr(result, "__await__"):
                    result = await result
                _emit({"t": "delegated_result", "ok": True, "value": result})
            except BaseException as exc:
                _emit({"t": "delegated_result", "ok": False,
                       "error": "%s: %s" % (type(exc).__name__, exc)})
            reply = _await_reply()
        if not reply.get("ok", False):
            raise RuntimeError(reply.get("error") or "refused")
        return reply.get("value")

    async def step(self, target, *positional, name=None, **arguments):
        if positional:
            # The wire carries a named mapping, and guessing parameter
            # names here would bind arguments to the wrong ones silently.
            raise TypeError(
                "a sandboxed body must pass step arguments by keyword; "
                "got %d positional" % len(positional)
            )
        return await self._call("step", target, arguments, "write", name=name)

    async def tool(self, target, **arguments):
        return await self._call("tool", target, arguments, "write")

    async def read(self, target, **arguments):
        return await self._call("tool", target, arguments, "read")

    async def agent(self, prompt, **arguments):
        value = await self._call("agent", prompt, arguments, "write")
        if isinstance(value, dict):
            return _AgentResult(value)
        return _AgentResult({"output": value})

    async def node(self, node_id, payload=None, **arguments):
        """Call a catalogued node. The payload crosses as plain JSON.

        A Pydantic model cannot go over the pipe, and ``ctx.node`` validates
        a mapping into the node's own Input on the parent side anyway — so
        the model is built where the type is known, which is the trusted
        side.
        """
        body = payload
        if hasattr(body, "model_dump"):
            body = body.model_dump(mode="json")
        return await self._call(
            "node", node_id, dict(arguments, payload=body), "write"
        )

    async def wait_for_event(self, name, **arguments):
        """Park until something outside answers.

        The call dies with this process — the parent raises Suspend and the
        child goes with it. Nothing is lost: the child holds no durable
        state, so re-entry re-runs the body from the top with every earlier
        call served from the parent's journal, and this one returns the
        recorded answer.
        """
        return await self._call("event", name, arguments, "read")

    async def wait_for_approval(self, subject, **arguments):
        return await self._call(
            "event", "approval:" + str(subject), arguments, "read"
        )

    async def sleep(self, seconds, *, name="sleep"):
        """Durably pause. Dies with this process like any other suspend:
        the parent's `Suspend` propagates out, the child exits, and
        re-entry resumes from the journaled wake time."""
        return await self._call(
            "sleep", "sleep", {"seconds": seconds, "name": name}, "write"
        )

    async def report(self, message, **arguments):
        """Not durable — mirrors `Context.report`, which is not journaled
        either. Reaches the parent's run stream and nothing else."""
        return await self._call("report", message, arguments, "read")

    # __CTX_SHIMS__

def _find_entrypoint(namespace, name):
    """The workflow to run: by name, else the sole workflow-like object.

    A host that renamed the definition (PipesHub compiles every version
    under a synthetic name so its `code_hash` is unique) binds it in the
    exec'd namespace under the *original* function name, not the one the
    engine sent as ``entrypoint`` — so the direct lookup misses. Falling
    back to "the only thing here that looks like a workflow" is safe
    specifically because the host's own compiler already enforces exactly
    one `@workflow` per source; if that ever stops being true, refusing
    rather than guessing which of several to run is the correct failure.
    """
    entry = namespace.get(name)
    if entry is not None:
        return entry
    candidates = [
        value
        for value in namespace.values()
        # `hasattr(value, "triggers")` is what tells a WorkflowDefinition
        # from a StepDefinition: both wrap a callable under `.fn`, but
        # only a workflow carries trigger specs. Without this a module
        # exporting local `@step` helpers alongside its one `@workflow`
        # (the ordinary shape) would see more than one "candidate" and
        # refuse to fall back at all.
        if callable(value) and hasattr(value, "fn") and hasattr(value, "triggers")
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        "no %r in the sandboxed source, and %d workflow-like candidates "
        "were found (need exactly 1 to fall back)" % (name, len(candidates))
    )

SANDBOXED_FILENAME = "<sandboxed>"


class ImportDenied(ImportError):
    """A module the policy did not permit."""


def _install_import_guard(allowed):
    """Restrict `import` *in sandboxed source* to *allowed*.

    Runs before user source is compiled, and only when the host asked for an
    import policy — `allowed is None` leaves `__import__` untouched, which is
    every policy written before this existed.

    The guard applies to a module only when the import was issued **from the
    sandboxed source itself**, decided by the calling frame's filename. The
    obvious implementation — exempt whatever is already in `sys.modules` —
    enforces nothing: this harness imports `asyncio`, `asyncio` imports
    `socket`, and a body asking for `socket` is then handed the module the
    harness happened to load. Keying on the caller instead means an allowed
    module's own transitive imports still work, which is what makes a narrow
    allowlist usable at all.

    `importlib.import_module` is followed through, because its frames sit
    between the caller and here. Beyond that this is a second line and not the
    first: a body reaching into `sys.modules` directly is not stopped here, and
    `CodeValidator` is where that is caught.
    """
    import builtins

    permitted = frozenset(allowed)
    real_import = builtins.__import__

    # This function's own frame is the harness script's, and so is
    # `guarded`'s. Both sit between `__import__` and the code that issued the
    # import, and both have to be stepped over before the caller is visible.
    _guard_frame = sys._getframe(0).f_code.co_filename

    def _issued_by_sandboxed_source():
        """Whether the import statement being executed came from user source."""
        depth = 1
        while depth < 12:
            try:
                frame = sys._getframe(depth)
            except ValueError:
                return False
            name = frame.f_code.co_filename
            if name == SANDBOXED_FILENAME:
                return True
            if name == _guard_frame:
                depth += 1
                continue
            # Step over importlib's own machinery, so
            # `importlib.import_module("socket")` is attributed to its caller
            # rather than to importlib. Any other frame decides immediately —
            # walking past one would let a harness-internal import be blamed on
            # user source further up the stack.
            if "importlib" in name:
                depth += 1
                continue
            return False
        return False

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if not _issued_by_sandboxed_source():
            return real_import(name, globals, locals, fromlist, level)
        if level:
            raise ImportDenied(
                "relative imports are not available to sandboxed source"
            )
        root = name.split(".")[0] if name else name
        if root and root not in permitted:
            raise ImportDenied(
                "import of %r is not permitted here; the sandbox policy allows "
                "%s" % (name, ", ".join(sorted(permitted)) or "nothing")
            )
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded


async def _main():
    request = json.loads(sys.stdin.readline())
    namespace = dict(request.get("namespace") or {})
    # User source runs with stdout redirected to stderr: only `_emit`
    # (bound to the real stdout above) may write to the wire.
    sys.stdout = sys.stderr
    allowed_imports = request.get("allowed_imports")
    if allowed_imports is not None:
        _install_import_guard(allowed_imports)
    exec(compile(request["source"], SANDBOXED_FILENAME, "exec"), namespace)
    entry = _find_entrypoint(namespace, request["entrypoint"])
    result = entry(Ctx(request["run_id"]), request["input"])
    if hasattr(result, "__await__"):
        result = await result
    _emit({"t": "done", "value": result})

try:
    asyncio.run(_main())
except BaseException as exc:
    _emit({"t": "error", "error": "%s: %s" % (type(exc).__name__, exc)})
    sys.exit(1)
'''


def build_child_script(*, ctx_shims: str = "") -> str:
    """Build the child harness script.

    ``ctx_shims`` is extra Python source injected into the ``Ctx`` class
    body, after every standard method, and before the module-level
    ``_find_entrypoint``/``_main`` machinery. Empty by default, which is
    exactly :data:`_TEMPLATE` with its marker line removed — the standard
    harness, nothing added.

    A shim redefining a name already defined above it (``step``, most
    commonly) replaces that method: a class body executes top to bottom, so
    the later ``def`` simply overwrites the earlier entry in the class's own
    namespace. That is how a host adapts a call shape a prior source
    convention used, without forking this entire script to do it.
    """
    return _TEMPLATE.replace(_SHIM_MARKER, ctx_shims)
