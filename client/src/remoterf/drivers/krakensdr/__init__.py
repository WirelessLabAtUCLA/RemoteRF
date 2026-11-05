# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from importlib import import_module as _import_module

from . import krakensdr_remote as adi
from .krakensdr_remote import KrakenSDR

__all__ = ["adi", "KrakenSDR"]
krakensdr = _import_module("remoterf.drivers.support.krakensdr")
__all__.append("krakensdr")
if hasattr(krakensdr, "bind_client_class"):
    krakensdr.bind_client_class("KrakenSDR", KrakenSDR)
for _name in getattr(krakensdr, "__all__", ()):
    globals().setdefault(_name, getattr(krakensdr, _name))
    if _name not in __all__:
        __all__.append(_name)

