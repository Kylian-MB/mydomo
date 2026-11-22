"""What the code of a tool may use, read by ``ast`` in the core and never executed (spec 6, the
first check).

``forbidden`` refuses what a code does beyond these rules (a code that does not parse is one
finding):

A. A module enters a tool's code in one way only: ``import m`` or ``import m as n``, ``m`` a
   module of ``ALLOWED_MODULES`` (no dotted or relative import). ``from m import name`` is allowed
   when ``m`` is an allowed module, ``name`` does not start with ``_``, and the real attribute
   ``m.name`` (looked up by importing the allowed module in the core) exists and is not a module.
   No import binds the name ``self`` or ``cls``. The module ``domo`` (the program's requests to
   the core) is read on its stub, ``madetools.program_stub``: its public names alone.
B. Every chain of attributes rooted at a name bound by ``import`` (``m.a.b``) is resolved in the
   core, step by step, against the real objects: a step that starts with ``_``, that does not
   exist, or that yields a module (allowed or not) is refused.
C. A name bound by ``import`` is only ever the base of an attribute access (``m.x``): never a value
   (assigned, passed, put in a container, returned, compared, given as a default).
D. ``typing.get_type_hints`` and ``string.Formatter`` are refused when reached by A or B.
   ``operator.attrgetter`` and ``operator.methodcaller`` reached by A or B, under any name (each is
   told by the real object it is, not by how it is spelled), and the builtins ``getattr``,
   ``setattr``, ``delattr`` and ``hasattr``, are allowed only called directly with written-out
   names (every argument of ``attrgetter``, the first of ``methodcaller``, the second of the
   builtins), none of whose dotted parts starts with ``_``; never as values.
E. An attribute that starts with ``_`` is refused, but for ``self._x`` or ``cls._x`` written in
   the body of a function defined directly in a class body (its decorators, argument defaults and
   annotations are not its body), where ``self`` (or ``cls``) is that function's first parameter,
   the function has no decorator written ``staticmethod``, and nothing in it binds the name again
   (by any form of binding: assignment of any kind, ``for``, ``with``, ``except``, ``match``,
   ``import``, ``global``, ``nonlocal``, ``del``, a definition, a parameter of an inner function or
   lambda, a type parameter); an attribute between double underscores is refused everywhere.
F. The names of ``FORBIDDEN_NAMES`` (``eval``, ``exec``, ``compile``, ``open``, ``globals``,
   ``vars``...), any name between double underscores but ``__name__``, and any text that is a name
   between double underscores but ``"__main__"`` are refused.

This check narrows the obvious routes out of what a tool may do and makes a model's mistake visible
before anything runs; it does not claim to be complete. The sandbox is the boundary: a process of
its own, no network, no file kept, no secret, and every request it makes judged by the core.

``secret_literals`` lists the lines of the texts written in a code that have the shape of a secret
(``memory.service.refuses_secret_shapes``): a key or a token never goes in a tool. Only the texts
are read, never the names of the code (an ordinary long name is no secret).

Each finding is a short line for the model that wrote the code: what, and on which line; never a
piece of the code itself.
"""

import ast
import builtins
import importlib
import operator
import string
import typing
from types import ModuleType
from typing import Final

from domo_core.memory.service import refuses_secret_shapes

ALLOWED_MODULES: Final = frozenset(
    {
        "math",
        "decimal",
        "fractions",
        "statistics",
        "datetime",
        "zoneinfo",
        "calendar",
        "json",
        "re",
        "string",
        "textwrap",
        "unicodedata",
        "itertools",
        "functools",
        "collections",
        "dataclasses",
        "enum",
        "typing",
        "heapq",
        "bisect",
        "random",
        "uuid",
        "hashlib",
        "base64",
        "copy",
        "operator",
        # The program's requests to the core, resolved on ``program_stub`` (``_load``).
        "domo",
    }
)
# The modules the check reads in place of the one a program gets: the module ``domo`` is the
# sandbox's, never imported by the core; its names are read on a stub with no behaviour.
_STUBS: Final = {"domo": "domo_core.madetools.program_stub"}

# The names that run code, open a file, wait for a person or reach what the imports keep away.
FORBIDDEN_NAMES: Final = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "globals",
        "locals",
        "vars",
        "dir",
        "input",
        "breakpoint",
        "help",
        "exit",
        "quit",
        "memoryview",
    }
)
# The objects of the allowed modules that evaluate a text or walk objects by name (rule D).
_DENIED: Final[tuple[object, ...]] = (typing.get_type_hints, string.Formatter)
_GETTERS: Final[tuple[object, ...]] = (operator.attrgetter, operator.methodcaller)
# The only names between double underscores a tool may say, as a name or a text.
_HARMLESS_DUNDERS: Final = frozenset({"__name__", "__main__"})
# The calls that reach an attribute by its name as a text.
_BY_NAME: Final = frozenset({"getattr", "setattr", "delattr", "hasattr"})
_OWN: Final = frozenset({"self", "cls"})
_MISSING: Final = object()

