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


class _FunctionBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.locals: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.locals.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.locals.update(
            item.asname or item.name.split(".", 1)[0]
            for item in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.locals.update(item.asname or item.name for item in node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.locals.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.locals.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.locals.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        return

    def visit_SetComp(self, node: ast.SetComp) -> None:
        return

    def visit_DictComp(self, node: ast.DictComp) -> None:
        return

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        return

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.locals.add(node.name)
        self.generic_visit(node)


def _function_binding_names(
    body: list[ast.stmt],
) -> tuple[set[str], set[str], set[str]]:
    collector = _FunctionBindingCollector()
    for statement in body:
        collector.visit(statement)
    return collector.locals, collector.globals, collector.nonlocals


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
        self._final_module_aliases: dict[str, str | None] = {}
        self._final_module_strings: dict[str, str | None] = {}
        self._final_module_notion: dict[str, bool] = {}
        self._final_module_page_callables: dict[str, bool] = {}
        self._function_depth = 0
        self._function_globals: list[set[str]] = []
        self._function_nonlocals: list[set[str]] = []
        self._function_effects: list[bool] = []
        self._function_definition_scopes: list[
            dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
        ] = [{}]
        self._active_function_calls: set[int] = set()
        self._function_exit_states: list[list[tuple]] = []
        self._function_module_states: list[
            tuple[
                dict[str, str | None],
                dict[str, str | None],
                dict[str, bool],
                dict[str, bool],
            ]
        ] = []

    def visit_Module(self, node: ast.Module) -> None:
        self._build_final_module_environment(node.body)
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
        self._function_definition_scopes[-1][node.name] = node
        self._bind_target(node, qualified=None, string=None, name=node.name)
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function_definition_scopes[-1][node.name] = node
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

    def visit_If(self, node: ast.If) -> None:
        self._visit_if_statement(node)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        elements = self._literal_iterable_elements(node.iter)
        if elements is not None:
            for element in elements:
                self._bind_assignment(node.target, element)
                self._visit_block(node.body)
            self._visit_block(node.orelse)
            return
        before = self._capture_target_bindings(node.target)
        self._invalidate_target(node.target)
        self._visit_block(node.body)
        after = self._capture_target_bindings(node.target)
        self._restore_merged_target_bindings(before, after)
        self._visit_block(node.orelse)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate_target(item.optional_vars)
        self._visit_block(node.body)

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
        self._visit_block(node.body)

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
        self._visit_registered_function_call(called)
        if called and _is_writer_symbol(called):
            self._record(node, f"calls or constructs writer {called}")

        if _is_dynamic_importer(called):
            imported = self._argument_string(node, 0, "name")
            if imported and _is_writer_symbol(imported):
                self._record(node, f"dynamically imports writer {imported}")

        if self._is_notion_page_callable(node.func):
            self._record(node, f"calls Notion page writer {called}")

        method, url = self._http_operation(node, parts)
        if method in {"POST", "PATCH"} and _is_notion_page_url(url):
            self._record(node, f"constructs direct Notion {method} page operation")
        self.generic_visit(node)

    def _visit_registered_function_call(self, called: str | None) -> None:
        if called is None:
            return
        definition = self._registered_function(called)
        if definition is None or id(definition) in self._active_function_calls:
            return
        module_caller = self._function_depth == 0
        if self._function_depth and self._function_module_states:
            state = self._function_module_states[-1]
        elif self._function_depth:
            state = self._final_module_state()
        else:
            state = self._module_state()
        self._active_function_calls.add(id(definition))
        self._function_module_states.append(state)
        try:
            self._visit_function(definition, propagate_effects=True)
        finally:
            self._function_module_states.pop()
            self._active_function_calls.remove(id(definition))
        if module_caller:
            self._restore_module_state(state)

    def _registered_function(
        self,
        name: str,
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        return next(
            (
                scope[name]
                for scope in reversed(self._function_definition_scopes)
                if name in scope
            ),
            None,
        )

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
        *,
        propagate_effects: bool = False,
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        local_names, global_names, nonlocal_names = _function_binding_names(
            node.body
        )
        self._push_scope()
        self._function_definition_scopes.append({})
        self._function_depth += 1
        self._function_globals.append(global_names)
        self._function_nonlocals.append(nonlocal_names)
        self._function_effects.append(propagate_effects)
        self._function_exit_states.append([])
        self._bind_arguments(node.args)
        for local in local_names - global_names - nonlocal_names:
            self._bind_name(
                local,
                qualified=None,
                string=None,
                notion=False,
                page_callable=False,
            )
        terminator = self._visit_block(node.body)
        if terminator is None:
            self._function_exit_states[-1].append(self._analysis_state())
        if propagate_effects:
            self._restore_function_exit_effects(
                self._function_exit_states[-1]
            )
        self._function_exit_states.pop()
        self._function_effects.pop()
        self._function_nonlocals.pop()
        self._function_globals.pop()
        self._function_depth -= 1
        self._function_definition_scopes.pop()
        self._pop_scope()

    def _build_final_module_environment(self, body: list[ast.stmt]) -> None:
        self._apply_module_statements(body)
        self._final_module_aliases = dict(self._alias_scopes[0])
        self._final_module_strings = dict(self._string_scopes[0])
        self._final_module_notion = dict(self._notion_scopes[0])
        self._final_module_page_callables = dict(
            self._page_callable_scopes[0]
        )
        self._alias_scopes[0].clear()
        self._string_scopes[0].clear()
        self._notion_scopes[0].clear()
        self._page_callable_scopes[0].clear()

    def _apply_module_statements(self, body: list[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    self._bind_import(item)
            elif isinstance(statement, ast.ImportFrom):
                module = statement.module or ""
                for item in statement.names:
                    self._bind_import_from(module, item)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    self._bind_assignment(target, statement.value)
            elif isinstance(statement, ast.AnnAssign):
                if statement.value is None:
                    self._invalidate_target(statement.target)
                else:
                    self._bind_assignment(statement.target, statement.value)
            elif isinstance(statement, ast.If):
                self._apply_module_branches(
                    statement.body,
                    statement.orelse,
                )
            elif isinstance(statement, ast.Try):
                branches = [
                    [*statement.body, *statement.orelse],
                    *(handler.body for handler in statement.handlers),
                ]
                self._apply_module_branch_set(branches)
                self._apply_module_statements(statement.finalbody)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    if item.optional_vars is not None:
                        self._invalidate_target(item.optional_vars)
                self._apply_module_statements(statement.body)

    def _apply_module_branches(
        self,
        body: list[ast.stmt],
        orelse: list[ast.stmt],
    ) -> None:
        self._apply_module_branch_set((body, orelse))

    def _apply_module_branch_set(
        self,
        branches: object,
    ) -> None:
        before = self._module_state()
        outcomes = []
        for branch in branches:
            self._restore_module_state(before)
            self._apply_module_statements(list(branch))
            outcomes.append(self._module_state())
        self._restore_module_state(self._merge_module_states(outcomes))

    def _module_state(
        self,
    ) -> tuple[
        dict[str, str | None],
        dict[str, str | None],
        dict[str, bool],
        dict[str, bool],
    ]:
        return (
            dict(self._alias_scopes[0]),
            dict(self._string_scopes[0]),
            dict(self._notion_scopes[0]),
            dict(self._page_callable_scopes[0]),
        )

    def _final_module_state(
        self,
    ) -> tuple[
        dict[str, str | None],
        dict[str, str | None],
        dict[str, bool],
        dict[str, bool],
    ]:
        return (
            dict(self._final_module_aliases),
            dict(self._final_module_strings),
            dict(self._final_module_notion),
            dict(self._final_module_page_callables),
        )

    def _restore_module_state(
        self,
        state: tuple[
            dict[str, str | None],
            dict[str, str | None],
            dict[str, bool],
            dict[str, bool],
        ],
    ) -> None:
        (
            self._alias_scopes[0],
            self._string_scopes[0],
            self._notion_scopes[0],
            self._page_callable_scopes[0],
        ) = tuple(dict(part) for part in state)

    def _merge_module_states(
        self,
        states: list[
            tuple[
                dict[str, str | None],
                dict[str, str | None],
                dict[str, bool],
                dict[str, bool],
            ]
        ],
    ) -> tuple[
        dict[str, str | None],
        dict[str, str | None],
        dict[str, bool],
        dict[str, bool],
    ]:
        if not states:
            return self._module_state()
        alias_keys = set().union(*(state[0] for state in states))
        string_keys = set().union(*(state[1] for state in states))
        bool_keys = set().union(
            *(state[2] for state in states),
            *(state[3] for state in states),
        )
        aliases = {
            key: self._merge_alias_values(
                [state[0].get(key) for state in states]
            )
            for key in alias_keys
        }
        strings = {
            key: self._merge_string_values(
                [state[1].get(key) for state in states]
            )
            for key in string_keys
        }
        notions = {
            key: any(state[2].get(key, False) for state in states)
            for key in bool_keys
        }
        page_callables = {
            key: any(state[3].get(key, False) for state in states)
            for key in bool_keys
        }
        return aliases, strings, notions, page_callables

    def _visit_block(self, statements: list[ast.stmt]) -> str | None:
        for statement in statements:
            if isinstance(statement, ast.If):
                terminator = self._visit_if_statement(statement)
            else:
                self.visit(statement)
                if (
                    isinstance(statement, (ast.Return, ast.Raise))
                    and self._function_exit_states
                ):
                    self._function_exit_states[-1].append(
                        self._analysis_state()
                    )
                terminator = (
                    statement.__class__.__name__.casefold()
                    if isinstance(
                        statement,
                        (ast.Return, ast.Raise, ast.Break, ast.Continue),
                    )
                    else None
                )
            if terminator is not None:
                return terminator
        return None

    def _visit_if_statement(self, node: ast.If) -> str | None:
        self.visit(node.test)
        before = self._analysis_state()
        continuing = []
        terminators = []
        if (
            isinstance(node.test, ast.Constant)
            and isinstance(node.test.value, bool)
        ):
            branches = (node.body if node.test.value else node.orelse,)
        else:
            branches = (node.body, node.orelse)
        for branch in branches:
            self._restore_analysis_state(before)
            terminator = self._visit_block(branch)
            if terminator is None:
                continuing.append(self._analysis_state())
            else:
                terminators.append(terminator)
        if continuing:
            self._restore_analysis_state(
                self._merge_analysis_states(continuing)
            )
            return None
        self._restore_analysis_state(before)
        return terminators[0] if terminators else None

    def _restore_function_exit_effects(self, states: list[tuple]) -> None:
        if not states:
            return
        merged = self._merge_analysis_states(states)
        active_module = merged[0]
        if active_module is not None and self._function_module_states:
            current = self._function_module_states[-1]
            for target, source in zip(current, active_module):
                target.clear()
                target.update(source)
        for name in self._function_nonlocals[-1]:
            for index in range(len(self._alias_scopes) - 2, 0, -1):
                if any(
                    name in scopes[index]
                    for scopes in (
                        self._alias_scopes,
                        self._string_scopes,
                        self._notion_scopes,
                        self._page_callable_scopes,
                    )
                ):
                    self._write_scope_binding(
                        index,
                        name,
                        qualified=merged[1][index].get(name),
                        string=merged[2][index].get(name),
                        notion=merged[3][index].get(name, False),
                        page_callable=merged[4][index].get(name, False),
                    )
                    break

    def _analysis_state(self) -> tuple[
        tuple[
            dict[str, str | None],
            dict[str, str | None],
            dict[str, bool],
            dict[str, bool],
        ]
        | None,
        list[dict[str, str | None]],
        list[dict[str, str | None]],
        list[dict[str, bool]],
        list[dict[str, bool]],
    ]:
        active_module = (
            tuple(
                dict(part)
                for part in self._function_module_states[-1]
            )
            if self._function_module_states
            else None
        )
        return (
            active_module,
            [dict(scope) for scope in self._alias_scopes],
            [dict(scope) for scope in self._string_scopes],
            [dict(scope) for scope in self._notion_scopes],
            [dict(scope) for scope in self._page_callable_scopes],
        )

    def _restore_analysis_state(
        self,
        state: tuple[
            tuple[
                dict[str, str | None],
                dict[str, str | None],
                dict[str, bool],
                dict[str, bool],
            ]
            | None,
            list[dict[str, str | None]],
            list[dict[str, str | None]],
            list[dict[str, bool]],
            list[dict[str, bool]],
        ],
    ) -> None:
        active_module, aliases, strings, notions, page_callables = state
        if active_module is not None and self._function_module_states:
            current = self._function_module_states[-1]
            for target, source in zip(current, active_module):
                target.clear()
                target.update(source)
        self._alias_scopes[:] = [dict(scope) for scope in aliases]
        self._string_scopes[:] = [dict(scope) for scope in strings]
        self._notion_scopes[:] = [dict(scope) for scope in notions]
        self._page_callable_scopes[:] = [
            dict(scope) for scope in page_callables
        ]

    def _merge_analysis_states(
        self,
        states: list[
            tuple[
                tuple[
                    dict[str, str | None],
                    dict[str, str | None],
                    dict[str, bool],
                    dict[str, bool],
                ]
                | None,
                list[dict[str, str | None]],
                list[dict[str, str | None]],
                list[dict[str, bool]],
                list[dict[str, bool]],
            ]
        ],
    ) -> tuple[
        tuple[
            dict[str, str | None],
            dict[str, str | None],
            dict[str, bool],
            dict[str, bool],
        ]
        | None,
        list[dict[str, str | None]],
        list[dict[str, str | None]],
        list[dict[str, bool]],
        list[dict[str, bool]],
    ]:
        active_modules = [
            state[0] for state in states if state[0] is not None
        ]
        active_module = (
            self._merge_module_states(active_modules)
            if active_modules
            else None
        )
        return (
            active_module,
            self._merge_scope_states([state[1] for state in states], False),
            self._merge_scope_states([state[2] for state in states], True),
            self._merge_scope_states([state[3] for state in states], None),
            self._merge_scope_states([state[4] for state in states], None),
        )

    def _merge_scope_states(
        self,
        states: list[list[dict[str, object]]],
        value_kind: bool | None,
    ) -> list[dict[str, object]]:
        merged = []
        for index in range(len(states[0])):
            scopes = [state[index] for state in states]
            keys = set().union(*scopes)
            if value_kind is False:
                merged.append(
                    {
                        key: self._merge_alias_values(
                            [scope.get(key) for scope in scopes]
                        )
                        for key in keys
                    }
                )
            elif value_kind is True:
                merged.append(
                    {
                        key: self._merge_string_values(
                            [scope.get(key) for scope in scopes]
                        )
                        for key in keys
                    }
                )
            else:
                merged.append(
                    {
                        key: any(bool(scope.get(key)) for scope in scopes)
                        for key in keys
                    }
                )
        return merged

    @staticmethod
    def _merge_alias_values(values: list[str | None]) -> str | None:
        if all(value == values[0] for value in values):
            return values[0]
        risky = [value for value in values if _is_risky_callable(value)]
        return risky[0] if risky else None

    @staticmethod
    def _merge_string_values(values: list[str | None]) -> str | None:
        if all(value == values[0] for value in values):
            return values[0]
        notion_urls = [value for value in values if _is_notion_page_url(value)]
        return notion_urls[0] if notion_urls else None

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
        qualified, string, notion, page_callable = self._binding_for_value(
            value
        )
        self._bind_target(
            target,
            qualified=qualified,
            string=string,
            notion=notion,
            page_callable=page_callable,
        )

    def _binding_for_value(
        self,
        value: ast.AST,
    ) -> tuple[str | None, str | None, bool, bool]:
        if isinstance(value, ast.IfExp):
            left = self._binding_for_value(value.body)
            right = self._binding_for_value(value.orelse)
            return (
                self._merge_alias_values([left[0], right[0]]),
                self._merge_string_values([left[1], right[1]]),
                left[2] or right[2],
                left[3] or right[3],
            )
        return (
            self._resolved_assignment_qualified(value),
            self._string_pattern(value),
            self._is_notion_value(value),
            self._is_notion_page_callable(value),
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
        if self._function_depth and self._function_effects[-1]:
            if (
                name in self._function_globals[-1]
                and self._function_module_states
            ):
                self._write_state_binding(
                    self._function_module_states[-1],
                    name,
                    qualified=qualified,
                    string=string,
                    notion=notion,
                    page_callable=page_callable,
                )
                return
            if name in self._function_nonlocals[-1]:
                for index in range(len(self._alias_scopes) - 2, 0, -1):
                    if any(
                        name in scopes[index]
                        for scopes in (
                            self._alias_scopes,
                            self._string_scopes,
                            self._notion_scopes,
                            self._page_callable_scopes,
                        )
                    ):
                        self._write_scope_binding(
                            index,
                            name,
                            qualified=qualified,
                            string=string,
                            notion=notion,
                            page_callable=page_callable,
                        )
                        return
        self._write_scope_binding(
            len(self._alias_scopes) - 1,
            name,
            qualified=qualified,
            string=string,
            notion=notion,
            page_callable=page_callable,
        )

    def _write_scope_binding(
        self,
        index: int,
        name: str,
        *,
        qualified: str | None,
        string: str | None,
        notion: bool,
        page_callable: bool,
    ) -> None:
        self._alias_scopes[index][name] = qualified
        self._string_scopes[index][name] = string
        self._notion_scopes[index][name] = notion
        self._page_callable_scopes[index][name] = page_callable

    @staticmethod
    def _write_state_binding(
        state: tuple[
            dict[str, str | None],
            dict[str, str | None],
            dict[str, bool],
            dict[str, bool],
        ],
        name: str,
        *,
        qualified: str | None,
        string: str | None,
        notion: bool,
        page_callable: bool,
    ) -> None:
        state[0][name] = qualified
        state[1][name] = string
        state[2][name] = notion
        state[3][name] = page_callable

    def _invalidate_target(self, target: ast.AST) -> None:
        self._bind_target(
            target,
            qualified=None,
            string=None,
            notion=False,
            page_callable=False,
        )

    def _capture_target_bindings(
        self,
        target: ast.AST,
    ) -> dict[str, tuple[str | None, str | None, bool, bool]]:
        return {
            name: (
                self._lookup(self._alias_scopes, name),
                self._lookup(self._string_scopes, name),
                self._lookup_bool(self._notion_scopes, name),
                self._lookup_bool(self._page_callable_scopes, name),
            )
            for name in _assignment_target_names(target)
        }

    def _restore_merged_target_bindings(
        self,
        before: dict[str, tuple[str | None, str | None, bool, bool]],
        after: dict[str, tuple[str | None, str | None, bool, bool]],
    ) -> None:
        for name in before.keys() | after.keys():
            left = before.get(name, (None, None, False, False))
            right = after.get(name, (None, None, False, False))
            self._bind_name(
                name,
                qualified=self._merge_alias_values([left[0], right[0]]),
                string=self._merge_string_values([left[1], right[1]]),
                notion=left[2] or right[2],
                page_callable=left[3] or right[3],
            )

    def _is_notion_value(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self._lookup_bool(self._notion_scopes, node.id)
        if isinstance(node, ast.Attribute):
            return self._is_notion_value(node.value) or _is_notion_sdk_symbol(
                self._qualified_name(node) or ""
            )
        if isinstance(node, ast.Call):
            imported = self._dynamically_imported_module(node)
            return (
                (imported is not None and _is_notion_sdk_symbol(imported))
                or self._is_notion_value(node.func)
                or _is_notion_sdk_symbol(
                    self._qualified_name(node.func) or ""
                )
            )
        return False

    def _resolved_assignment_qualified(
        self,
        value: ast.AST,
    ) -> str | None:
        if isinstance(value, ast.Call):
            imported = self._dynamically_imported_module(value)
            if imported is not None:
                return imported
        return self._qualified_name(value)

    def _dynamically_imported_module(self, node: ast.Call) -> str | None:
        if not _is_dynamic_importer(self._qualified_name(node.func)):
            return None
        return self._argument_string(node, 0, "name")

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
        self._visit_comprehension_level(generators, outputs, 0)
        self._pop_scope()

    def _visit_comprehension_level(
        self,
        generators: list[ast.comprehension],
        outputs: tuple[ast.AST, ...],
        index: int,
    ) -> None:
        if index == len(generators):
            for output in outputs:
                self.visit(output)
            return
        generator = generators[index]
        self.visit(generator.iter)
        elements = self._literal_iterable_elements(generator.iter)
        if elements is None:
            self._invalidate_target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
            self._visit_comprehension_level(generators, outputs, index + 1)
            return
        for element in elements:
            self._bind_assignment(generator.target, element)
            for condition in generator.ifs:
                self.visit(condition)
            self._visit_comprehension_level(generators, outputs, index + 1)

    def _literal_iterable_elements(
        self,
        node: ast.AST,
    ) -> list[ast.AST] | None:
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            return list(node.elts)
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set"
            and not node.args
            and not node.keywords
        ):
            return None
        definition = self._registered_function("set")
        if definition is not None:
            if (
                len(definition.body) == 1
                and isinstance(definition.body[0], ast.Return)
                and isinstance(
                    definition.body[0].value,
                    (ast.List, ast.Set, ast.Tuple),
                )
            ):
                return list(definition.body[0].value.elts)
            return None
        if self._is_name_bound("set"):
            return None
        return []

    def _is_name_bound(self, name: str) -> bool:
        if any(name in scope for scope in self._alias_scopes):
            return True
        if self._function_depth:
            state = (
                self._function_module_states[-1]
                if self._function_module_states
                else self._final_module_state()
            )
            return name in state[0]
        return name in self._final_module_aliases

    def _lookup(
        self,
        scopes: list[dict[str, str | None]],
        name: str,
    ) -> str | None:
        lower_bound = 1 if self._function_depth else 0
        for scope in reversed(scopes[lower_bound:]):
            if name in scope:
                return scope[name]
        if self._function_depth:
            state = (
                self._function_module_states[-1]
                if self._function_module_states
                else self._final_module_state()
            )
            if scopes is self._alias_scopes:
                return state[0].get(name)
            if scopes is self._string_scopes:
                return state[1].get(name)
        return None

    def _lookup_bool(
        self,
        scopes: list[dict[str, bool]],
        name: str,
    ) -> bool:
        lower_bound = 1 if self._function_depth else 0
        for scope in reversed(scopes[lower_bound:]):
            if name in scope:
                return scope[name]
        if self._function_depth:
            state = (
                self._function_module_states[-1]
                if self._function_module_states
                else self._final_module_state()
            )
            if scopes is self._notion_scopes:
                return state[2].get(name, False)
            if scopes is self._page_callable_scopes:
                return state[3].get(name, False)
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


def _is_dynamic_importer(qualified: str | None) -> bool:
    return qualified in {
        "__import__",
        "builtins.__import__",
        "importlib.import_module",
    }


def _is_risky_callable(qualified: str | None) -> bool:
    if not qualified:
        return False
    if _is_dynamic_importer(qualified) or _is_writer_symbol(qualified):
        return True
    parts = qualified.casefold().split(".")
    return (
        parts[-1] in {"post", "patch", "request", "urlopen"}
        or parts[-2:] in (["pages", "create"], ["pages", "update"])
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


def _assignment_target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _assignment_target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        names: set[str] = set()
        for item in target.elts:
            names.update(_assignment_target_names(item))
        return names
    return set()


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
        "final writer global reaches function": (
            "load = safe\n"
            "def load_writer():\n"
            "    load('crm_intake')\n"
            "load = __import__\n"
            "load_writer()\n"
        ),
        "writer global active at function call": (
            "def load_writer():\n"
            "    load('crm_intake')\n"
            "load = __import__\n"
            "load_writer()\n"
            "load = safe\n"
        ),
        "literal loop binds page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "for send in [requests.patch]:\n"
            "    send(notion_url)\n"
        ),
        "literal comprehension binds page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "[send(notion_url) for send in [requests.patch]]\n"
        ),
        "unknown loop preserves prior page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "for send in safe_senders:\n"
            "    pass\n"
            "send(notion_url)\n"
        ),
        "empty loop preserves page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "for send in []:\n"
            "    pass\n"
            "send(notion_url)\n"
        ),
        "empty set loop preserves page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "for send in set():\n"
            "    pass\n"
            "send(notion_url)\n"
        ),
        "conditional literal loop may bind page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "for send in [requests.patch if enabled else safe]:\n"
            "    send(notion_url)\n"
        ),
        "global page writer assignment": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "def dispatch():\n"
            "    global send\n"
            "    send = requests.patch\n"
            "    send(notion_url)\n"
            "dispatch()\n"
        ),
        "global writer survives function return": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = safe\n"
            "def configure():\n"
            "    global send\n"
            "    send = requests.patch\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "conditional global safe reset preserves writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "def configure():\n"
            "    global send\n"
            "    if enabled:\n"
            "        send = safe\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "unreachable global safe reset preserves writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "def configure():\n"
            "    global send\n"
            "    return\n"
            "    send = safe\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "returning branch global writer survives function": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = safe\n"
            "def configure():\n"
            "    global send\n"
            "    if enabled:\n"
            "        send = requests.patch\n"
            "        return\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "nonlocal writer survives nested return": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "def dispatch():\n"
            "    send = safe\n"
            "    def configure():\n"
            "        nonlocal send\n"
            "        send = requests.patch\n"
            "    configure()\n"
            "    send(notion_url)\n"
            "dispatch()\n"
        ),
        "conditional nonlocal safe reset preserves writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "def dispatch():\n"
            "    send = requests.patch\n"
            "    def configure():\n"
            "        nonlocal send\n"
            "        if enabled:\n"
            "            send = safe\n"
            "    configure()\n"
            "    send(notion_url)\n"
            "dispatch()\n"
        ),
        "returning branch nonlocal writer survives nested function": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "def dispatch():\n"
            "    send = safe\n"
            "    def configure():\n"
            "        nonlocal send\n"
            "        if enabled:\n"
            "            send = requests.patch\n"
            "            return\n"
            "    configure()\n"
            "    send(notion_url)\n"
            "dispatch()\n"
        ),
        "shadowed set may yield page writer": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "def set():\n"
            "    return [requests.patch]\n"
            "for send in set():\n"
            "    send(notion_url)\n"
        ),
        "conditional final writer global": (
            "load = safe\n"
            "if enabled:\n"
            "    load = __import__\n"
            "def load_writer():\n"
            "    load('crm_intake')\n"
            "load_writer()\n"
        ),
        "dynamic Notion SDK import": (
            "import importlib\n"
            "sdk = importlib.import_module('notion')\n"
            "client = sdk.Client(auth='token')\n"
            "client.pages.create(parent={})\n"
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
        "final safe global reaches function": (
            "load = __import__\n"
            "def load_writer():\n"
            "    load('crm_intake')\n"
            "load = safe\n"
            "load_writer()\n"
        ),
        "global safe reset survives function return": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "def configure():\n"
            "    global send\n"
            "    send = safe\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "literal true global safe reset": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = requests.patch\n"
            "def configure():\n"
            "    global send\n"
            "    if True:\n"
            "        send = safe\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "literal false global writer assignment": (
            "import requests\n"
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "send = safe\n"
            "def configure():\n"
            "    global send\n"
            "    if False:\n"
            "        send = requests.patch\n"
            "configure()\n"
            "send(notion_url)\n"
        ),
        "genuine builtin empty set has no loop body": (
            "notion_url = 'https://api.notion.com/v1/pages/page-id'\n"
            "for send in set():\n"
            "    send(notion_url)\n"
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
