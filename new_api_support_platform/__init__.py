def register(ctx):
    from .adapter import register as _register

    return _register(ctx)


__all__ = ["register"]