type _Function = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _load(name: str) -> ModuleType:
    """The allowed module ``name`` as the check reads it: the module itself, or its stub."""
    return importlib.import_module(_STUBS.get(name, name))


def _attribute(value: object, name: str) -> object:
    """The real attribute ``name`` of ``value`` (an object of an allowed module), or
    ``_MISSING``."""
    try:
        return getattr(value, name, _MISSING)
    except Exception:
        return _MISSING


def _bound_in(function: _Function) -> set[str]:
    """Every name a binding inside ``function`` binds, but its own parameters: any assignment
    target, ``for``, ``with``, ``except``, ``match`` captures, ``import``, ``global``,
    ``nonlocal``, ``del``, a definition, the parameters of an inner function or lambda, a type
    parameter."""
    own = {id(arg) for arg in ast.walk(function.args) if isinstance(arg, ast.arg)}
    names: set[str] = set()
    for node in ast.walk(function):
        if node is function:
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.arg) and id(node) not in own:
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".", 1)[0])
        elif isinstance(node, ast.Global | ast.Nonlocal):
            names.update(node.names)
        elif isinstance(
            node,
            ast.FunctionDef
            | ast.AsyncFunctionDef
            | ast.ClassDef
            | ast.TypeVar
            | ast.ParamSpec
            | ast.TypeVarTuple,
        ):
            names.add(node.name)
    return names


