"""An in-memory mailbox with sample threads: try the tools and the confirmation window without Exchange,
and run the tests. `OWA_MAIL_BACKEND=demo`."""

from __future__ import annotations

import itertools
import threading
from dataclasses import replace
from datetime import datetime, timedelta

from .backend import AttachmentData, BackendError, FolderScope, SearchQuery, SearchResult, pick_attachment
from .models import Message, MessageSummary, Outgoing, Person
from .text import preview
from .when import now

ME = Person(email="ivan.petrov@example.com", name="Иван Петров", job_title="Руководитель проекта")
PEOPLE = [
    ME,
    Person(email="anna.smirnova@example.com", name="Анна Смирнова", job_title="Финансовый аналитик", department="Финансы"),
    Person(email="oleg.kuznetsov@example.com", name="Олег Кузнецов", job_title="Тимлид", department="Разработка"),
    Person(email="maria.ivanova@example.com", name="Мария Иванова", job_title="HR-партнёр", department="HR"),
    Person(email="john.doe@partner.org", name="John Doe", job_title="Account manager"),
]
_by_email = {p.email: p for p in PEOPLE}


class DemoBackend:
    name = "demo"

    def __init__(self, reference: datetime | None = None) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.sent: list[Outgoing] = []
        ref = reference or now()
        self.messages: list[Message] = []
        anna, oleg, maria, john = PEOPLE[1:]

        def add(sender, to, subject, body, hours_ago, folder="Inbox", is_read=True, conv=None, cc=(), attachments=()):
            msg = Message(
                id=f"demo-{next(self._ids)}",
                subject=subject,
                sender=sender,
                received=ref - timedelta(hours=hours_ago),
                folder=folder,
                is_read=is_read,
                has_attachments=bool(attachments),
                preview=preview(body),
                conversation_id=conv,
                to=list(to),
                cc=list(cc),
                body=body,
                attachments=list(attachments),
            )
            self.messages.append(msg)
            return msg

        add(anna, [ME], "Бюджет Q4: черновик", "Иван, добрый день!\n\nПрикладываю черновик бюджета на Q4. Посмотри, пожалуйста, строки по подрядчикам до пятницы.\n\nАнна", 72, conv="c-budget", cc=[oleg], attachments=["budget_q4.xlsx"])
        add(ME, [anna], "RE: Бюджет Q4: черновик", "Анна, спасибо. Подрядчиков сократим на 10%, остальное ок.\n\nFrom: Анна Смирнова\nSent: Monday\nSubject: Бюджет Q4: черновик\n\nИван, добрый день! ...", 50, folder="Sent Items", conv="c-budget")
        add(oleg, [ME, anna], "RE: Бюджет Q4: черновик", "Минус 10% по подрядчикам означает, что релиз 3.2 сдвинется на две недели. Предлагаю обсудить в четверг.\n\nОт: Иван Петров\nОтправлено: понедельник\nТема: RE: Бюджет Q4\n\nАнна, спасибо...", 26, is_read=False, conv="c-budget")
        add(anna, [ME, oleg], "RE: Бюджет Q4: черновик", "Ок, ставлю встречу на четверг 15:00. До неё нужна оценка сдвига от Олега.", 3, is_read=False, conv="c-budget")
        add(maria, [ME], "Отпуск в ноябре", "Иван, подтвердите, пожалуйста, ваш отпуск с 10 по 21 ноября до конца недели.", 20, is_read=False, conv="c-vacation")
        add(john, [ME], "Contract renewal 2027", "Hi Ivan,\n\nOur contract expires on Dec 31. Could we schedule a call next week to discuss renewal terms?\n\nBest,\nJohn", 8, is_read=False, conv="c-contract")
        add(oleg, [ME], "Инцидент в проде: разбор", "Коротко: в 02:10 упал платёжный шлюз, восстановили за 40 минут. Постмортем в приложении.", 30, conv="c-incident", attachments=["postmortem.txt"])
        self.files = {
            "postmortem.txt": ("text/plain", "Постмортем\n02:10 — отказ платёжного шлюза.\n02:50 — восстановлено.\nПричина: истёк сертификат.".encode()),
            "budget_q4.xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK..."),
        }

    # --- MailBackend -------------------------------------------------------------------------

    def whoami(self) -> Person:
        return ME

    def unread_count(self) -> int | None:
        return sum(1 for m in self.messages if m.folder == "Inbox" and not m.is_read)

    def _in_scope(self, msg: Message, scope: FolderScope) -> bool:
        if scope == "inbox":
            return msg.folder == "Inbox"
        if scope == "sent":
            return msg.folder == "Sent Items"
        return True

    def search(self, query: SearchQuery) -> SearchResult:
        def matches(m: Message) -> bool:
            if not self._in_scope(m, query.folder):
                return False
            if query.since and m.received and m.received < query.since:
                return False
            if query.until and m.received and m.received >= query.until:
                return False
            if query.unread_only and m.is_read:
                return False
            if query.has_attachments is not None and m.has_attachments != query.has_attachments:
                return False
            if query.sender and not _person_matches(m.sender, query.sender):
                return False
            if query.recipient and not any(_person_matches(p, query.recipient) for p in m.to + m.cc):
                return False
            if query.subject and query.subject.lower() not in m.subject.lower():
                return False
            if query.text:
                hay = f"{m.subject}\n{m.body}\n{m.sender.name if m.sender else ''}".lower()
                if not all(word in hay for word in query.text.lower().split()):
                    return False
            return True

        with self._lock:
            found = sorted((m for m in self.messages if matches(m)), key=lambda m: m.received, reverse=True)
        return SearchResult(messages=[_summary(m) for m in found[: query.limit]], scanned=len(self.messages), complete=len(found) <= query.limit)

    def get_message(self, message_id: str) -> Message:
        with self._lock:
            for m in self.messages:
                if m.id == message_id:
                    return replace(m)
        raise BackendError("No message with this message_id. Take ids from check_mail, search_messages or get_thread.")

    def get_thread(self, message_id: str, max_messages: int) -> list[Message]:
        anchor = self.get_message(message_id)
        with self._lock:
            thread = [replace(m) for m in self.messages if anchor.conversation_id and m.conversation_id == anchor.conversation_id]
        thread.sort(key=lambda m: m.received)
        return thread[-max_messages:] or [anchor]

    def scan_headers(self, folder: FolderScope, since, until, max_items: int) -> list[MessageSummary]:
        result = self.search(SearchQuery(folder=folder, since=since, until=until, limit=max_items))
        return result.messages

    def read_attachment(self, message_id: str, attachment: str) -> AttachmentData:
        msg = self.get_message(message_id)
        name = pick_attachment(msg.attachments, attachment, lambda n: n)
        content_type, data = self.files.get(name, (None, b""))
        return AttachmentData(name=name, content_type=content_type, size=len(data), data=data)

    def find_people(self, query: str, limit: int) -> list[Person]:
        q = query.lower().strip()
        return [p for p in PEOPLE if _person_matches(p, q)][:limit]

    def send(self, message: Outgoing) -> str | None:
        with self._lock:
            self.sent.append(message)
            original = next((m for m in self.messages if m.id == message.reply_to_id), None)
            msg = Message(
                id=f"demo-{next(self._ids)}",
                subject=message.subject,
                sender=ME,
                received=now(),
                folder="Sent Items",
                preview=preview(message.body),
                conversation_id=original.conversation_id if original else f"c-sent-{len(self.sent)}",
                to=[_by_email.get(x, Person(email=x)) for x in message.to],
                cc=[_by_email.get(x, Person(email=x)) for x in message.cc],
                body=message.body,
            )
            self.messages.append(msg)
            return msg.id


def _person_matches(person: Person | None, needle: str) -> bool:
    if person is None:
        return False
    needle = needle.lower().strip()
    name = (person.name or "").lower()
    email = (person.email or "").lower()
    if needle == email or needle in email:
        return True
    return all(part in name for part in needle.split())


def _summary(m: Message) -> MessageSummary:
    fields = {k: getattr(m, k) for k in MessageSummary.__dataclass_fields__}
    return MessageSummary(**fields)
