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

from . import wifi_remote as adi
from .wifi_remote import WifiDongle

__all__ = ["adi", "WifiDongle"]
wifi = _import_module("remoterf.drivers.support.wifi")
__all__.append("wifi")
if hasattr(wifi, "bind_client_class"):
    wifi.bind_client_class("WifiDongle", WifiDongle)
for _name in getattr(wifi, "__all__", ()):
    globals().setdefault(_name, getattr(wifi, _name))
    if _name not in __all__:
        __all__.append(_name)

