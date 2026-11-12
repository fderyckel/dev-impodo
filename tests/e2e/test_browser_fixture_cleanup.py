"""Browser startup failures must release Playwright before other tests run."""

import importlib
import unittest
from unittest.mock import Mock

import pytest

BROWSER_CLASSES = (
    ("test_constant_relationship_editor", "ConstantRelationshipEditorTests"),
    ("test_derived_entities_navigation", "DerivedEntitiesNavigationTests"),
    ("test_source_confirmation_guard", "SourceConfirmationGuardTests"),
    ("test_source_save_recovery", "SourceSaveRecoveryBrowserTests"),
    ("test_source_workflow_linked_data_guard", "SourceWorkflowLinkedDataGuardTests"),
    ("test_value_match_visibility", "ValueMatchVisibilityTests"),
)


@pytest.mark.parametrize(("module_name", "class_name"), BROWSER_CLASSES)
@pytest.mark.parametrize("failure_phase", (None, "launch", "close"))
def test_browser_class_releases_runtime_after_setup_and_cleanup_failures(
    monkeypatch, module_name, class_name, failure_phase
):
    module = importlib.import_module(f"tests.e2e.{module_name}")
    browser_class = getattr(module, class_name)
    driver, browser = Mock(), Mock()
    events = []

    def close_browser():
        events.append("close browser")
        if failure_phase == "close":
            raise RuntimeError("injected browser close failure")

    browser.close.side_effect = close_browser
    driver.stop.side_effect = lambda: events.append("stop Playwright")
    driver.chromium.launch.return_value = browser
    if failure_phase == "launch":
        driver.chromium.launch.side_effect = RuntimeError(
            "injected browser launch failure"
        )
    manager = Mock()
    manager.start.return_value = driver
    monkeypatch.setattr(module, "sync_playwright", Mock(return_value=manager))

    class ProbeCase(browser_class):
        __unittest_skip__ = False

        def setUp(self):
            pass

        def tearDown(self):
            pass

        def runTest(self):
            pass

    result = unittest.TestResult()
    unittest.TestSuite((ProbeCase("runTest"),)).run(result)

    assert result.failures == []
    if failure_phase:
        assert len(result.errors) == 1
        assert f"injected browser {failure_phase} failure" in result.errors[0][1]
    else:
        assert result.errors == []
    assert result.testsRun == (0 if failure_phase == "launch" else 1)
    assert events == (
        ["stop Playwright"]
        if failure_phase == "launch"
        else ["close browser", "stop Playwright"]
    )
    driver.stop.assert_called_once_with()
