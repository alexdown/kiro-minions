"""Pure-stdlib fakes and SDK stubs for the kiro-minions test suite.

The sandbox interpreter cannot import boto3 or bedrock_agentcore (the global
site-packages is read-only and those packages are absent). To let the test
suite import orchestrator/handler.py and runtime/agent.py without any installs,
this module injects lightweight fake modules into ``sys.modules`` via
``install_sdk_stubs()``. ``tests/conftest.py`` calls that helper at import time,
before any test imports the production modules.

Everything here is standard library only. No fake performs real network, AWS,
GitHub, Jira, or kiro-cli calls.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any


# ---------------------------------------------------------------------------
# botocore.exceptions.ClientError (fake)
# ---------------------------------------------------------------------------


class ClientError(Exception):
    """Minimal stand-in for botocore.exceptions.ClientError.

    Mirrors the real attribute surface the production code relies on:
    ``exc.response["Error"]["Code"]``.
    """

    def __init__(self, error_response: dict | None = None,
                 operation_name: str = "FakeOperation") -> None:
        self.response = error_response or {"Error": {"Code": "Unknown", "Message": ""}}
        self.operation_name = operation_name
        code = self.response.get("Error", {}).get("Code", "Unknown")
        super().__init__(f"{operation_name}: {code}")


def _conditional_check_failed() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException",
                   "Message": "The conditional request failed"}},
        operation_name="PutItem",
    )


# ---------------------------------------------------------------------------
# DynamoDB fakes
# ---------------------------------------------------------------------------


class FakeDynamoTable:
    """In-memory DynamoDB table supporting the subset handler.py uses.

    Records every put_item/update_item/get_item call and enforces just enough
    ConditionExpression semantics for ``_claim_job``:

    * ``attribute_not_exists(sonar_hash)`` — item does not yet exist.
    * ``#s = :failed`` — an existing item whose status is ``failed``.
    * A lease-expiry clause (added in FEAT-004), expressed as either
      ``attribute_not_exists(lease_expires_at)`` or ``#l < :now`` / ``:now > #l``
      — an existing in-flight row whose lease has expired is claimable again.

    When the condition does not hold, a ClientError with
    ``Error.Code == 'ConditionalCheckFailedException'`` is raised, matching real
    DynamoDB behavior.
    """

    def __init__(self, name: str = "fake-table") -> None:
        self.name = name
        self.items: dict[Any, dict] = {}
        self.calls: list[dict] = []
        # Partition key attribute name used by this project.
        self.key_attr = "sonar_hash"

    # -- helpers ---------------------------------------------------------

    def _key_of(self, item: dict) -> Any:
        return item.get(self.key_attr)

    def _record(self, op: str, **kwargs: Any) -> None:
        self.calls.append({"op": op, **kwargs})

    def _condition_holds(self, key: Any, cond: str,
                         expr_names: dict | None,
                         expr_values: dict | None) -> bool:
        """Evaluate the (small) set of ConditionExpressions used by the code."""
        if not cond:
            return True
        existing = self.items.get(key)
        expr_names = expr_names or {}
        expr_values = expr_values or {}
        now = int(time.time())

        # Split on OR; the claim condition is a disjunction of simple clauses.
        clauses = [c.strip() for c in cond.split(" OR ")]
        for clause in clauses:
            if self._clause_holds(clause, existing, expr_names, expr_values, now):
                return True
        return False

    def _strip_wrapping_parens(self, clause: str) -> str:
        """Remove a single pair of balanced parens that wraps the whole clause.

        Unlike ``str.strip('()')`` this never eats the closing paren of a
        function call like ``attribute_not_exists(x)`` — it only unwraps when the
        leading ``(`` matches the trailing ``)`` at the very ends of the clause.
        """
        clause = clause.strip()
        while clause.startswith("(") and clause.endswith(")"):
            depth = 0
            matched = True
            for i, ch in enumerate(clause):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and i != len(clause) - 1:
                        # The opening paren closes before the end -> not a wrap.
                        matched = False
                        break
            if not matched:
                break
            clause = clause[1:-1].strip()
        return clause

    def _clause_holds(self, clause: str, existing: dict | None,
                      expr_names: dict, expr_values: dict, now: int) -> bool:
        clause = self._strip_wrapping_parens(clause)

        # Conjunctions: every AND-part must hold. Supports the FEAT-004 reclaim
        # clause "(#s = :in_flight AND #l < :now)".
        if " AND " in clause:
            return all(
                self._clause_holds(part, existing, expr_names, expr_values, now)
                for part in clause.split(" AND ")
            )

        if clause.startswith("attribute_not_exists"):
            attr = clause[clause.index("(") + 1:clause.rindex(")")].strip()
            attr = expr_names.get(attr, attr)
            if attr == self.key_attr:
                return existing is None
            return existing is None or attr not in existing

        if clause.startswith("attribute_exists"):
            attr = clause[clause.index("(") + 1:clause.rindex(")")].strip()
            attr = expr_names.get(attr, attr)
            return existing is not None and attr in existing

        # Binary comparisons: "#s = :failed", "#l < :now", ":now > #l".
        for op in ("<=", ">=", "<", ">", "="):
            if f" {op} " in clause:
                left, right = (s.strip() for s in clause.split(f" {op} ", 1))
                return self._compare(op, left, right, existing, expr_names,
                                     expr_values, now)
        return False

    def _resolve(self, token: str, existing: dict | None,
                 expr_names: dict, expr_values: dict, now: int):
        if token in expr_values:
            return expr_values[token]
        # Attribute reference (possibly aliased via #name).
        attr = expr_names.get(token, token)
        if existing is None:
            return None
        return existing.get(attr)

    def _compare(self, op: str, left: str, right: str, existing: dict | None,
                 expr_names: dict, expr_values: dict, now: int) -> bool:
        lval = self._resolve(left, existing, expr_names, expr_values, now)
        rval = self._resolve(right, existing, expr_names, expr_values, now)
        if lval is None or rval is None:
            return False
        try:
            if op == "=":
                return lval == rval
            if op == "<":
                return lval < rval
            if op == ">":
                return lval > rval
            if op == "<=":
                return lval <= rval
            if op == ">=":
                return lval >= rval
        except TypeError:
            return False
        return False

    # -- DynamoDB API subset --------------------------------------------

    def put_item(self, Item: dict, ConditionExpression: str | None = None,  # noqa: N803
                 ExpressionAttributeNames: dict | None = None,  # noqa: N803
                 ExpressionAttributeValues: dict | None = None,  # noqa: N803
                 **kwargs: Any) -> dict:
        self._record("put_item", Item=Item,
                     ConditionExpression=ConditionExpression,
                     ExpressionAttributeNames=ExpressionAttributeNames,
                     ExpressionAttributeValues=ExpressionAttributeValues)
        key = self._key_of(Item)
        if ConditionExpression and not self._condition_holds(
                key, ConditionExpression, ExpressionAttributeNames,
                ExpressionAttributeValues):
            raise _conditional_check_failed()
        self.items[key] = dict(Item)
        return {}

    def update_item(self, Key: dict, UpdateExpression: str = "",  # noqa: N803
                    ConditionExpression: str | None = None,  # noqa: N803
                    ExpressionAttributeNames: dict | None = None,  # noqa: N803
                    ExpressionAttributeValues: dict | None = None,  # noqa: N803
                    **kwargs: Any) -> dict:
        self._record("update_item", Key=Key, UpdateExpression=UpdateExpression,
                     ConditionExpression=ConditionExpression,
                     ExpressionAttributeNames=ExpressionAttributeNames,
                     ExpressionAttributeValues=ExpressionAttributeValues)
        key = Key.get(self.key_attr)
        if ConditionExpression and not self._condition_holds(
                key, ConditionExpression, ExpressionAttributeNames,
                ExpressionAttributeValues):
            raise _conditional_check_failed()
        item = self.items.setdefault(key, dict(Key))
        self._apply_set(item, UpdateExpression, ExpressionAttributeNames or {},
                        ExpressionAttributeValues or {})
        return {}

    def get_item(self, Key: dict, **kwargs: Any) -> dict:  # noqa: N803
        self._record("get_item", Key=Key)
        key = Key.get(self.key_attr)
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def _apply_set(self, item: dict, update_expr: str, expr_names: dict,
                   expr_values: dict) -> None:
        """Apply a ``SET a = :x, b = :y`` update expression."""
        expr = update_expr.strip()
        if not expr.upper().startswith("SET"):
            return
        assignments = expr[3:].split(",")
        for assignment in assignments:
            if "=" not in assignment:
                continue
            target, value_token = (s.strip() for s in assignment.split("=", 1))
            attr = expr_names.get(target, target)
            value = expr_values.get(value_token, value_token)
            item[attr] = value


class FakeDynamoResource:
    """Fake boto3 DynamoDB resource; ``.Table(name)`` returns a FakeDynamoTable."""

    def __init__(self) -> None:
        self.tables: dict[str, FakeDynamoTable] = {}

    def Table(self, name: str) -> FakeDynamoTable:  # noqa: N802 - boto3 API name
        return self.tables.setdefault(name, FakeDynamoTable(name))


# ---------------------------------------------------------------------------
# SQS fake (used by FEAT-004)
# ---------------------------------------------------------------------------


class FakeSqsClient:
    """Fake boto3 SQS client recording send_message calls."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs: Any) -> dict:
        self.messages.append(dict(kwargs))
        return {"MessageId": f"fake-msg-{len(self.messages)}"}


