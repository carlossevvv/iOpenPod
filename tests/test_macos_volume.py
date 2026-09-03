from __future__ import annotations

import sys
import uuid

import pytest

from iopenpod.device import macos_volume


def test_mount_flag_names_match_mount_output_spelling() -> None:
    flags = 0x00000001 | 0x00000008 | 0x00000010 | 0x00001000 | 0x00800000
    assert macos_volume.mount_flag_names(flags) == ("read-only", "nosuid", "nodev", "local", "journaled")
    assert macos_volume.mount_flag_names(0) == ()


def test_device_id_strips_the_dev_prefix() -> None:
    facts = macos_volume.MacOSVolumeFacts(
        mount_path="/Volumes/iPod",
        device_node="/dev/disk4s2",
        filesystem_type="hfs",
        read_only=False,
        mount_options=(),
        block_size=16384,
        volume_uuid="",
    )
    assert facts.device_id == "disk4s2"


def test_read_mounted_volume_is_none_off_macos(monkeypatch) -> None:
    monkeypatch.setattr(macos_volume.sys, "platform", "linux")
    assert macos_volume.read_mounted_volume("/") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the live root volume through statfs/getattrlist")
def test_read_mounted_volume_describes_the_root_volume() -> None:
    facts = macos_volume.read_mounted_volume("/")
    assert facts is not None
    assert facts.mount_path == "/"
    assert facts.device_node.startswith("/dev/disk")
    assert facts.filesystem_type == "apfs"
    assert facts.block_size > 0
    assert uuid.UUID(facts.volume_uuid)
    assert macos_volume.read_mounted_volume("/definitely/not/mounted") is None
