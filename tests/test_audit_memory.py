import unittest
import tempfile
import tracemalloc
from pathlib import Path

from src.memory.layer1_summarizer import GroupTopicSummarizer, TopicState


class TopicAuditTest(unittest.TestCase):
    def test_trimming_keeps_unrelated_old_content_out_of_new_topic(self):
        summarizer = GroupTopicSummarizer(max_topics_per_stream=1)
        old = TopicState(topic_id="old", keywords=["music"], key_points=["rehearsal"], last_updated=1.0)
        new = TopicState(topic_id="new", keywords=["food"], key_points=["lunch"], last_updated=2.0)
        summarizer.topics["chat"] = {"old": old, "new": new}

        summarizer._trim_if_needed("chat")

        self.assertTrue(old.is_closed)
        self.assertFalse(new.is_closed)
        self.assertEqual(new.keywords, ["food"])
        self.assertEqual(new.key_points, ["lunch"])


class MemoryScanAuditTest(unittest.TestCase):
    def test_monthly_scan_does_not_load_unused_large_contents(self):
        from src.memory.insight_engine import InsightEngine
        from src.memory.schema import MemoryAtom, configure_memory_database, initialize_database, memory_db

        previous_path = memory_db.database
        with tempfile.TemporaryDirectory() as directory:
            configure_memory_database(str(Path(directory) / "memory.db"))
            initialize_database()
            try:
                with memory_db.atomic():
                    for index in range(24):
                        MemoryAtom.create(
                            atom_id=str(index),
                            atom_type="factual",
                            content="x" * 1_000_000,
                            entities='["person"]',
                        )
                engine = InsightEngine(object())
                tracemalloc.start()
                try:
                    insights = engine._scan_atomic_patterns()
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertTrue(insights)
                self.assertLess(peak, 4_000_000, "统计扫描不能把全部原子正文加载到内存")
            finally:
                memory_db.close()
                configure_memory_database(str(previous_path))
