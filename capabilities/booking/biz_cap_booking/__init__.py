"""booking: offer free slots and book meetings through the calendar port."""

from biz_cap_booking.models import Meeting
from biz_cap_booking.tools import register

__all__ = ["Meeting", "register"]
