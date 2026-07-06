"""LangBot compatibility: Command errors stub.

Replaces `langbot_plugin.api.entities.builtin.command.errors`.
Only the minimal types used by copied adapters.
"""


class CommandError(Exception):
    """命令错误基类。"""
    pass


class CommandNotFoundError(CommandError):
    """命令未找到。"""
    pass


class CommandPermissionError(CommandError):
    """命令权限错误。"""
    pass
