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

from . import rfbeam_kmd7_remote as adi
from .rfbeam_kmd7_remote import RFBeamKMD7

__all__ = ["adi", "RFBeamKMD7"]
KMD7 = RFBeamKMD7
__all__.append("KMD7")
rfbeam_kmd7 = _import_module("remoterf.drivers.support.rfbeam_kmd7")
__all__.append("rfbeam_kmd7")
if hasattr(rfbeam_kmd7, "bind_client_class"):
    rfbeam_kmd7.bind_client_class("RFBeamKMD7", RFBeamKMD7)
for _name in getattr(rfbeam_kmd7, "__all__", ()):
    globals().setdefault(_name, getattr(rfbeam_kmd7, _name))
    if _name not in __all__:
        __all__.append(_name)

