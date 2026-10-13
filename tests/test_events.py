"""事件基础设施的测试：总线、扇出中心、插话队列。

这三者 M3.2 之前长在 ``server.py`` 里，跟着 HTTP 一起测。
挪进 ``events.py`` 之后它们有了自己的家 —— 它们本来就不含任何 HTTP 概念，
而网页工作台要在**编排层**用它们，那里不许 import ``server``。
"""

from __future__ import annotations

import unittest

from sparky.events import (
    EPHEMERAL_EVENT_TYPES,
    TURN_TOKEN,
    Event,
    EventBus,
    EventHub,
    InterjectionQueue,
)


def make_event(seq: int, event_type: str) -> Event:
    return Event(type=event_type, session_id="s", seq=seq, data={"seq": seq})


class TestEventBus(unittest.TestCase):
    def test_publish_reaches_every_subscriber(self):
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(seen.append)
        bus.subscribe(seen.append)
        bus.publish(make_event(1, "session.start"))
        self.assertEqual([e.seq for e in seen], [1, 1])

    def test_unknown_event_type_raises(self):
        """未登记的事件类型是程序错误，不能静默丢弃 —— 前端会永久卡住。"""
        bus = EventBus()
        with self.assertRaises(ValueError) as ctx:
            bus.publish(make_event(1, "没登记过的事件"))
        self.assertIn("未登记的事件类型", str(ctx.exception))

    def test_subscriber_exception_propagates(self):
        """表现层出错必须被看见，否则用户看到一个安静但错误的界面。"""
        bus = EventBus()

        def boom(_event: Event) -> None:
            raise RuntimeError("订阅者炸了")

        bus.subscribe(boom)
        with self.assertRaises(RuntimeError):
            bus.publish(make_event(1, "session.start"))


class TestEventHub(unittest.TestCase):
    def test_non_ephemeral_events_are_kept_as_history(self):
        hub = EventHub()
        hub.publish(make_event(1, "session.start"))
        hub.publish(make_event(2, "brief.ready"))
        _, history = hub.subscribe()
        self.assertEqual([e.seq for e in history], [1, 2])

    def test_ephemeral_events_are_not_history(self):
        """turn.token 不落盘、也不进历史 —— 否则刷新页面会重放上千条增量。"""
        hub = EventHub()
        hub.publish(make_event(1, "session.start"))
        hub.publish(make_event(2, TURN_TOKEN))
        _, history = hub.subscribe()
        self.assertEqual([e.seq for e in history], [1])

    def test_subscribe_returns_snapshot_and_live_queue(self):
        hub = EventHub()
        hub.publish(make_event(1, "session.start"))
        queue_, history = hub.subscribe()
        hub.publish(make_event(2, "brief.ready"))
        self.assertEqual([e.seq for e in history], [1])
        self.assertEqual(queue_.get_nowait().seq, 2)

    def test_every_subscriber_gets_every_event(self):
        hub = EventHub()
        first, _ = hub.subscribe()
        second, _ = hub.subscribe()
        hub.publish(make_event(1, "session.start"))
        self.assertEqual(first.get_nowait().seq, 1)
        self.assertEqual(second.get_nowait().seq, 1)

    def test_unsubscribe_stops_delivery(self):
        hub = EventHub()
        queue_, _ = hub.subscribe()
        hub.unsubscribe(queue_)
        hub.publish(make_event(1, "session.start"))
        self.assertTrue(queue_.empty())
        self.assertEqual(hub.client_count, 0)

    def test_ephemeral_event_still_reaches_live_clients(self):
        """不落盘 ≠ 不广播。界面靠这些增量做打字机。"""
        hub = EventHub()
        queue_, _ = hub.subscribe()
        hub.publish(make_event(1, TURN_TOKEN))
        self.assertEqual(queue_.get_nowait().type, TURN_TOKEN)

    def test_ephemeral_set_is_exactly_the_high_frequency_one(self):
        self.assertEqual(EPHEMERAL_EVENT_TYPES, frozenset({TURN_TOKEN}))


class TestInterjectionQueue(unittest.TestCase):
    def test_add_then_drain_returns_once(self):
        q = InterjectionQueue()
        q.add("第一条", received_round=1)
        q.add("第二条", received_round=1)
        self.assertEqual([n["text"] for n in q.drain()], ["第一条", "第二条"])
        self.assertEqual(q.drain(), [], "取过一次就不该再取到")
        self.assertEqual(q.pending_count, 0)

    def test_all_keeps_everything_ever_added(self):
        """``all`` 是"收到过的全部"，``drain`` 是"还没送达的" —— 两者职责不同。"""
        q = InterjectionQueue()
        q.add("甲", received_round=1)
        q.drain()
        q.add("乙", received_round=2)
        self.assertEqual([n["text"] for n in q.all], ["甲", "乙"])

    def test_record_is_shared_not_copied(self):
        """队列与会话记录必须共享同一个 dict —— 否则 delivered_round 会分叉。"""
        q = InterjectionQueue()
        record = q.add("共享对象", received_round=1)
        record["delivered_round"] = 2
        self.assertEqual(q.all[0]["delivered_round"], 2)

    def test_new_record_starts_undelivered(self):
        q = InterjectionQueue()
        self.assertIsNone(q.add("还没送", received_round=1)["delivered_round"])


if __name__ == "__main__":
    unittest.main()
