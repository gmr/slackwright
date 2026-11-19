import importlib.metadata
import unittest

import slackwright


class VersionTestCase(unittest.TestCase):
    def test_version_matches_metadata(self) -> None:
        self.assertEqual(
            slackwright.version, importlib.metadata.version('slackwright')
        )
