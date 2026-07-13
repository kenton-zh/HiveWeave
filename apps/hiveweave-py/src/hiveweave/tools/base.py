"""Tool registry, decorator, and parameter model infrastructure.

This module provides the type-driven tool definition system:
- ``@tool`` decorator registers tools into ``_TOOL_REGISTRY``
- ``ToolDef`` wraps metadata + Pydantic params model + execute function
- ``to_llm_schema()`` auto-generates JSON Schema from the Pydantic model

Key invariant: the Pydantic model IS the schema. Field names in the model
must match field names read in ``execute_fn`` — the type system enforces this.
"""

from __future__ import annotations

from typing import Any, Callable, get_type_hints
from pydantic import BaseModel, Field, ConfigDict, ValidationError


# ── Registry ─────────────────────────────────────────────

_TOOL_REGISTRY: dict[str, "ToolDef"] = {}


def get_registry() -> dict[str, "ToolDef"]:
    """Return the global tool registry (for introspection/testing)."""
    return _TOOL_REGISTRY


def get_tool_def(name: str) -> "ToolDef | None":
    """Look up a tool definition by name."""
    return _TOOL_REGISTRY.get(name)


# ── ToolDef ──────────────────────────────────────────────


class ToolDef:
    """Tool definition: name + description + Pydantic params + execute fn + metadata.

    Attributes:
        name:           Tool name (as called by LLM).
        description:    Human/LLM-readable description.
        params_model:   Pydantic BaseModel subclass for parameter validation.
        execute_fn:     Async callable ``(params, agent_id, workspace) -> ToolResult | dict``.
        requires_workspace: Whether the tool needs a workspace path.
        security_level: ``"standard"`` | ``"file_op"`` | ``"shell"``.
    """

    def __init__(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        execute_fn: Callable,
        requires_workspace: bool = False,
        security_level: str = "standard",
    ):
        self.name = name
        self.description = description
        self.params_model = params_model
        self.execute_fn = execute_fn
        self.requires_workspace = requires_workspace
        self.security_level = security_level

    # ── schema generation ────────────────────────────────

    def to_llm_schema(self) -> dict[str, Any]:
        """Generate JSON Schema for LLM consumption.

        Strips Pydantic internals (title, $defs), keeps type/description/enum.
        Preserves ``aliases`` from field metadata for the alias resolver.
        """
        schema = self.params_model.model_json_schema()
        props: dict[str, Any] = {}
        for field_name, field_info in self.params_model.model_fields.items():
            # Build a clean property entry
            prop_schema = schema.get("properties", {}).get(field_name, {})
            cleaned: dict[str, Any] = {
                "type": _json_type_from_py(field_info.annotation),
            }
            if "description" in prop_schema:
                cleaned["description"] = prop_schema["description"]
            if "enum" in prop_schema:
                cleaned["enum"] = prop_schema["enum"]
            # Preserve aliases from field metadata
            aliases = _extract_aliases(field_info)
            if aliases:
                cleaned["aliases"] = aliases
            props[field_name] = cleaned

        required = [
            name
            for name, info in self.params_model.model_fields.items()
            if info.is_required()
        ]

        return {
            "description": self.description,
            "properties": props,
            "required": required,
        }

    # ── validation ───────────────────────────────────────

    def validate(self, raw_args: dict[str, Any]) -> tuple[BaseModel | None, str | None]:
        """Validate + alias-normalize raw args via the Pydantic model.

        Returns ``(model_instance, None)`` on success or ``(None, error_msg)``.
        The error message is formatted to be actionable for LLM self-correction:
        includes the field name, what went wrong, and the expected type/description.
        """
        try:
            normalized = self._normalize_aliases(raw_args)
            params = self.params_model(**normalized)
            return params, None
        except ValidationError as exc:
            # Build a concise, actionable error message for the LLM
            parts: list[str] = []
            for err in exc.errors():
                field = ".".join(str(x) for x in err["loc"])
                msg = err["msg"]
                # Look up field description for context
                field_info = self.params_model.model_fields.get(
                    field.split(".")[0]
                )
                desc = ""
                if field_info and field_info.description:
                    desc = f" ({field_info.description})"
                parts.append(f"'{field}': {msg}{desc}")
            return None, "; ".join(parts)
        except Exception as exc:
            return None, str(exc)

    def _normalize_aliases(self, raw_args: dict[str, Any]) -> dict[str, Any]:
        """Map alias names to canonical Pydantic field names.

        Tries direct field name first, then checks ``aliases`` metadata.
        Unknown keys pass through so Pydantic can report them.
        """
        result: dict[str, Any] = {}
        fields = self.params_model.model_fields

        for key, value in raw_args.items():
            if value is None:
                continue
            # Direct match
            if key in fields:
                result[key] = value
                continue
            # Check aliases
            found = False
            for field_name, field_info in fields.items():
                aliases = _extract_aliases(field_info)
                if key in aliases:
                    result[field_name] = value
                    found = True
                    break
            if not found:
                # Pass through; Pydantic will reject unknown fields
                result[key] = value

        return result