class _Reader:
    """One reading of a tree: the names ``import`` binds, the names bound to a getter, where each
    node sits, and the findings, line by line."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.modules: dict[str, ModuleType] = {}
        self.getters: dict[str, object] = {}
        self.found: list[tuple[int, str]] = []

    def say(self, node: ast.AST, what: str) -> None:
        self.found.append((getattr(node, "lineno", 0), what))

    # --- rule A ----------------------------------------------------------------------------------

    def imports(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname in _OWN:
                self.say(node, f"an import may not be named {alias.asname}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    self.say(node, f"import {alias.name} is not allowed")
                else:
                    self.modules[alias.asname or alias.name] = _load(alias.name)
            return
        if node.level:
            self.say(node, "a relative import is not allowed")
            return
        if node.module not in ALLOWED_MODULES:
            self.say(node, f"import from {node.module} is not allowed")
            return
        module = _load(node.module)
        for alias in node.names:
            value = _attribute(module, alias.name) if not alias.name.startswith("_") else None
            if alias.name == "*" or value is None or value is _MISSING:
                self.say(node, f"import of {alias.name} from {node.module} is not allowed")
            elif isinstance(value, ModuleType):
                self.say(node, f"{alias.name} is a module: import {alias.name} itself")
            elif any(value is denied for denied in _DENIED):
                self.say(node, f"{alias.name} is not allowed")
            elif any(value is getter for getter in _GETTERS):
                self.getters[alias.asname or alias.name] = value

    # --- rules B, C, D ---------------------------------------------------------------------------

    def _called(self, node: ast.expr) -> ast.Call | None:
        parent = self.parents.get(node)
        return parent if isinstance(parent, ast.Call) and parent.func is node else None

    def _by_name(self, node: ast.expr, what: str, called: object) -> None:
        """Rule D: ``what`` (``node``), bound to ``called``, only called directly, with written-out
        names: every argument of ``attrgetter``, the first of ``methodcaller``, the second of the
        builtins of ``_BY_NAME``."""
        call = self._called(node)
        if call is None:
            self.say(node, f"{what} may only be called directly")
            return
        if called is operator.attrgetter:
            names = call.args
            written = bool(names) and not call.keywords
        elif called is operator.methodcaller:
            names = call.args[:1]
            written = bool(names)
        else:
            names = call.args[1:2]
            written = len(call.args) >= 2
        for named in names:
            if not (isinstance(named, ast.Constant) and isinstance(named.value, str)):
                written = False
            elif any(part.startswith("_") for part in named.value.split(".")):
                self.say(node, f"{what} on {named.value} is not allowed")
        if not written:
            self.say(node, f"{what} with a name that is not written out")

    def chain(self, node: ast.Attribute) -> None:
        """Rule B: the chain of attributes ending at ``node`` (the outermost), when it is rooted at
        a module, resolved step by step."""
        steps: list[ast.Attribute] = []
        base: ast.expr = node
        while isinstance(base, ast.Attribute):
            steps.append(base)
            base = base.value
        if not (isinstance(base, ast.Name) and base.id in self.modules):
            return
        value: object = self.modules[base.id]
        where = base.id
        for step in reversed(steps):
            if step.attr.startswith("_"):
                # Never a method's own: a name bound by an import is never self or cls (rule A).
                self.say(step, f"the attribute {step.attr} is not allowed")
                return
            value = _attribute(value, step.attr)
            where = f"{where}.{step.attr}"
            if value is _MISSING:
                self.say(step, f"{where} is not something the core can tell")
                return
            if isinstance(value, ModuleType):
                self.say(step, f"{where} is a module: import it itself")
                return
            if any(value is denied for denied in _DENIED):
                self.say(step, f"{where} is not allowed")
                return
        if any(value is getter for getter in _GETTERS):
            self._by_name(node, where, value)

    def name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES or (_dunder(node.id) and node.id not in _HARMLESS_DUNDERS):
            self.say(node, f"{node.id} is not allowed")
            return
        if not isinstance(node.ctx, ast.Load):
            return
        parent = self.parents.get(node)
        if node.id in self.modules:
            if not (isinstance(parent, ast.Attribute) and parent.value is node):
                self.say(node, f"the module {node.id} may only be used as {node.id}.something")
        elif node.id in _BY_NAME:
            self._by_name(node, node.id, getattr(builtins, node.id))
        elif node.id in self.getters:
            self._by_name(node, node.id, self.getters[node.id])

    # --- rule E ----------------------------------------------------------------------------------

    def _functions_of(self, node: ast.AST) -> list[_Function]:
        """The functions whose body holds ``node``, innermost first; a function's decorators,
        defaults and annotations are not its body (they are evaluated where it is defined)."""
        found: list[_Function] = []
        child, parent = node, self.parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                body = parent.body if isinstance(parent.body, list) else [parent.body]
                if any(part is child for part in body):
                    found.append(parent)
            child, parent = parent, self.parents.get(parent)
        return found

    def _own(self, node: ast.Attribute) -> bool:
        base = node.value
        if not (isinstance(base, ast.Name) and base.id in _OWN):
            return False
        for function in self._functions_of(node):
            arguments = function.args
            parameters = [
                arg.arg
                for arg in [
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    *(arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None),
                ]
            ]
            if base.id not in parameters:
                continue  # a closure: the name is an outer function's
            first = [*arguments.posonlyargs, *arguments.args][:1]
            return (
                not isinstance(function, ast.Lambda)
                and isinstance(self.parents.get(function), ast.ClassDef)
                and bool(first)
                and first[0].arg == base.id
                and not any(
                    isinstance(d, ast.Name) and d.id == "staticmethod"
                    for d in function.decorator_list
                )
                and base.id not in _bound_in(function)
            )
        return False

    def attribute(self, node: ast.Attribute) -> None:
        if _dunder(node.attr) or (node.attr.startswith("_") and not self._own(node)):
            self.say(node, f"the attribute {node.attr} is not allowed")
        parent = self.parents.get(node)
        if not (isinstance(parent, ast.Attribute) and parent.value is node):
            self.chain(node)

    # --- rule F and the whole reading ------------------------------------------------------------

    def text(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, str) and _dunder(value) and value not in _HARMLESS_DUNDERS:
            self.say(node, f"the text {value} is not allowed")

    def read(self) -> list[str]:
        nodes = list(ast.walk(self.tree))
        # The imports first: they bind the names the rest is read with.
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                self.imports(node)
        for node in nodes:
            if isinstance(node, ast.Name):
                self.name(node)
            elif isinstance(node, ast.Attribute):
                self.attribute(node)
            elif isinstance(node, ast.Constant):
                self.text(node)
        ordered = sorted(dict.fromkeys(self.found), key=lambda found: found[0])
        return [f"line {line}: {what}" for line, what in ordered]


def _tree(code: str) -> ast.Module | str:
    """The tree of ``code``, or the one finding it is when it does not parse."""
    try:
        return ast.parse(code)
    except SyntaxError as error:
        return f"line {error.lineno or 0}: the code does not parse ({error.msg})"
    except (ValueError, RecursionError, MemoryError):
        return "the code does not parse"


def forbidden(code: str) -> list[str]:
    """What ``code`` uses beyond what a tool may (see the module docstring), one line per finding,
    in the order of the code; empty when it uses nothing else. ``code`` is never executed."""
    tree = _tree(code)
    if isinstance(tree, str):
        return [tree]
    try:
        return _Reader(tree).read()
    except RecursionError:
        return ["the code nests too deep"]


def secret_literals(code: str) -> list[int]:
    """The lines of the texts written in ``code`` that have the shape of a secret, in order (none
    for a code that does not parse: ``forbidden`` says so)."""
    tree = _tree(code)
    if isinstance(tree, str):
        return []
    lines = {
        getattr(node, "lineno", 0)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and refuses_secret_shapes(node.value)
    }
    return sorted(lines)