# ---------------------------------------------------------------------------
# bedrock-agentcore client fake (used by FEAT-004)
# ---------------------------------------------------------------------------


class FakeAgentCoreClient:
    """Fake boto3 ``bedrock-agentcore`` client recording invoke calls.

    Returns a canned response with a readable ``body`` so callers that aggregate
    the stream/body get deterministic text without any network.
    """

    def __init__(self, response_text: str = '{"status": "ok"}') -> None:
        self.invocations: list[dict] = []
        self.response_text = response_text

    def invoke(self, **kwargs: Any) -> dict:
        self.invocations.append(dict(kwargs))
        return {"body": _FakeStreamingBody(self.response_text.encode("utf-8"))}


class _FakeStreamingBody:
    """Minimal streaming-body stand-in exposing ``.read()``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._data


# ---------------------------------------------------------------------------
# subprocess fakes
# ---------------------------------------------------------------------------


class FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, args: Any, returncode: int = 0, stdout: str = "",
                 stderr: str = "") -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    """Callable drop-in for ``subprocess.run`` that records every invocation.

    Captures the exact ``args`` (argv list or shell string) and the ``shell``
    flag so tests can assert that argv lists (not shell strings) are used for
    ticket-derived commands and that no secret ever appears in a recorded call.

    Canned results can be queued; otherwise a zero-exit FakeCompletedProcess is
    returned.
    """

    def __init__(self, default_result: FakeCompletedProcess | None = None) -> None:
        self.calls: list[dict] = []
        self._queue: list[FakeCompletedProcess] = []
        self._default = default_result

    def queue_result(self, result: FakeCompletedProcess) -> None:
        self._queue.append(result)

    def __call__(self, args: Any, shell: bool = False, cwd: Any = None,
                 capture_output: bool = False, text: bool = False,
                 timeout: Any = None, **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({
            "args": args,
            "shell": shell,
            "cwd": str(cwd) if cwd is not None else None,
            "timeout": timeout,
            "kwargs": kwargs,
        })
        if self._queue:
            result = self._queue.pop(0)
        elif self._default is not None:
            result = self._default
        else:
            result = FakeCompletedProcess(args, returncode=0)
        if result.args is None:
            result.args = args
        return result

    # -- assertion helpers ----------------------------------------------

    def all_args(self) -> list[Any]:
        return [call["args"] for call in self.calls]

    def flat_text(self) -> str:
        """All recorded args flattened to a single string (for secret scans)."""
        parts: list[str] = []
        for call in self.calls:
            args = call["args"]
            if isinstance(args, (list, tuple)):
                parts.extend(str(a) for a in args)
            else:
                parts.append(str(args))
        return "\n".join(parts)

    def contains(self, needle: str) -> bool:
        return needle in self.flat_text()


# ---------------------------------------------------------------------------
# sys.modules SDK stubs
# ---------------------------------------------------------------------------


def _make_boto3_module() -> types.ModuleType:
    """Build a fake ``boto3`` module.

    ``boto3.resource("dynamodb", ...)`` returns a FakeDynamoResource and
    ``boto3.client(name, ...)`` returns a FakeSqsClient / FakeAgentCoreClient /
    generic recorder depending on the service name. This lets handler.py build
    its module-level resource/client at import time with no real AWS.
    """
    module = types.ModuleType("boto3")

    def resource(service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "dynamodb":
            return FakeDynamoResource()
        return FakeDynamoResource()

    def client(service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "sqs":
            return FakeSqsClient()
        if service_name in ("bedrock-agentcore", "bedrock-agentcore-runtime"):
            return FakeAgentCoreClient()
        # Generic fallback client that records nothing but accepts calls.
        return FakeAgentCoreClient()

    module.resource = resource  # type: ignore[attr-defined]
    module.client = client  # type: ignore[attr-defined]
    return module


def _make_botocore_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Build fake ``botocore`` and ``botocore.exceptions`` modules."""
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError  # type: ignore[attr-defined]
    botocore.exceptions = exceptions  # type: ignore[attr-defined]
    return botocore, exceptions


