def hello(args, **kwargs):
    name = args.get("name", "plugin")
    return {"message": f"Hello, {name}!"}
