from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="hello",
        toolset="hello",
        schema=schemas.HELLO,
        handler=tools.hello,
        description="Print a hello message.",
    )
