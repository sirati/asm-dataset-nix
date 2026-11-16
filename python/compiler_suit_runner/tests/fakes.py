"""Shared test doubles for the compiler_suit_runner unit tests.

Use these instead of writing a new runner stub, config factory or
primary handle in each test file.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Union

from compiler_suit_runner.suit_task import SuitTaskConfig

Response = tuple[bytes, bytes, int]
ResponseOrFn = Union[Response, Callable[[list[str]], Response]]


def make_suit_config(tmp_path: pathlib.Path, **overrides: Any) -> SuitTaskConfig:
    """A primary-side ``SuitTaskConfig`` rooted at ``tmp_path``.

    Keyword arguments override any field.
    """
    fields: dict[str, Any] = dict(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "manifests",
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
    )
    fields.update(overrides)
    return SuitTaskConfig(**fields)


class FakeRunner:
    """Scripted stand-in for a ``run_subprocess(argv) -> (out, err, rc)``.

    Register responses by argv prefix with :meth:`on`; the first matching
    rule wins. A response is a ``(stdout, stderr, rc)`` tuple or a callable
    that takes the argv and returns one. Every call is logged in
    ``calls``. An argv that matches no rule returns ``default`` when one is
    set and raises otherwise, so a code path that escapes the script is
    noticed.
    """

    def __init__(self, default: Response | None = None) -> None:
        self.calls: list[list[str]] = []
        self.default = default
        self._rules: list[tuple[list[str], ResponseOrFn]] = []

    def on(
        self,
        *prefix: str,
        stdout: bytes = b"",
        stderr: bytes = b"",
        rc: int = 0,
        respond: Callable[[list[str]], Response] | None = None,
    ) -> "FakeRunner":
        self._rules.append(
            (list(prefix), respond if respond is not None else (stdout, stderr, rc))
        )
        return self

    def __call__(self, argv: list[str], *_args: Any, **_kwargs: Any) -> Response:
        argv = list(argv)
        self.calls.append(argv)
        for prefix, response in self._rules:
            if argv[: len(prefix)] == prefix:
                return response(argv) if callable(response) else response
        if self.default is not None:
            return self.default
        raise AssertionError(f"FakeRunner: no scripted response for {argv!r}")


class FakePrimaryHandle:
    """Records ``spawn_tasks`` calls and returns the configured errors."""

    def __init__(self, errors: list | None = None) -> None:
        self.calls: list[list] = []
        self._errors = errors or []

    def spawn_tasks(self, task_infos):
        self.calls.append(list(task_infos))
        return list(self._errors)
