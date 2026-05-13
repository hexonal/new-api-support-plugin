def register(ctx):
    try:
        from .new_api_support_platform.adapter import register as _register
    except ImportError:
        from new_api_support_platform.adapter import register as _register

    return _register(ctx)

__all__ = ["register"]
