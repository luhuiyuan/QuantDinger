from app.services.task_control.errors import is_temporary_error, retry_delay_seconds


def test_error_classifier_distinguishes_temporary_errors():
    assert is_temporary_error("TimeoutError")
    assert is_temporary_error("custom_retry", ("custom_retry",))
    assert not is_temporary_error("ValueError")


def test_retry_delay_is_exponential_and_bounded(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda low, high: 0)
    assert retry_delay_seconds(1) == 2
    assert retry_delay_seconds(2) == 4
    assert retry_delay_seconds(20) == 300
