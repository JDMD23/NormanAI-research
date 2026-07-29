"""Pin the handoff to NormanAI-crm-core.

The CSV this repo writes is consumed by crm-core's `scripts/crm_intake.py`,
which matches columns case-insensitively against its own `CSV_ALIASES`. A column
name that falls outside those aliases is not an error over there — the field is
silently dropped and the company lands on JD's board missing data.

`CRM_CORE_CSV_ALIASES` below is copied verbatim from crm_intake.py at
commit 4fc6ef0. If intake's aliases change, update this constant and let the
test tell you what broke.
"""

from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import urlsplit

from lib.candidates import CSV_COLUMNS, Candidate

CRM_CORE_CSV_ALIASES = {
    "company": ("company", "company name", "name", "account", "organization name", "organization"),
    "website": ("website", "url", "domain", "company website"),
    "linkedin": ("company linkedin", "linkedin", "linkedin url", "company linkedin url"),
    "crunchbase": ("crunchbase", "crunchbase url", "cb url", "organization name url", "organization url"),
    "founders": ("founders", "founder", "ceo/founder", "ceo founder"),
    "one_liner": ("one-liner", "one liner", "description", "about"),
    "x_handle": ("x handle", "twitter", "x", "twitter url", "x (twitter)"),
    "founded": ("founded date", "founded", "founded year", "year founded"),
    "hq": ("headquarters location", "hq", "headquarters", "location"),
    "industries": ("industries", "industry", "categories"),
    "last_funding_date": ("last funding date",),
    "last_funding_usd": ("last funding amount (in usd)", "last funding amount", "last funding usd"),
    "last_funding_type": ("last funding type", "funding type", "stage"),
    "total_funding_usd": ("total funding amount (in usd)", "total funding amount", "total funding usd"),
    "num_rounds": ("number of funding rounds", "funding rounds"),
    "investors": ("top 5 investors", "investors", "lead investors"),
}

ALL_ALIASES = {alias for group in CRM_CORE_CSV_ALIASES.values() for alias in group}