class _FakeBedrockAgentCoreApp:
    """Fake ``BedrockAgentCoreApp``.

    Mirrors the real app surface the runtime relies on: ``.entrypoint`` is a
    decorator that returns the wrapped function unchanged, and ``.run()`` is a
    no-op (so importing agent.py and constructing the app never starts a server).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.entrypoints: list[Any] = []
        self.async_tasks: list[Any] = []

    def entrypoint(self, func):
        self.entrypoints.append(func)
        return func

    def async_task(self, func):
        self.async_tasks.append(func)
        return func

    def run(self, *args: Any, **kwargs: Any) -> None:
        return None


def _make_bedrock_agentcore_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Build fake ``bedrock_agentcore`` and ``bedrock_agentcore.runtime``."""
    pkg = types.ModuleType("bedrock_agentcore")
    runtime = types.ModuleType("bedrock_agentcore.runtime")
    runtime.BedrockAgentCoreApp = _FakeBedrockAgentCoreApp  # type: ignore[attr-defined]
    pkg.runtime = runtime  # type: ignore[attr-defined]
    pkg.BedrockAgentCoreApp = _FakeBedrockAgentCoreApp  # type: ignore[attr-defined]
    return pkg, runtime


def install_sdk_stubs() -> None:
    """Inject fake boto3 / botocore / bedrock_agentcore modules into sys.modules.

    Idempotent and safe to call from conftest at import time. Must run BEFORE any
    import of orchestrator/handler.py or runtime/agent.py so their module-level
    SDK usage resolves to the fakes.
    """
    boto3_module = _make_boto3_module()
    botocore, botocore_exceptions = _make_botocore_modules()
    bedrock_pkg, bedrock_runtime = _make_bedrock_agentcore_modules()

    sys.modules["boto3"] = boto3_module
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = botocore_exceptions
    sys.modules["bedrock_agentcore"] = bedrock_pkg
    sys.modules["bedrock_agentcore.runtime"] = bedrock_runtime
