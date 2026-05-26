"""Unit tests for ``integrations.channels.room_capable`` (UNIF-G2 Mixin).

Pure interface contract — no I/O, no network.  Tests cover:
  - is_room_capable() correctly distinguishes Mixin'd vs non-Mixin'd adapters
  - default RoomCapableAdapter raises NotImplementedError (forces subclasses
    to implement)
  - default list_room_members returns [] (so adapters that can't list
    members don't have to override)
  - UnsupportedRoomError is a proper Exception subclass
"""
from __future__ import annotations

import asyncio
import unittest


class IsRoomCapableTest(unittest.TestCase):

    def test_plain_object_is_not_room_capable(self):
        from integrations.channels.room_capable import is_room_capable
        self.assertFalse(is_room_capable(object()))
        self.assertFalse(is_room_capable(None))
        self.assertFalse(is_room_capable("string-not-an-adapter"))

    def test_mixin_subclass_is_room_capable(self):
        from integrations.channels.room_capable import (
            RoomCapableAdapter, is_room_capable,
        )

        class FakeRoomAdapter(RoomCapableAdapter):
            pass

        self.assertTrue(is_room_capable(FakeRoomAdapter()))


class DefaultMixinTest(unittest.TestCase):

    def test_join_room_default_raises_not_implemented(self):
        from integrations.channels.room_capable import RoomCapableAdapter

        class Bare(RoomCapableAdapter):
            pass

        with self.assertRaises(NotImplementedError):
            asyncio.run(Bare().join_room('room-1'))

    def test_leave_room_default_raises_not_implemented(self):
        from integrations.channels.room_capable import RoomCapableAdapter

        class Bare(RoomCapableAdapter):
            pass

        with self.assertRaises(NotImplementedError):
            asyncio.run(Bare().leave_room('room-1'))

    def test_list_room_members_default_returns_empty(self):
        # list_room_members is optional — default empty list lets
        # adapters without member-listing skip the override.
        from integrations.channels.room_capable import RoomCapableAdapter

        class Bare(RoomCapableAdapter):
            pass

        result = asyncio.run(Bare().list_room_members('room-1'))
        self.assertEqual(result, [])


class UnsupportedRoomErrorTest(unittest.TestCase):

    def test_is_exception_subclass(self):
        from integrations.channels.room_capable import UnsupportedRoomError
        self.assertTrue(issubclass(UnsupportedRoomError, Exception))

    def test_carries_message(self):
        from integrations.channels.room_capable import UnsupportedRoomError
        try:
            raise UnsupportedRoomError("SMS does not support rooms")
        except UnsupportedRoomError as e:
            self.assertIn("SMS", str(e))


if __name__ == '__main__':
    unittest.main()
