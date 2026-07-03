HELLO = {
    "name": "hello",
    "description": "Print a hello message.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name to greet",
            }
        },
        "required": ["name"],
    },
}
