"""demo_desk: the smallest capability that exercises read, draft and external tools."""

from biz_cap_demo_desk.models import DemoRequest
from biz_cap_demo_desk.tools import register

__all__ = ["DemoRequest", "register"]