class _NotionWriteVisitor(ast.NodeVisitor):
    """Resolve enough Python semantics to enforce the Research writer boundary."""

    _UNKNOWN_STRING_PART = "{__runtime_value__}"

    def __init__(self, label: str) -> None:
        self.label = label
        self.violations: list[str] = []
        self._alias_scopes: list[dict[str, str | None]] = [{}]
        self._string_scopes: list[dict[str, str | None]] = [{}]
        self._notion_scopes: list[dict[str, bool]] = [{}]
        self._page_callable_scopes: list[dict[str, bool]] = [{}]

    def visit_Module(self, node: ast.Module) -> None:
        self._prebind_module(node.body)
        for statement in node.body:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self._bind_import(item)
            if _is_writer_symbol(item.name):
                self._record(node, f"imports writer module {item.name}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for item in node.names:
            qualified = ".".join(part for part in (module, item.name) if part)
            self._bind_import_from(module, item)
            if _is_writer_symbol(qualified):
                self._record(node, f"imports writer symbol {qualified}")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_assignment(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        if node.value is None:
            self._invalidate_target(node.target)
        else:
            self._bind_assignment(node.target, node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind_target(node, qualified=None, string=None, name=node.name)
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._bind_target(node, qualified=None, string=None, name=node.name)
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._push_scope()
        self._bind_arguments(node.args)
        self.visit(node.body)
        self._pop_scope()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind_target(node, qualified=None, string=None, name=node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._push_scope()
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._invalidate_target(node.target)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._invalidate_target(node.target)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._bind_name(
                node.name,
                qualified=None,
                string=None,
                notion=False,
                page_callable=False,
            )
        for statement in node.body:
            self.visit(statement)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_Call(self, node: ast.Call) -> None:
        called = self._qualified_name(node.func)
        parts = called.casefold().split(".") if called else []
        if called and _is_writer_symbol(called):
            self._record(node, f"calls or constructs writer {called}")

        if called in {
            "__import__",
            "builtins.__import__",
            "importlib.import_module",
        }:
            imported = self._argument_string(node, 0, "name")
            if imported and _is_writer_symbol(imported):
                self._record(node, f"dynamically imports writer {imported}")

        if self._is_notion_page_callable(node.func):
            self._record(node, f"calls Notion page writer {called}")

        method, url = self._http_operation(node, parts)
        if method in {"POST", "PATCH"} and _is_notion_page_url(url):
            self._record(node, f"constructs direct Notion {method} page operation")
        self.generic_visit(node)

    def _http_operation(
        self,
        node: ast.Call,
        parts: list[str],
    ) -> tuple[str | None, str | None]:
        if parts[-3:] == ["urllib", "request", "request"]:
            url = self._argument_url(node, 0, "url", "full_url")
            method = self._argument_string(node, None, "method")
            if method is None and self._has_data_argument(node):
                method = "POST"
            return _upper(method), url

        if parts and parts[-1] in {"post", "patch"}:
            return parts[-1].upper(), self._argument_url(node, 0, "url")

        if parts and parts[-1] == "request":
            return (
                _upper(self._argument_string(node, 0, "method")),
                self._argument_url(node, 1, "url"),
            )

        if parts[-3:] == ["urllib", "request", "urlopen"]:
            url = self._argument_url(node, 0, "url")
            method = "POST" if self._has_data_argument(node) else "GET"
            return method, url
        return None, None

    def _argument_string(
        self,
        node: ast.Call,
        position: int | None,
        *names: str,
    ) -> str | None:
        for keyword in node.keywords:
            if keyword.arg in names:
                return self._static_string(keyword.value)
        if position is not None and len(node.args) > position:
            return self._static_string(node.args[position])
        return None

    def _argument_url(
        self,
        node: ast.Call,
        position: int,
        *names: str,
    ) -> str | None:
        for keyword in node.keywords:
            if keyword.arg in names:
                return self._string_pattern(keyword.value)
        if len(node.args) > position:
            return self._string_pattern(node.args[position])
        return None

    @staticmethod
    def _has_data_argument(node: ast.Call) -> bool:
        return len(node.args) > 1 or any(
            keyword.arg in {"data", "json"} for keyword in node.keywords
        )

    def _static_string(self, node: ast.AST | None) -> str | None:
        value = self._string_pattern(node)
        if value is None or self._UNKNOWN_STRING_PART in value:
            return None
        return value

    def _string_pattern(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self._lookup(self._string_scopes, node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._string_pattern(node.left)
            right = self._string_pattern(node.right)
            if left is None and right is None:
                return None
            return (
                left if left is not None else self._UNKNOWN_STRING_PART
            ) + (
                right if right is not None else self._UNKNOWN_STRING_PART
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self._string_pattern(node.left)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            return self._string_pattern(node.func.value)
        if isinstance(node, ast.JoinedStr):
            pieces: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    pieces.append(value.value)
                    continue
                if isinstance(value, ast.FormattedValue):
                    resolved = self._string_pattern(value.value)
                    if resolved is not None:
                        pieces.append(resolved)
                    else:
                        pieces.append(self._UNKNOWN_STRING_PART)
                    continue
                return None
            return "".join(pieces)
        return None

    def _qualified_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._lookup(self._alias_scopes, node.id) or node.id
        if isinstance(node, ast.Attribute):
            owner = self._qualified_name(node.value)
            return f"{owner}.{node.attr}" if owner else node.attr
        if isinstance(node, ast.Call):
            return self._qualified_name(node.func)
        return None

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        self._push_scope()
        self._bind_arguments(node.args)
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()

    def _prebind_module(self, body: list[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    self._bind_import(item)
            elif isinstance(statement, ast.ImportFrom):
                module = statement.module or ""
                for item in statement.names:
                    self._bind_import_from(module, item)

        for _ in range(max(1, len(body))):
            before = (
                dict(self._alias_scopes[-1]),
                dict(self._string_scopes[-1]),
                dict(self._notion_scopes[-1]),
                dict(self._page_callable_scopes[-1]),
            )
            for statement in body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        self._bind_assignment(target, statement.value)
                elif (
                    isinstance(statement, ast.AnnAssign)
                    and statement.value is not None
                ):
                    self._bind_assignment(statement.target, statement.value)
            after = (
                self._alias_scopes[-1],
                self._string_scopes[-1],
                self._notion_scopes[-1],
                self._page_callable_scopes[-1],
            )
            if before == after:
                break

    def _bind_import(self, item: ast.alias) -> None:
        local = item.asname or item.name.split(".", 1)[0]
        qualified = item.name if item.asname else item.name.split(".", 1)[0]
        self._bind_name(
            local,
            qualified=qualified,
            string=None,
            notion=_is_notion_sdk_symbol(item.name),
            page_callable=False,
        )

    def _bind_import_from(self, module: str, item: ast.alias) -> None:
        qualified = ".".join(part for part in (module, item.name) if part)
        self._bind_name(
            item.asname or item.name,
            qualified=qualified,
            string=None,
            notion=_is_notion_sdk_symbol(qualified),
            page_callable=False,
        )

    def _bind_assignment(self, target: ast.AST, value: ast.AST) -> None:
        if (
            isinstance(target, (ast.List, ast.Tuple))
            and isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
        ):
            for target_item, value_item in zip(target.elts, value.elts):
                self._bind_assignment(target_item, value_item)
            return
        if isinstance(target, (ast.List, ast.Tuple)):
            self._invalidate_target(target)
            return
        self._bind_target(
            target,
            qualified=self._qualified_name(value),
            string=self._string_pattern(value),
            notion=self._is_notion_value(value),
            page_callable=self._is_notion_page_callable(value),
        )

    def _bind_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
        for argument in all_arguments:
            self._bind_target(argument, qualified=None, string=None, name=argument.arg)
        if arguments.vararg is not None:
            self._bind_target(
                arguments.vararg,
                qualified=None,
                string=None,
                name=arguments.vararg.arg,
            )
        if arguments.kwarg is not None:
            self._bind_target(
                arguments.kwarg,
                qualified=None,
                string=None,
                name=arguments.kwarg.arg,
            )

    def _bind_target(
        self,
        target: ast.AST,
        *,
        qualified: str | None,
        string: str | None,
        notion: bool = False,
        page_callable: bool = False,
        name: str | None = None,
    ) -> None:
        if name is not None:
            names = (name,)
        elif isinstance(target, ast.Name):
            names = (target.id,)
        elif isinstance(target, ast.Starred):
            self._bind_target(
                target.value,
                qualified=qualified,
                string=string,
                notion=notion,
                page_callable=page_callable,
            )
            return
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                self._bind_target(
                    item,
                    qualified=qualified,
                    string=string,
                    notion=notion,
                    page_callable=page_callable,
                )
            return
        else:
            return
        for local in names:
            self._bind_name(
                local,
                qualified=qualified,
                string=string,
                notion=notion,
                page_callable=page_callable,
            )

    def _bind_name(
        self,
        name: str,
        *,
        qualified: str | None,
        string: str | None,
        notion: bool,
        page_callable: bool,
    ) -> None:
        self._alias_scopes[-1][name] = qualified
        self._string_scopes[-1][name] = string
        self._notion_scopes[-1][name] = notion
        self._page_callable_scopes[-1][name] = page_callable

    def _invalidate_target(self, target: ast.AST) -> None:
        self._bind_target(
            target,
            qualified=None,
            string=None,
            notion=False,
            page_callable=False,
        )

    def _is_notion_value(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self._lookup_bool(self._notion_scopes, node.id)
        if isinstance(node, ast.Attribute):
            return self._is_notion_value(node.value) or _is_notion_sdk_symbol(
                self._qualified_name(node) or ""
            )
        if isinstance(node, ast.Call):
            return self._is_notion_value(node.func) or _is_notion_sdk_symbol(
                self._qualified_name(node.func) or ""
            )
        return False

    def _is_notion_page_callable(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self._lookup_bool(self._page_callable_scopes, node.id)
        return (
            isinstance(node, ast.Attribute)
            and node.attr in {"create", "update"}
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "pages"
            and self._is_notion_value(node.value.value)
        )

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        outputs: tuple[ast.AST, ...],
    ) -> None:
        self._push_scope()
        for generator in generators:
            self.visit(generator.iter)
            self._invalidate_target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for output in outputs:
            self.visit(output)
        self._pop_scope()

    @staticmethod
    def _lookup(
        scopes: list[dict[str, str | None]],
        name: str,
    ) -> str | None:
        for scope in reversed(scopes):
            if name in scope:
                return scope[name]
        return None

    @staticmethod
    def _lookup_bool(scopes: list[dict[str, bool]], name: str) -> bool:
        for scope in reversed(scopes):
            if name in scope:
                return scope[name]
        return False

    def _push_scope(self) -> None:
        self._alias_scopes.append({})
        self._string_scopes.append({})
        self._notion_scopes.append({})
        self._page_callable_scopes.append({})

    def _pop_scope(self) -> None:
        self._alias_scopes.pop()
        self._string_scopes.pop()
        self._notion_scopes.pop()
        self._page_callable_scopes.pop()

    def _record(self, node: ast.AST, reason: str) -> None:
        self.violations.append(f"{self.label}:{node.lineno}: {reason}")


def _notion_write_violations(source: str, label: str) -> list[str]:
    visitor = _NotionWriteVisitor(label)
    visitor.visit(ast.parse(source, filename=label))
    return visitor.violations


def _is_writer_symbol(qualified: str) -> bool:
    parts = [
        part.replace("-", "_").casefold()
        for part in qualified.split(".")
        if part
    ]
    if any(part in {"crm_intake", "crm_funding_handoff"} for part in parts):
        return True
    if any(
        part in {"notion_client", "notion_writer", "notionclient"}
        for part in parts
    ):
        return True
    return "notion" in parts and any(
        part in {"client", "asyncclient", "writer"} for part in parts
    )


def _is_notion_sdk_symbol(qualified: str) -> bool:
    parts = {
        part.replace("-", "_").casefold()
        for part in qualified.split(".")
        if part
    }
    return bool(
        parts.intersection(
            {"notion", "notion_client", "notion_writer", "notionclient"}
        )
    )


def _is_notion_page_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "api.notion.com"
        or port not in {None, 443}
    ):
        return False
    path = parsed.path.rstrip("/")
    return path == "/v1/pages" or path.startswith("/v1/pages/")


def _upper(value: str | None) -> str | None:
    return value.strip().upper() if value is not None else None


def test_every_emitted_column_is_recognised_by_intake():
    unknown = [c for c in CSV_COLUMNS if c.strip().casefold() not in ALL_ALIASES]
    assert not unknown, f"crm_intake.py would silently drop these columns: {unknown}"


def test_every_intake_field_is_populated_by_us():
    """We must emit a column for each field intake knows how to hydrate."""
    emitted = {c.strip().casefold() for c in CSV_COLUMNS}
    missing = [
        field
        for field, aliases in CRM_CORE_CSV_ALIASES.items()
        if not emitted.intersection(aliases)
    ]
    assert not missing, f"no CSV column maps to intake fields: {missing}"


def test_unknown_numbers_are_blank_not_zero():
    """Unknown ≠ 0 is a crm-core rule; it has to hold at the point of emission."""
    row = Candidate(company="Acme").csv_row()
    for col in (
        "Last Funding Amount (in USD)",
        "Total Funding Amount (in USD)",
        "Number of Funding Rounds",
    ):
        assert row[col] == "", f"{col} must be blank when unknown, got {row[col]!r}"


def test_x_handle_is_canonicalised_to_url():
    row = Candidate(company="Acme", x_handle="@acmehq").csv_row()
    assert row["X Handle"] == "https://x.com/acmehq"

    row = Candidate(company="Acme", x_handle="https://twitter.com/AcmeHQ").csv_row()
    assert row["X Handle"] == "https://x.com/acmehq"

    assert Candidate(company="Acme").csv_row()["X Handle"] == ""


def test_csv_row_keys_match_header_exactly():
    assert list(Candidate(company="Acme").csv_row().keys()) == CSV_COLUMNS


def test_research_scripts_have_no_notion_write_authority():
    """Research may query the database, but mutation belongs to CRM Core."""
    forbidden_probes = {
        "core writer import": (
            "from crm_core.scripts.crm_funding_handoff import main\n"
        ),
        "dynamic Core writer import": (
            "import importlib\n"
            "module = 'crm_' + 'intake'\n"
            "importlib.import_module(module)\n"
        ),
        "assigned dynamic Core import": (
            "import importlib\n"
            "load = importlib.import_module\n"
            "load('crm_intake')\n"
        ),
        "builtins dynamic Core import": (
            "import builtins\n"
            "load = builtins.__import__\n"
            "load('crm_intake')\n"
        ),
        "attribute assignment preserves dynamic importer": (
            "import importlib\n"
            "importlib.extra = helper\n"
            "importlib.import_module('crm_intake')\n"
        ),
        "Notion SDK writer": (
            "from notion import Client as Board\n"
            "Board(auth='token').pages.create(parent={})\n"
        ),
        "aliased direct page request": (
            "from urllib.request import Request as Build\n"
            "endpoint = 'https://api.notion.com/v1/' + 'pages'\n"
            "verb = 'POST'\n"
            "Build(endpoint, data=b'{}', method=verb)\n"
        ),
        "assigned direct page request": (
            "from urllib.request import Request\n"
            "Build = Request\n"
            "Build('https://api.notion.com/v1/pages', method='POST')\n"
        ),
        "module-qualified direct page request": (
            "import urllib.request\n"
            "endpoint = 'https://api.notion.com/v1/pages/page-id'\n"
            "urllib.request.Request(endpoint, data=b'{}', method='PATCH')\n"
        ),
        "attribute assignment preserves request module": (
            "import urllib.request\n"
            "urllib.request.extra = helper\n"
            "urllib.request.Request("
            "'https://api.notion.com/v1/pages', method='POST')\n"
        ),
        "direct PATCH page call": (
            "import requests\n"
            "page = 'https://api.notion.com/v1/pages/page-id'\n"
            "requests.patch(page, json={})\n"
        ),
        "explicit HTTPS port PATCH": (
            "import requests\n"
            "page = 'https://api.notion.com:443/v1/pages/page-id'\n"
            "requests.patch(page, json={})\n"
        ),
        "runtime page-id PATCH": (
            "import requests\n"
            "page_id = get_page_id()\n"
            "page = f'https://api.notion.com/v1/pages/{page_id}'\n"
            "requests.patch(page, json={})\n"
        ),
        "format page-id PATCH": (
            "import requests\n"
            "page_id = get_page_id()\n"
            "template = 'https://api.notion.com/v1/pages/{}'\n"
            "requests.patch(template.format(page_id), json={})\n"
        ),
        "percent page-id PATCH": (
            "import requests\n"
            "page_id = get_page_id()\n"
            "template = 'https://api.notion.com/v1/pages/%s'\n"
            "requests.patch(template % page_id, json={})\n"
        ),
        "destructured direct page call": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send, _ = requests.patch, None\n"
            "send(notion_url, json={})\n"
        ),
        "late-bound dynamic Core import": (
            "def load_writer():\n"
            "    load('crm_intake')\n"
            "load = __import__\n"
            "load_writer()\n"
        ),
        "Notion page method call": (
            "import notion as sdk\n"
            "client = sdk.Client(auth='token')\n"
            "client.pages.update(page_id='page-id', properties={})\n"
        ),
        "assigned Notion page method": (
            "import notion as sdk\n"
            "client = sdk.Client(auth='token')\n"
            "send = client.pages.update\n"
            "send(page_id='page-id', properties={})\n"
        ),
    }
    missed = {
        label: source
        for label, source in forbidden_probes.items()
        if not _notion_write_violations(source, label)
    }
    assert not missed, f"architecture check missed forbidden probes: {missed}"

    allowed_probes = {
        "read-only board prefilter": (
            "import urllib.request\n"
            "query = 'https://api.notion.com/v1/databases/db-id/query'\n"
            "urllib.request.Request(query, data=b'{}', method='POST')\n"
        ),
        "public Core handoff CLI": (
            "import subprocess\n"
            "subprocess.run(['python3', "
            "'/Core CRM/scripts/crm_funding_handoff.py', '--dry-run'])\n"
        ),
        "read-only Notion page GET": (
            "from urllib.request import Request\n"
            "Request('https://api.notion.com/v1/pages/page-id', method='GET')\n"
        ),
        "unrelated relative pages endpoint": (
            "import requests\n"
            "requests.post('/v1/pages', json={'site': 'internal wiki'})\n"
        ),
        "unrelated pages object": (
            "site.pages.create(title='documentation')\n"
        ),
        "for-target shadows page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "for send in [safe_send]:\n"
            "    send(notion_url)\n"
        ),
        "with-target shadows page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "with safe_sender() as send:\n"
            "    send(notion_url)\n"
        ),
        "except-target shadows page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "try:\n"
            "    raise SafeSender()\n"
            "except SafeSender as send:\n"
            "    send(notion_url)\n"
        ),
        "comprehension-target shadows page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "[send(notion_url) for send in safe_senders]\n"
        ),
    }
    rejected = {
        label: violations
        for label, source in allowed_probes.items()
        if (violations := _notion_write_violations(source, label))
    }
    assert not rejected, f"architecture check rejected allowed probes: {rejected}"

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    violations = []
    for path in scripts.rglob("*.py"):
        violations.extend(
            _notion_write_violations(
                path.read_text(encoding="utf-8"),
                str(path.relative_to(scripts.parent)),
            )
        )
    assert not violations, (
        "Research scripts must hand off to CRM Core instead of writing Notion: "
        f"{violations}"
    )