# ── Helpers ──────────────────────────────────────────────


def _extract_aliases(field_info) -> list[str]:
    """Extract aliases list from a Pydantic FieldInfo's metadata."""
    for item in field_info.metadata:
        if isinstance(item, dict) and "aliases" in item:
            return item["aliases"]
    # Also check json_schema_extra for aliases
    extra = field_info.json_schema_extra
    if isinstance(extra, dict) and "aliases" in extra:
        return extra["aliases"]
    return []


def _json_type_from_py(annotation: Any) -> str:
    """Map Python type annotations to JSON Schema type strings."""
    import typing

    # Unwrap Optional / Union
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            annotation = args[0]
            origin = typing.get_origin(annotation)

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    if annotation in type_map:
        return type_map[annotation]
    if origin in (list, typing.List):
        return "array"
    if origin in (dict, typing.Dict):
        return "object"
    return "string"  # safe default


# ── Decorator ────────────────────────────────────────────


def tool(
    name: str,
    description: str,
    requires_workspace: bool = False,
    security_level: str = "standard",
) -> Callable:
    """Register an async function as a tool.

    The function's first parameter must be a Pydantic BaseModel subclass
    (the params model). Remaining parameters are ``(agent_id, workspace)``.

    Example::

        class ReadFileParams(BaseModel):
            file_path: str = Field(alias="filePath")

        @tool("read_file", "Read a file.", security_level="file_op")
        async def read_file(params: ReadFileParams, agent_id: str, workspace: str):
            ...

    The Pydantic model is auto-extracted from the function's type hints.
    """

    def decorator(fn: Callable) -> Callable:
        # Extract the params model from the function's type hints
        hints = get_type_hints(fn)
        # First parameter (after self if method) should be the model
        import inspect

        sig = inspect.signature(fn)
        params_list = list(sig.parameters.values())
        # Skip 'self' if present
        start_idx = 1 if params_list and params_list[0].name == "self" else 0
        if len(params_list) <= start_idx:
            raise ValueError(
                f"@tool('{name}'): function must have at least a params parameter"
            )

        first_param_name = params_list[start_idx].name
        params_model = hints.get(first_param_name)
        if params_model is None or not (
            isinstance(params_model, type) and issubclass(params_model, BaseModel)
        ):
            raise ValueError(
                f"@tool('{name}'): first parameter '{first_param_name}' must be a "
                f"Pydantic BaseModel subclass, got {params_model}"
            )

        _TOOL_REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            params_model=params_model,
            execute_fn=fn,
            requires_workspace=requires_workspace,
            security_level=security_level,
        )
        return fn

    return decorator


# ── Schema helpers (for LLM prompt generation) ──────────


def get_tool_schema_for_llm(tool_name: str) -> dict[str, Any] | None:
    """Get JSON Schema for a tool, as seen by the LLM.

    Returns ``None`` if the tool is not in the registry.
    """
    td = _TOOL_REGISTRY.get(tool_name)
    if td is None:
        return None
    return td.to_llm_schema()


def get_all_tool_schemas_for_llm() -> dict[str, dict[str, Any]]:
    """Get schemas for all registered tools."""
    return {name: td.to_llm_schema() for name, td in _TOOL_REGISTRY.items()}


def list_tool_names() -> list[str]:
    """Return sorted list of all registered tool names."""
    return sorted(_TOOL_REGISTRY.keys())
