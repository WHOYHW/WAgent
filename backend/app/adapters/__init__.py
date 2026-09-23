"""M4 数据适配层：屏蔽异构源系统差异，统一为内部数据契约 + 缓存 + 降级。"""

from .base import BaseAdapter, SourceNotConfiguredError
from .edu import EduAdapter
from .library import LibAdapter

__all__ = ["BaseAdapter", "SourceNotConfiguredError", "EduAdapter", "LibAdapter"]
