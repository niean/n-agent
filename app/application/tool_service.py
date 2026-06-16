from __future__ import annotations

from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


class ToolService:
    def __init__(self, executor: ToolExecutor, definitions: list[ToolDefinition]):
        self.executor = executor
        self.definitions = {definition.name: definition for definition in definitions}
        self.dynamic_definitions: dict[str, dict[str, ToolDefinition]] = {}

    def set_dynamic_definitions(self, source_key: str, definitions: list[ToolDefinition]) -> None:
        static_names = set(self.definitions)
        dynamic: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in static_names:
                continue
            if definition.name in dynamic:
                continue
            if not isinstance(definition.input_schema, dict) or definition.input_schema.get("type") != "object":
                continue
            dynamic[definition.name] = definition
        self.dynamic_definitions[source_key] = dynamic

    def list_definitions(self) -> list[ToolDefinition]:
        definitions = list(self.definitions.values())
        for dynamic in self.dynamic_definitions.values():
            definitions.extend(dynamic.values())
        return definitions

    def _definition(self, name: str) -> ToolDefinition | None:
        if name in self.definitions:
            return self.definitions[name]
        for dynamic in self.dynamic_definitions.values():
            if name in dynamic:
                return dynamic[name]
        return None

    def list_openai_tools(self, risk_level: RiskLevel | None = None) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_schema,
                },
            }
            for definition in self.list_definitions()
            if definition.enabled
            and (definition.risk_level is risk_level if risk_level else definition.risk_level is not RiskLevel.DANGEROUS)
            and isinstance(definition.input_schema, dict)
            and definition.input_schema.get("type") == "object"
        ]

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        definition = self._definition(request.name)
        if definition is None:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "tool not found"})
        if definition.risk_level is RiskLevel.CONFIRM and not _is_confirm_allowed(request, context):
            return ToolResult(request.id, request.name, ToolResultStatus.PERMISSION_DENIED, {"error": "permission_denied"})
        if definition.risk_level is RiskLevel.DANGEROUS or not definition.enabled:
            return ToolResult(request.id, request.name, ToolResultStatus.PERMISSION_DENIED, {"error": "permission_denied"})
        try:
            return await self.executor.execute(request, context)
        except TypeError:
            return await self.executor.execute(request)


def _is_confirm_allowed(request: ToolCallRequest, context: ToolExecutionContext | None) -> bool:
    if context is None:
        return False
    expected = context.allowed_confirm_tools.get(request.name)
    if expected is None:
        return False
    for key, value in expected.items():
        if request.arguments.get(key) != value:
            return False
    return True


def knowledge_tool_definitions(enabled: bool = True) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="search_knowledge",
            description="Search the N-KB general knowledge base for relevant snippets.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            enabled=enabled,
            source_type=ToolSourceType.KNOWLEDGE,
            toolset="knowledge",
        )
    ]


def builtin_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_current_time",
            description="Get the current UTC time.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source_type=ToolSourceType.BUILTIN,
            toolset="system",
        ),
        ToolDefinition(
            name="calculator",
            description="Evaluate a safe arithmetic expression.",
            input_schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="math",
        ),
        ToolDefinition(
            name="list_directory",
            description="List entries in a directory under the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="file",
        ),
        ToolDefinition(
            name="read_text_file",
            description="Read a text file under the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="file",
        ),
    ]
