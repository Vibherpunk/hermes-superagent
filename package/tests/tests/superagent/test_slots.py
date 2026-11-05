"""Tests for SlotPool controller per E-023 and Card 0.4."""

import pytest
from gateway.kanban_watchers_dispatcher import SlotPool


class TestSlotPool:
    """Unit tests for SlotPool capacity, reserve slot semantics, and release."""

    def test_new_slot_pool_starts_empty(self):
        pool = SlotPool(worker_slots=4, reserve_slots=1)
        assert pool.in_use() == 0
        assert pool.capacity == 5

    def test_fill_worker_slots_refuses_next_launch(self):
        pool = SlotPool(worker_slots=2, reserve_slots=1)
        t1 = pool.try_acquire("launch")
        t2 = pool.try_acquire("launch")
        assert t1 is not None
        assert t2 is not None
        assert pool.in_use() == 2

        # 3rd launch refused because worker_slots=2 is full
        t3 = pool.try_acquire("launch")
        assert t3 is None
        assert pool.in_use() == 2

    def test_person_wake_acquires_reserve_slot(self):
        pool = SlotPool(worker_slots=2, reserve_slots=1)
        t1 = pool.try_acquire("launch")
        t2 = pool.try_acquire("launch")
        assert t1 is not None and t2 is not None
        assert pool.in_use() == 2

        # Launch is full, but person_wake taps reserve slot
        pw1 = pool.try_acquire("person_wake")
        assert pw1 is not None
        assert pool.in_use() == 3

        # Second person_wake refused when reserve also full
        pw2 = pool.try_acquire("person_wake")
        assert pw2 is None
        assert pool.in_use() == 3

    def test_release_frees_slot(self):
        pool = SlotPool(worker_slots=1, reserve_slots=1)
        t1 = pool.try_acquire("launch")
        assert t1 is not None
        assert pool.try_acquire("launch") is None

        # Release t1
        released = pool.release(t1)
        assert released is True
        assert pool.in_use() == 0

        # Now launch acquires again
        t2 = pool.try_acquire("launch")
        assert t2 is not None
        assert pool.in_use() == 1

    def test_release_unknown_token_raises_value_error(self):
        pool = SlotPool(worker_slots=1, reserve_slots=1)
        with pytest.raises(ValueError) as excinfo:
            pool.release("unknown-token")
        assert "unknown-token" in str(excinfo.value)

        # Releasing the same token twice raises ValueError on the second release
        t1 = pool.try_acquire("launch")
        assert t1 is not None
        assert pool.release(t1) is True
        with pytest.raises(ValueError) as excinfo2:
            pool.release(t1)
        assert t1 in str(excinfo2.value)

    def test_invalid_kind_raises(self):
        pool = SlotPool(worker_slots=2, reserve_slots=1)
        with pytest.raises(ValueError, match="Invalid slot acquire kind"):
            pool.try_acquire("unknown_kind")
