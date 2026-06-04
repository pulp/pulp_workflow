from unittest import mock

from pulp_workflow.app import signals as signals_module


def test_signal_skips_when_all_tasks_dispatched_is_false():
    """The handler is a no-op when the TaskGroup save did not flip the flag."""
    instance = mock.Mock(all_tasks_dispatched=False)

    with mock.patch.object(signals_module, "transaction") as txn:
        signals_module.sync_workflow_state_on_task_group_dispatch(
            sender=mock.Mock(), instance=instance
        )

    txn.atomic.assert_not_called()
