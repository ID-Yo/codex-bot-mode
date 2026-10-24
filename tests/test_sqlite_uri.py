import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room.sqlite_uri import readonly_uri, uri_for  # noqa: E402


class ReadonlyUriTests(unittest.TestCase):
    def test_extended_local_drive_path(self):
        self.assertEqual(uri_for('\\\\?\\D:\\Work\\office.db'), 'file:///D:/Work/office.db?mode=ro')

    def test_extended_path_with_spaces_and_non_ascii(self):
        self.assertEqual(uri_for('\\\\?\\D:\\My Work\\\u0431\u043e\u0442 \u00e9.db'),
                         'file:///D:/My%20Work/%D0%B1%D0%BE%D1%82%20%C3%A9.db?mode=ro')

    def test_extended_unc_path(self):
        self.assertEqual(uri_for('\\\\?\\UNC\\server\\share\\a b\\\u00e9.db'),
                         'file://server/share/a%20b/%C3%A9.db?mode=ro')

    def test_plain_unc_and_drive_paths_are_unchanged(self):
        self.assertEqual(uri_for('\\\\server\\share\\x.db'), 'file://server/share/x.db?mode=ro')
        self.assertEqual(uri_for('C:\\a b\\x.db'), 'file:///C:/a%20b/x.db?mode=ro')

    def test_no_authority_is_ever_a_question_mark(self):
        for text in ('\\\\?\\D:\\x.db', '\\\\?\\UNC\\s\\h\\x.db', '\\\\.\\D:\\x.db'):
            self.assertNotIn('%3F', uri_for(text))

    @unittest.skipUnless(sys.platform == 'win32', 'extended paths are Windows only')
    def test_sqlite_opens_a_database_through_an_extended_path(self):
        with tempfile.TemporaryDirectory(prefix='\u0431\u043e\u0442 ') as tmp:
            file = Path(tmp) / 'caf\u00e9 office.db'
            with closing(sqlite3.connect(file)) as db:
                db.execute('CREATE TABLE t(x)')
                db.execute('INSERT INTO t VALUES (7)')
                db.commit()
            extended = Path('\\\\?\\' + str(file.resolve()))
            uri = readonly_uri(extended)
            self.assertNotIn('%3F', uri)
            with closing(sqlite3.connect(uri, uri=True)) as db:
                self.assertEqual(db.execute('SELECT x FROM t').fetchone(), (7,))
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('INSERT INTO t VALUES (8)')


if __name__ == '__main__':
    unittest.main()
