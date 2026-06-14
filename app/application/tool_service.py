from __future__ import annotations

from app.domain.tool import RiskLevel, ToolCallRequest, ToolDefinition, ToolExecutor, ToolResult, ToolResultStatus


class ToolService:
    def __init__(self, executor: ToolExecutor, definitions: list[ToolDefinition]):
        self.executor = executor
        self.definitions = {definition.name: definition for definition in definitions}

    def list_definitions(self) -> list[ToolDefinition]:
        return list(self.definitions.values())

    def list_openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_schema,
                },
            }
            for definition in self.definitions.values()
            if definition.enabled and definition.risk_level is not RiskLevel.DANGEROUS
        ]

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        definition = self.definitions.get(request.name)
        if definition is None:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "tool not found"})
        if definition.risk_level is RiskLevel.CONFIRM:
            return ToolResult(request.id, request.name, ToolResultStatus.PERMISSION_DENIED, {"error": "permission_denied"})
        if definition.risk_level is RiskLevel.DANGEROUS or not definition.enabled:
            return ToolResult(request.id, request.name, ToolResultStatus.PERMISSION_DENIED, {"error": "permission_denied"})
        return await self.executor.execute(request)


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
        )
    ]


def builtin_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_current_time",
            description="Get the current UTC time.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
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
        ),
    ]
