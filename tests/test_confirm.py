import threading

from owa_mail_mcp.confirm import BrokerBusy, ConfirmationBroker, Status
from owa_mail_mcp.models import Outgoing
from owa_mail_mcp.safety import OutgoingReview, check_outgoing

import pytest


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _broker(clock=None, **kw):
    return ConfirmationBroker(lambda m: check_outgoing(m, my_email="me@corp.ru"), clock=clock or Clock(), **kw)


def _submit(broker, timeout=30):
    return broker.submit(Outgoing(to=["a@corp.ru"], subject="s", body="b"), OutgoingReview(), client="test", sender="me@corp.ru", timeout=timeout)


def test_expires_at_deadline():
    clock = Clock()
    broker = _broker(clock)
    p = _submit(broker, timeout=10)
    clock.t += 11
    assert broker.wait(p.id, 0.01) == Status.EXPIRED
    assert broker.decide(p.id, True) == "Already expired."


def test_answer_is_taken_once():
    broker = _broker()
    p = _submit(broker)
    assert broker.decide(p.id, False) is None
    assert broker.decide(p.id, True) == "Already rejected."
    assert broker.wait(p.id, 0.01) == Status.REJECTED


def test_invalid_edit_is_refused_and_question_stays():
    broker = _broker()
    p = _submit(broker)
    error = broker.decide(p.id, True, Outgoing(to=["bad"], subject="s", body="b"))
    assert "plain email" in error
    assert broker.get(p.id).status == Status.PENDING


def test_wait_wakes_on_decision():
    broker = ConfirmationBroker(lambda m: OutgoingReview())
    p = broker.submit(Outgoing(to=["a@corp.ru"], subject="s", body="b"), OutgoingReview(), client="", sender="", timeout=30)
    threading.Timer(0.05, lambda: broker.decide(p.id, True)).start()
    assert broker.wait(p.id, 5) == Status.APPROVED


def test_pending_cap():
    broker = _broker(max_pending=2)
    _submit(broker), _submit(broker)
    with pytest.raises(BrokerBusy):
        _submit(broker)


def test_cancel_only_while_pending():
    broker = _broker()
    p = _submit(broker)
    broker.decide(p.id, True)
    broker.cancel(p.id)
    assert broker.get(p.id).status == Status.APPROVED
