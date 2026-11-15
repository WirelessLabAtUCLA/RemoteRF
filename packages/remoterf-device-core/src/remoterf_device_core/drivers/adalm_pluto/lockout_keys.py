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

"""Pluto lockout init keys. No libiio import, so load checks for other drivers work without it."""

LOCKOUT_KEYS: frozenset[str] = frozenset({
    "tx_min_freq", "tx_max_freq", "admin_group_override", "tx_max_gain_db",
    "tx_enabled", "park_tx_lo", "allow_rf_loopback", "allow_dds",
    "tx_max_dds_scale", "allow_cyclic_tx", "tx_max_buffer_samples",
    "max_sample_rate", "tx_allowed_channels", "rx_min_freq", "rx_max_freq",
    "lockout_allow_methods",
})
