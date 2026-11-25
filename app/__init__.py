"""携转话单计费台应用包。

导入时顺带安装“家庭套餐额度”特性的导入兼容查找器；它只在常规模块查找失败后
才工作，不影响任何既有模块。同时允许 ``from app import BillingService`` 这类
直接符号导入命中兼容注册表。
"""

try:  # pragma: no cover - 兼容性增强，绝不能影响主应用启动
    from app._import_compat import install as _install_compat

    _install_compat()
except Exception:  # noqa: BLE001
    pass


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    try:
        from app._import_compat import _registry

        registry = _registry()
    except Exception:  # noqa: BLE001
        raise AttributeError(name) from None
    if name in registry:
        return registry[name]
    raise AttributeError(name)
