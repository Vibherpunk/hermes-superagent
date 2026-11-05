"""Tests for kanban worker toolset blocklist and deny list (E-020)."""
from __future__ import annotations

import hermes_bootstrap
import unittest

from toolsets import resolve_toolset
from tools.delegate_tool_toolsets import (
    DELEGATE_BLOCKED_TOOLS,
    KANBAN_WORKER_BLOCKED_TOOLS,
    _blocked_toolsets_for_role,
    _strip_blocked_tools,
    kanban_worker_deny_toolsets,
)


class TestKanbanWorkerBlocklist(unittest.TestCase):
    def test_kanban_worker_blocked_tools_superset_of_delegate(self):
        """DELEGATE_BLOCKED_TOOLS is a strict subset of KANBAN_WORKER_BLOCKED_TOOLS,
        and the difference equals exactly the 14 UI tool names."""
        expected_diff = {
            "annotate_preview",
            "close_terminal",
            "desktop_preview",
            "desktop_project",
            "drive_preview",
            "focus_pane",
            "gui_tour",
            "manage_catalog",
            "react_to_message",
            "read_terminal",
            "read_window_below",
            "set_theme",
            "setup_beat",
            "show_tip",
        }
        self.assertTrue(DELEGATE_BLOCKED_TOOLS < KANBAN_WORKER_BLOCKED_TOOLS)
        diff = KANBAN_WORKER_BLOCKED_TOOLS - DELEGATE_BLOCKED_TOOLS
        self.assertEqual(diff, expected_diff)

    def test_kanban_worker_deny_toolsets(self):
        """kanban_worker_deny_toolsets() returns the 8 deny toolsets and never includes kanban."""
        expected_deny = [
            "catalog",
            "clarify",
            "cronjob",
            "delegation",
            "desktop_ui",
            "memory",
            "onboarding",
            "project",
        ]
        deny = kanban_worker_deny_toolsets()
        self.assertEqual(deny, expected_deny)
        self.assertNotIn("kanban", deny)

    def test_coverage_of_blocked_tools_in_deny_toolsets(self):
        """Coverage: every tool in KANBAN_WORKER_BLOCKED_TOOLS that appears in any
        resolve_toolset(n, include_registry=False) is covered by the union of the deny toolsets."""
        from toolsets import TOOLSETS

        covered_tools = set()
        for ts_name in kanban_worker_deny_toolsets():
            resolved = resolve_toolset(ts_name, include_registry=False)
            if resolved:
                covered_tools.update(resolved)

        resolvable_blocked_tools = set()
        for ts_name in TOOLSETS:
            resolved = resolve_toolset(ts_name, include_registry=False)
            if resolved:
                for tool in resolved:
                    if tool in KANBAN_WORKER_BLOCKED_TOOLS:
                        resolvable_blocked_tools.add(tool)

        self.assertTrue(
            resolvable_blocked_tools <= covered_tools,
            f"Uncovered blocked tools: {resolvable_blocked_tools - covered_tools}",
        )

    def test_delegate_behavior_unchanged(self):
        """Delegate behavior remains unchanged."""
        self.assertEqual(
            _blocked_toolsets_for_role("leaf"),
            ["clarify", "cronjob", "delegation", "memory"],
        )
        self.assertEqual(
            _blocked_toolsets_for_role("orchestrator"),
            ["clarify", "cronjob", "memory"],
        )
        self.assertEqual(
            _strip_blocked_tools(["kanban", "delegation", "desktop_ui", "terminal", "memory"]),
            ["desktop_ui", "terminal"],
        )
